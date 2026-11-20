from __future__ import annotations

import json
import logging
from pathlib import Path
from string import Formatter
import threading
import time

from PIL import Image

from .controls import control_commands, diagnostic_values, night_commands, night_phase, night_signature
from .media import encode_full_frames, encode_jpeg, publish
from .lighting import lighting_approval, lighting_payload, target_signature
from .models import ControlRequest, METRICS, TAKEOVER_CONTROLS, Device, LightingSettings, Screen, http_base
from .protocol import DeviceError, device_command, native_clock_payload, play_gif, send_jpeg
from .render import render
from .store import Store
from .strava import Strava
from .widgets import WidgetSources

logger = logging.getLogger(__name__)


class Engine(threading.Thread):
    def __init__(self, store: Store, directory: Path, media_base_url: str,
                 strava: Strava, *, demo: bool = False):
        super().__init__(daemon=True, name="timesgate-scheduler")
        self.store, self.directory = store, directory
        self.media_base_url, self.strava, self.demo = media_base_url, strava, demo
        self.stopping = threading.Event()
        self.last_cleanup = 0.0
        self.last_heartbeat = 0.0
        self.screen_send_ready: dict[str, float] = {}
        self.sources = WidgetSources(demo=demo)

    def image(self, screen: Screen, timezone: str, *, preview: bool = False) -> Image.Image:
        weather = None
        headline = None
        metrics = None
        if screen.kind == "weather":
            if screen.latitude is None or screen.longitude is None:
                raise ValueError("Weather requires latitude and longitude")
            weather = self.sources.weather(screen.latitude, screen.longitude)
        elif screen.kind == "rss":
            headline = self.sources.headline(screen.url, screen.news_seconds, time.time())
        elif screen.kind == "custom" and any(
            (layer.type == "bar" and layer.metric != "value")
            or (layer.type == "text" and any(
                name in METRICS for _, name, _, _ in Formatter().parse(layer.text)
            ))
            for layer in screen.elements
        ):
            metrics = self.sources.metrics()
        return render(screen, timezone, self.store.get("strava_cache"),
                      weather=weather, headline=headline, metrics=metrics, media_dir=self.directory,
                      preview=preview)

    def select(self, device_id: str, panel: int) -> tuple[Device, Screen, str]:
        config = self.store.config()
        device = next((d for d in config.devices if d.id == device_id), None)
        if device is None or panel not in range(5):
            raise ValueError("Unknown device or screen")
        return device, device.screens[panel], config.timezone

    def content(self, device_id: str, panel: int) -> tuple[str, bool]:
        _, screen, timezone = self.select(device_id, panel)
        if screen.kind == "media":
            name = screen.media_id + ".gif"
            if not screen.media_id or not (self.directory / name).is_file():
                raise ValueError("Choose an existing media file")
            return name, False
        if screen.kind == "unmanaged":
            raise ValueError("This screen is unmanaged; choose content first")
        image = self.image(screen, timezone)
        name = publish(self.directory, encode_full_frames([image], [1000]))
        self.store.remember_render(name)
        return name, True

    def process_control(self, job: dict) -> None:
        device, _, timezone = self.select(job["device_id"], 0)
        payload = json.loads(job["payload"])
        if payload["target"] != target_signature(device):
            raise ValueError("Device connection changed; review and submit the control again")
        scheduled = job["operation"] == "device_schedule"
        request = None
        if scheduled:
            if (
                not device.night.enabled or payload["signature"] != night_signature(device, timezone)
                or payload["phase"] != night_phase(device.night, timezone, time.time())
            ):
                self.store.finish(job["id"], "cancelled", "Night schedule changed or its time window passed")
                return
            action = f"Night mode {payload['phase']}"
            commands = night_commands(device.night, payload["phase"])
        else:
            request = ControlRequest.model_validate(payload["request"])
            action = request.control.action
            if action == "resume":
                count = self.store.resume_device(device)
                self.store.finish(job["id"], "done", f"Controller resumed; {count} managed screens queued")
                return
            commands = control_commands(request, device, timezone, time.time())
            if action in (*TAKEOVER_CONTROLS, "reboot"):
                self.store.update_controls(device, paused=True)
            if action == "preferences" and not self.demo:
                self.store.update_controls(device, preferences=None)
        completed = 0
        try:
            for command in commands:
                if command["Command"] == "Channel/OnOffScreen" and command["OnOff"] == 0:
                    # Preserve the sleep guard even if the device's response is uncertain.
                    self.store.update_controls(device, sleeping=True)
                body = {} if self.demo else device_command(device, command)
                completed += 1
                if command["Command"] == "Channel/OnOffScreen" and command["OnOff"] == 1:
                    self.store.update_controls(device, sleeping=False)
                if command["Command"] == "Channel/SetBrightness" and not self.demo:
                    self.store.update_controls(device, brightness=command["Brightness"])
                if action == "read_settings":
                    self.store.update_controls(device, diagnostic={
                        "available": not self.demo, "checked_at": time.time(),
                        "values": diagnostic_values(body),
                        "message": "Demo only; no query sent." if self.demo else
                        "Query acknowledged. Only supported numeric fields are shown; this is not live monitoring.",
                    })
        except DeviceError as error:
            if action == "read_settings":
                self.store.update_controls(device, diagnostic={
                    "available": False, "checked_at": time.time(), "values": {},
                    "message": "Status unavailable; see Activity. This does not mark the device offline.",
                })
            raise DeviceError(f"{action}: {completed}/{len(commands)} commands acknowledged. {error}") from None
        if self.demo:
            self.store.update_controls(device, simulated_at=time.time())
        else:
            changes: dict = {"acknowledged_at": time.time()}
            if request and action == "preferences":
                changes["preferences"] = request.control.model_dump(exclude={"action"})
            self.store.update_controls(device, **changes)
        self.store.finish(
            job["id"], "demo" if self.demo else "acknowledged",
            f"{action}: simulated; no device commands sent." if self.demo else
            f"{action}: {completed}/{len(commands)} commands acknowledged; physical effect not verified.",
        )

    def process(self, job: dict) -> None:
        key = job["id"]
        try:
            if job["operation"] == "strava":
                if self.demo:
                    raise ValueError("Strava network access is disabled in demo mode")
                self.strava.fetch()
                self.store.finish(key, "done", "Strava statistics refreshed")
                return
            if job["operation"] in ("control", "device_schedule"):
                self.process_control(job)
                return
            if job["operation"] in ("lighting", "lighting_restore"):
                restoring = job["operation"] == "lighting_restore"
                device, _, _ = self.select(job["device_id"], 0)
                payload = json.loads(job["payload"])
                if payload["target"] != target_signature(device):
                    raise ValueError("Device connection changed; review and apply lighting again")
                if restoring:
                    saved = self.store.get(f"lighting:{device.id}")
                    with self.store.connect() as db:
                        manual = db.execute(
                            "SELECT 1 FROM jobs WHERE operation='lighting' AND device_id=? "
                            "AND status IN ('pending','running')", (device.id,),
                        ).fetchone()
                    if (
                        saved is None or not saved.get("restore_on_startup", False)
                        or saved["target"] != payload["target"]
                        or lighting_approval(saved) != payload["approval"]
                        or manual is not None
                    ):
                        self.store.finish(
                            key, "cancelled", "Restoration disabled, approval changed or manual Apply queued.",
                        )
                        return
                settings = LightingSettings.model_validate(payload["settings"])
                if self.demo:
                    self.store.finish(key, "demo", "Lighting simulated; no device command or settings saved")
                    return
                device_command(device, lighting_payload(settings))
                self.store.remember_lighting(device, settings, restored=restoring)
                self.store.finish(key, "acknowledged",
                                  ("Startup restoration" if restoring else "Manual lighting")
                                  + " acknowledged; visible lighting not verified")
                return
            device, screen, timezone = self.select(job["device_id"], job["panel"])
            controls = self.store.control_state(device)
            if controls["sleeping"] or controls["paused"]:
                self.store.finish(key, "cancelled", "Displays asleep or controller paused; wake/resume before sending")
                return
            if job["automatic"] and not screen.enabled:
                self.store.finish(key, "cancelled", "Schedule was disabled before execution")
                return
            native = screen.kind == "clock" and device.clock_renderer == "native"
            direct = (
                screen.kind not in ("media", "unmanaged")
                and not native and device.widget_transport == "jpeg"
            )
            jpeg = None
            clock = None
            url = ""
            if direct:
                jpeg = encode_jpeg(self.image(screen, timezone))
            else:
                if not self.demo and not self.media_base_url:
                    raise ValueError("Set MEDIA_BASE_URL to an HTTP origin reachable by the device")
                if native:
                    name = publish(self.directory, encode_full_frames(
                        [Image.new("RGB", (128, 128), "#08121e")], [1000],
                    ))
                    self.store.remember_render(name)
                else:
                    name, _ = self.content(device.id, job["panel"])
                if not self.demo:
                    url = http_base(self.media_base_url, http_only=True) + "/" + name
                    if native:
                        clock = native_clock_payload(device, job["panel"], screen, url)
            if self.demo:
                self.store.finish(key, "demo", "Rendered in demo mode; no device command sent")
                return
            delay = self.screen_send_ready.get(device.host, 0.0) - time.monotonic()
            if delay > 0 and self.stopping.wait(delay):
                self.store.finish(key, "cancelled", "Controller stopping; screen send not attempted")
                return
            try:
                if clock is not None:
                    request = ControlRequest(device_id=device.id, control={"action": "sync_time"})
                    commands = [*control_commands(request, device, timezone, time.time()), clock]
                    completed = 0
                    try:
                        for command in commands:
                            device_command(device, command)
                            completed += 1
                    except DeviceError as error:
                        raise DeviceError(
                            f"Native clock: {completed}/{len(commands)} commands acknowledged; {error}"
                        ) from None
                    message = (
                        "Native clock setup/time sync acknowledged; "
                        "background download/rendering not verified"
                    )
                elif jpeg is not None:
                    send_jpeg(device, job["panel"], jpeg)
                    message = "Direct JPEG upload acknowledged; rendering not verified"
                else:
                    play_gif(device, job["panel"], url)
                    message = "Playback command acknowledged; download/rendering not verified"
            finally:
                self.screen_send_ready[device.host] = time.monotonic() + 2.0
            self.store.finish(key, "acknowledged", message)
        except Exception as error:
            # Unexpected errors remain visible, without logging secret-bearing response bodies.
            safe_types = (ValueError, RuntimeError)
            message = str(error) if isinstance(error, safe_types) else f"Internal {type(error).__name__}; job failed"
            logger.error("Job %s: %s", key, message)
            self.store.finish(key, "error", message)

    def tick(self, now: float) -> None:
        if not self.demo and self.strava.status()["connected"]:
            due = self.store.get("strava_next_poll", 0) or 0
            queued, strava_queued = self.store.queue_status()
            if due <= now and not strava_queued and queued < 30:
                self.store.enqueue("strava")
        self.store.schedule_due(now)
        if now - self.last_cleanup > 3600:
            self.store.expire_renders(self.directory, now)
            self.last_cleanup = now
        self.last_heartbeat = now

    def run(self) -> None:
        while not self.stopping.is_set():
            try:
                self.tick(time.time())
                job = self.store.claim()
                if job:
                    self.process(job)
                    continue
            except Exception:
                logger.exception("Scheduler failed")
            self.stopping.wait(1)

    def stop(self) -> None:
        self.stopping.set()
