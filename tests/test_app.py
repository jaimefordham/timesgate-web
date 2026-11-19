import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from timesgate.app import create_app
from timesgate.engine import Engine
from timesgate.models import Configuration, DesignLayer, Device, LightingSettings, Screen, WIDGET_ACCENTS
from timesgate.store import Store

class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.app = create_app(root / "data", root / "media", demo=True)
        self.client = TestClient(self.app).__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.headers["Origin"] = "http://localhost:8080"
        response = self.client.post("/api/auth/setup", json={
            "username": "admin", "password": "test-password-for-admin",
        })
        self.assertEqual(response.status_code, 200, response.text)

    def test_authentication_and_origin_protection_and_no_public_media(self):
        self.assertEqual(self.client.get("/api/state", headers={"Cookie": ""}).status_code, 401)
        self.assertEqual(self.client.get("/api/state", headers={"Origin": "http://untrusted.invalid"}).status_code, 403)
        self.assertEqual(self.client.get("/media/file.gif").status_code, 404)
        self.assertTrue(self.client.get("/healthz").json()["ok"])

    def test_lighting_requires_authentication_and_valid_saved_target_and_simulates_in_demo(self):
        settings = {"color": "#36D9FF", "brightness": 60, "zone": 2, "effect": 5}
        store = self.app.state.store
        config = store.config()
        device = config.devices[0]
        values = {"device_id": device.id, "settings": settings}
        self.assertEqual(self.client.post("/api/lighting", json=values, headers={
            "Cookie": "",
        }).status_code, 401)
        self.assertEqual(self.client.post("/api/lighting", json=values, headers={
            "Origin": "http://untrusted.invalid",
        }).status_code, 403)
        self.assertEqual(self.client.post("/api/lighting", json=values).status_code, 400)
        device.host, device.local_token = "192.168.1.10", 123456
        store.save_config(config, store.revision())
        for invalid in (
            {**values, "settings": {**settings, "brightness": 101}},
            {**values, "settings": {**settings, "Command": "Draw/Anything"}},
            {**values, "device_id": "missing"},
        ):
            self.assertEqual(self.client.post("/api/lighting", json=invalid).status_code, 400)
        before = store.public_state()
        with patch("timesgate.engine.device_command") as send:
            response = self.client.post("/api/lighting", json=values)
            self.assertEqual(response.status_code, 200, response.text)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                job = next(item for item in store.jobs() if item["id"] == response.json()["job"])
                if job["status"] not in ("pending", "running"):
                    break
                time.sleep(.02)
            self.assertEqual(job["status"], "demo")
            send.assert_not_called()
        state = self.client.get("/api/state").json()
        self.assertEqual(state["lighting"], {})
        self.assertEqual(store.public_state(), before)
        self.assertEqual(state["lighting_effects"]["back"][5]["name"], "Solid colour")
        self.assertEqual(state["lighting_effects"]["edges"][5]["name"], "Steady light")
        self.assertNotIn("payload", state["jobs"][0])

    def test_scheduled_updates_include_last_automatic_result_and_widget_configuration(self):
        store = self.app.state.store
        with patch.object(self.app.state.engine, "tick"):
            config = store.config()
            device = config.devices[0]
            device.host, device.local_token = "192.168.1.10", 123456
            device.screens[0].kind = "clock"
            device.screens[0].enabled = True
            store.save_config(config, store.revision(), now=time.time() + 3600)
            state = self.client.get("/api/state").json()
            self.assertEqual(state["schedules"], [{
                "device_id": device.id, "panel": 0,
                "next_due": store.schedules()[0]["next_due"], "last_run": None,
            }])
            self.assertEqual(state["config"]["devices"][0]["screens"][0]["kind"], "clock")

    def test_startup_restoration_preference_is_authenticated_strict_and_does_not_send(self):
        store = self.app.state.store
        config = store.config()
        device = config.devices[0]
        device.host, device.local_token = "192.168.1.10", 123456
        store.save_config(config, store.revision())
        values = {"device_id": device.id, "enabled": True}
        self.assertEqual(self.client.put("/api/lighting/restore", json=values).status_code, 400)
        store.remember_lighting(device, LightingSettings())
        self.assertEqual(self.client.put("/api/lighting/restore", json=values, headers={
            "Cookie": "",
        }).status_code, 401)
        self.assertEqual(self.client.put("/api/lighting/restore", json=values, headers={
            "Origin": "http://untrusted.invalid",
        }).status_code, 403)
        self.assertEqual(self.client.put("/api/lighting/restore", json={
            **values, "enabled": "true",
        }).status_code, 400)
        before = store.public_state()
        response = self.client.put("/api/lighting/restore", json=values)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["lighting_restoration"][0]["enabled"])
        self.assertIsNone(response.json()["lighting_restoration"][0]["last_run"])
        self.assertEqual(store.jobs(), [])
        self.assertEqual(store.public_state(), before)

    def test_device_controls_and_full_presets_require_authentication_origin_and_validated_actions(self):
        store = self.app.state.store
        config = store.config()
        device = config.devices[0]
        device.host, device.local_token = "192.168.1.10", 123456
        store.save_config(config, store.revision())
        values = {"device_id": device.id, "control": {"action": "brightness", "value": 55}}
        for headers, status in (({"Cookie": ""}, 401), ({"Origin": "http://untrusted.invalid"}, 403)):
            self.assertEqual(self.client.post("/api/device/control", json=values, headers=headers).status_code, status)
            self.assertEqual(self.client.put("/api/lighting/presets", json={}, headers=headers).status_code, status)
        for control in (
            {"action": "brightness", "value": True}, {"action": "reboot"},
            {"Command": "Device/SysReboot"}, {"action": "power", "on": 1},
        ):
            self.assertEqual(self.client.post("/api/device/control", json={
                **values, "control": control,
            }).status_code, 400)
        with patch("timesgate.engine.device_command") as send:
            response = self.client.post("/api/device/control", json=values)
            self.assertEqual(response.status_code, 200, response.text)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and store.jobs()[0]["status"] in ("pending", "running"):
                time.sleep(.02)
            self.assertEqual(store.jobs()[0]["status"], "demo")
            send.assert_not_called()
        store.update_controls(device, sleeping=True)
        device.screens[0].kind = "clock"
        store.save_config(config, store.revision())
        self.assertEqual(self.client.post("/api/send", json={
            "device_id": device.id, "panel": 0,
        }).status_code, 400)
        response = self.client.put("/api/lighting/presets", json={
            "name": "Evening", "settings": LightingSettings(brightness=20).model_dump(),
        })
        self.assertEqual(response.status_code, 200, response.text)
        preset = response.json()["lighting_presets"][0]
        state = self.client.get("/api/state").json()
        self.assertTrue(state["controls"][device.id]["sleeping"])
        self.assertNotIn("123456", str(state))
        self.assertEqual(state["lighting_presets"], [preset])
        self.assertEqual(self.client.delete(f"/api/lighting/presets/{preset['id']}").status_code, 200)

    def test_application_start_queues_restore_once_and_holds_interrupted_attempts(self):
        root = Path(self.temp.name)
        data, media = root / "startup-data", root / "startup-media"
        store = Store(data / "timesgate.sqlite")
        config = store.config()
        device = config.devices[0]
        device.host, device.local_token = "192.168.1.10", 123456
        store.save_config(config, store.revision())
        store.remember_lighting(device, LightingSettings())
        with TestClient(create_app(data, media, demo=True)):
            self.assertEqual(store.jobs(), [])
        store.configure_lighting_restore(device.id, True)
        with patch("timesgate.engine.device_command") as send:
            for expected in (1, 2):
                with TestClient(create_app(data, media, demo=True)):
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        jobs = store.jobs()
                        if len(jobs) == expected and all(job["status"] == "demo" for job in jobs):
                            break
                        time.sleep(.02)
                    self.assertEqual(len(jobs), expected)
                    self.assertTrue(all(job["operation"] == "lighting_restore" for job in jobs))
                    self.assertTrue(all(job["status"] == "demo" for job in jobs))
            store.queue_startup_lighting()
            interrupted_id = store.jobs()[0]["id"]
            with TestClient(create_app(data, media, demo=True)):
                self.assertEqual(len(store.jobs()), 3)
                self.assertEqual(next(
                    job for job in store.jobs() if job["id"] == interrupted_id
                )["status"], "interrupted")
                self.assertTrue(store.lighting_restoration()[0]["blocked"])
            send.assert_not_called()

    def test_upload_save_preview_queue_and_demo_acknowledgement(self):
        source = io.BytesIO()
        Image.new("RGB", (128, 128), "red").save(source, "GIF")
        upload = self.client.post("/api/media?name=test.gif", content=source.getvalue())
        self.assertEqual(upload.status_code, 200, upload.text)
        state = self.client.get("/api/state").json()
        device = state["config"]["devices"][0]
        device.update(host="192.168.1.10", local_token=123456)
        device["screens"][0].update(kind="media", media_id=upload.json()["id"])
        saved = self.client.put("/api/config", json={"config": state["config"], "revision": state["revision"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        preview = self.client.get(f"/api/preview/{device['id']}/0")
        self.assertEqual(preview.headers["content-type"], "image/gif")
        state = self.client.get("/api/state").json()
        self.assertNotIn("123456", str(state))
        result = self.client.post("/api/send", json={"device_id": device["id"], "panel": 0})
        self.assertEqual(result.status_code, 200)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.client.get("/api/state").json()["jobs"][0]
            if job["status"] not in ("pending", "running"):
                break
            time.sleep(.02)
        self.assertEqual(job["status"], "demo", job)

    def test_invalid_secret_input_is_never_echoed_in_errors(self):
        state = self.client.get("/api/state").json()
        state["config"]["devices"][0]["local_token"] = "sensitive-wrong-input"
        response = self.client.put("/api/config", json={"config": state["config"], "revision": state["revision"]})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("sensitive-wrong-input", response.text)

    def test_new_widgets_share_preview_and_send_rendering_and_persist(self):
        state = self.client.get("/api/state").json()
        device = state["config"]["devices"][0]
        widgets = [
            {"kind": "weather", "latitude": 38.7, "longitude": -9.1},
            {"kind": "countdown", "target": "2099-12-25T00:00"},
            {"kind": "rss", "url": "https://example.com/feed"},
            {"kind": "custom", "elements": [
                {"type": "text", "text": "{time}"},
                {"type": "bar", "y": 80, "metric": "cpu"},
            ]},
        ]
        for panel, widget in enumerate(widgets):
            device["screens"][panel].update(widget)
        response = self.client.put("/api/config", json={
            "config": state["config"], "revision": state["revision"],
        })
        self.assertEqual(response.status_code, 200, response.text)
        restored = self.client.get("/api/state").json()["config"]["devices"][0]["screens"]
        queued = set()
        for panel, widget in enumerate(widgets):
            with self.subTest(kind=widget["kind"]):
                self.assertEqual(restored[panel]["kind"], widget["kind"])
                preview = self.client.get(f"/api/preview/{device['id']}/{panel}")
                self.assertEqual(preview.status_code, 200, preview.text[:100] if preview.status_code != 200 else "")
                self.assertEqual(preview.headers["content-type"], "image/png")
                filename, generated = self.app.state.engine.content(device["id"], panel)
                self.assertTrue(generated)
                with Image.open(io.BytesIO(preview.content)) as png, Image.open(
                    self.app.state.engine.directory / filename,
                ) as gif:
                    self.assertEqual((png.size, gif.size), ((128, 128), (128, 128)))
                sent = self.client.post("/api/send", json={"device_id": device["id"], "panel": panel})
                self.assertEqual(sent.status_code, 200, sent.text)
                queued.add(sent.json()["job"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            jobs = [
                job for job in self.client.get("/api/state").json()["jobs"] if job["id"] in queued
            ]
            if len(jobs) == 4 and all(job["status"] == "demo" for job in jobs):
                break
            time.sleep(.02)
        self.assertEqual([job["status"] for job in jobs], ["demo"] * 4)
        self.assertEqual(
            Store(self.app.state.store.path).config().devices[0].screens[1].target,
            "2099-12-25T00:00",
        )

    def test_custom_image_layers_require_registered_media(self):
        state = self.client.get("/api/state").json()
        state["config"]["devices"][0]["screens"][0].update(
            kind="custom", elements=[{"type": "image", "media_id": "a" * 32}],
        )
        response = self.client.put("/api/config", json={
            "config": state["config"], "revision": state["revision"],
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Selected media does not exist", response.text)

    def test_accents_persist_independently_and_match_preview_and_published_gifs(self):
        state = self.client.get("/api/state").json()
        self.assertEqual(state["widget_accents"], WIDGET_ACCENTS)
        device = state["config"]["devices"][0]
        choices = ["electric-cyan", "solar-gold", "tangerine", "hot-pink", "ultraviolet"]
        kinds = ["clock", "text", "strava", "weather", "countdown"]
        for screen, kind, accent in zip(device["screens"], kinds, choices):
            screen.update(
                kind=kind, accent=accent, latitude=0, longitude=0,
                target="2099-12-25T00:00",
            )
        stats = {"distance": 1250000, "count": 42, "moving_time": 180000}
        self.app.state.store.put("strava_cache", {"fetched_at": 1000, "stats": {"ytd": stats}})
        response = self.client.put("/api/config", json={
            "config": state["config"], "revision": state["revision"],
        })
        self.assertEqual(response.status_code, 200, response.text)
        restored = Store(self.app.state.store.path).config().devices[0].screens
        self.assertEqual([screen.accent for screen in restored], choices)
        for panel, (kind, accent) in enumerate(zip(kinds, choices)):
            with self.subTest(kind=kind, accent=accent):
                position = (6, 20 if kind == "strava" else 23)
                expected = tuple(bytes.fromhex(WIDGET_ACCENTS[accent]["color"][1:]))
                response = self.client.get(f"/api/preview/{device['id']}/{panel}")
                self.assertEqual(response.status_code, 200)
                with Image.open(io.BytesIO(response.content)) as preview:
                    self.assertEqual(preview.getpixel(position), expected)
                filename, _ = self.app.state.engine.content(device["id"], panel)
                with Image.open(self.app.state.engine.directory / filename) as published:
                    self.assertEqual(published.convert("RGB").getpixel(position), expected)

    def test_strava_example_preview_is_labelled_and_never_used_for_sends(self):
        state = self.client.get("/api/state").json()
        device = state["config"]["devices"][0]
        device["screens"][0].update(kind="strava")
        response = self.client.put("/api/config", json={
            "config": state["config"], "revision": state["revision"],
        })
        self.assertEqual(response.status_code, 200, response.text)
        with patch("timesgate.render.ImageDraw.ImageDraw.text", autospec=True) as text:
            preview = self.client.get(f"/api/preview/{device['id']}/0")
        self.assertEqual(preview.status_code, 200, preview.text if preview.status_code != 200 else "")
        self.assertEqual(preview.headers["content-type"], "image/png")
        with Image.open(io.BytesIO(preview.content)) as image:
            self.assertEqual(image.size, (128, 128))
        self.assertIn("EXAMPLE DATA", [call.args[2] for call in text.call_args_list])
        self.assertIsNone(self.app.state.store.get("strava_cache"))
        with self.assertRaisesRegex(ValueError, "No cached Strava statistics"):
            self.app.state.engine.content(device["id"], 0)

    def test_strava_preview_prefers_real_cache_without_example_label(self):
        config = self.app.state.store.config()
        device = config.devices[0]
        device.screens[0] = Screen(kind="strava", strava_mode="all-time", units="imperial")
        self.app.state.store.save_config(config, self.app.state.store.revision())
        stats = {"distance": 160934.4, "count": 12, "moving_time": 36000, "elevation_gain": 1000}
        cache = {"fetched_at": 1000, "stats": {"all-time": stats}}
        self.app.state.store.put("strava_cache", cache)
        with patch("timesgate.render.ImageDraw.ImageDraw.text", autospec=True) as text:
            response = self.client.get(f"/api/preview/{device.id}/0")
        self.assertEqual(response.status_code, 200)
        labels = [call.args[2] for call in text.call_args_list]
        self.assertIn("100", labels)
        self.assertIn("mi cycled", labels)
        self.assertIn("12", labels)
        self.assertIn("RIDES", labels)
        self.assertNotIn("EXAMPLE DATA", labels)
        self.assertTrue(any(label.startswith("Data ") for label in labels))
        self.assertEqual(self.app.state.store.get("strava_cache"), cache)

    def test_multi_device_layered_configuration_fits_config_endpoint(self):
        state = self.client.get("/api/state").json()
        config = Configuration(devices=[Device(screens=[
            Screen(kind="custom", elements=[DesignLayer(text="x" * 300) for _ in range(20)])
            for _ in range(5)
        ]) for _ in range(3)])
        self.assertGreater(len(config.model_dump_json()), 128 * 1024)
        response = self.client.put("/api/config", json={
            "config": config.model_dump(), "revision": state["revision"],
        })
        self.assertEqual(response.status_code, 200, response.text)

    def test_unscheduled_screen_remains_unscheduled_after_restart(self):
        self.assertEqual(self.client.get("/api/state").json()["schedules"], [])
        self.assertEqual(self.client.get("/api/state").json()["jobs"], [])

    def test_recovery_finishes_before_worker_can_accept_new_jobs(self):
        root = Path(self.temp.name)
        store = Store(root / "restart-data" / "timesgate.sqlite")
        config = store.config()
        device = config.devices[0]
        device.screens[0].kind = "clock"
        store.save_config(config, store.revision())
        old_job = store.enqueue("send", device.id, 0)
        new_jobs = []
        original_start = Engine.start

        def start(engine):
            self.assertEqual(engine.store.jobs()[0]["status"], "interrupted")
            new_jobs.append(engine.store.enqueue("send", device.id, 0))
            original_start(engine)

        app = create_app(root / "restart-data", root / "restart-media", demo=True)
        with patch.object(Engine, "start", start), TestClient(app) as client:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                jobs = {job["id"]: job for job in store.jobs()}
                if jobs[new_jobs[0]]["status"] == "demo":
                    break
                time.sleep(.02)
            self.assertEqual(jobs[old_job]["status"], "interrupted")
            self.assertEqual(jobs[new_jobs[0]]["status"], "demo")
