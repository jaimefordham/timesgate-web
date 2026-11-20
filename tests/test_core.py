import base64
from datetime import datetime
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from PIL import Image
import requests

from timesgate.engine import Engine
from timesgate.media import encode_full_frames
from timesgate.models import Configuration, Device, Screen, next_due
from timesgate.protocol import DeviceError, play_gif
from timesgate.store import Conflict, Store
from timesgate.strava import Strava


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite")

    def test_tokens_are_write_only_and_preserved_unless_target_changes(self):
        config = self.store.config()
        device = config.devices[0]
        device.host, device.local_token = "192.168.1.10", 123456
        self.store.save_config(config, self.store.revision())
        public = self.store.public_config()
        self.assertNotIn("123456", str(public))
        self.assertTrue(public["devices"][0]["local_token_set"])
        public["devices"][0]["name"] = "Renamed"
        self.store.save_config(Configuration.model_validate(public), self.store.revision())
        self.assertEqual(self.store.config().devices[0].local_token, 123456)
        public = self.store.public_config()
        public["devices"][0]["host"] = "192.168.1.11"
        self.store.save_config(Configuration.model_validate(public), self.store.revision())
        self.assertIsNone(self.store.config().devices[0].local_token)

    def test_conflicting_save_does_not_replace_configuration(self):
        stale = self.store.revision()
        changed = self.store.config()
        changed.timezone = "UTC"
        self.store.save_config(changed, stale)
        with self.assertRaises(Conflict):
            self.store.save_config(Configuration(), stale)
        self.assertEqual(self.store.config().timezone, "UTC")

    def test_public_config_and_revision_come_from_one_snapshot(self):
        initial = self.store.get("config")
        changed = self.store.config()
        changed.timezone = "UTC"
        saved_revision = self.store.save_config(changed, self.store.revision())
        self.assertEqual(saved_revision, self.store.revision())
        with patch.object(self.store, "get", side_effect=[initial, changed.model_dump()]) as get:
            snapshot = self.store.public_state()
        get.assert_called_once_with("config")
        self.assertEqual(snapshot["config"]["timezone"], "Europe/Lisbon")
        self.assertNotEqual(snapshot["revision"], saved_revision)

    def test_persistent_schedules_coalesce_overdue_updates_and_do_not_duplicate(self):
        config = self.store.config()
        device = config.devices[0]
        device.host, device.local_token = "192.168.1.10", 123456
        device.screens[0] = Screen(kind="clock", enabled=True, refresh_seconds=60)
        self.store.save_config(config, self.store.revision(), now=1000)
        reopened = Store(self.store.path)
        reopened.schedule_due(10000)
        self.assertEqual(len(reopened.jobs()), 1)
        reopened.schedule_due(20000)
        self.assertEqual(len(reopened.jobs()), 1)
        job = reopened.claim()
        reopened.finish(job["id"], "acknowledged", "test")
        reopened.schedule_due(20000)
        self.assertEqual(len(reopened.jobs()), 2)

    def test_restart_never_replays_uncertain_manual_writes(self):
        key = self.store.enqueue("send", self.store.config().devices[0].id, 0)
        self.store.claim()
        self.store.recover()
        self.assertIsNone(self.store.claim())
        job = self.store.jobs()[0]
        self.assertEqual(job["id"], key)
        self.assertEqual(job["status"], "interrupted")

    def test_schedule_status_tracks_only_automatic_runs_and_survives_history_pruning(self):
        config = self.store.config()
        device = config.devices[0]
        device.host, device.local_token = "192.168.1.10", 123456
        device.screens[0] = Screen(kind="clock", enabled=True, refresh_seconds=60)
        self.store.save_config(config, self.store.revision(), now=1000)
        self.assertIsNone(self.store.schedules()[0]["last_run"])
        self.store.schedule_due(1000)
        self.assertEqual(self.store.schedules()[0]["last_run"]["status"], "pending")
        automatic = self.store.claim()
        self.assertEqual(self.store.schedules()[0]["last_run"]["status"], "running")
        self.store.finish(automatic["id"], "error", "Device command timeout")
        self.assertEqual(self.store.schedules()[0]["last_run"]["message"], "Device command timeout")

        with self.store.connect() as db:
            db.executemany(
                "INSERT INTO jobs (id,operation,device_id,panel,status,created,finished) "
                "VALUES (?,'send',?,0,'acknowledged',?,?)",
                [(f"manual-{index}", device.id, 10000000000 + index, 10000000000 + index)
                 for index in range(305)],
            )
        self.store.finish("manual-304", "acknowledged", "Manual update")
        self.assertNotIn(automatic["id"], [job["id"] for job in self.store.jobs()])
        reopened = Store(self.store.path)
        run = reopened.schedules()[0]["last_run"]
        self.assertEqual(run["id"], automatic["id"])
        self.assertEqual(run["status"], "error")
        self.assertIsNotNone(run["finished"])
        reopened.schedule_due(1060)
        updated = reopened.claim()
        reopened.finish(updated["id"], "acknowledged", "Playback command acknowledged")
        self.assertEqual(reopened.schedules()[0]["last_run"]["id"], updated["id"])
        self.assertEqual(reopened.schedules()[0]["last_run"]["status"], "acknowledged")
        with reopened.connect() as db:
            self.assertIsNone(db.execute(
                "SELECT id FROM jobs WHERE id=?", (automatic["id"],),
            ).fetchone())
        device.screens[0].enabled = False
        reopened.save_config(config, reopened.revision(), now=1100)
        self.assertEqual(reopened.schedules(), [])

    def test_schedule_results_are_isolated_by_device_and_screen_and_recover(self):
        config = Configuration(devices=[
            Device(host=f"192.168.1.{index}", local_token=123456,
                   screens=[Screen(kind="text", enabled=True), Screen(kind="clock", enabled=True),
                            Screen(), Screen(), Screen()])
            for index in (10, 11)
        ])
        self.store.save_config(config, self.store.revision(), now=1000)
        self.store.schedule_due(1000)
        first = self.store.claim()
        self.store.finish(first["id"], "demo", "Demo only")
        second = self.store.claim()
        self.store.finish(second["id"], "acknowledged", "Accepted")
        self.store.claim()
        self.store.recover()
        results = {
            (row["device_id"], row["panel"]): row["last_run"]["status"]
            for row in self.store.schedules()
        }
        self.assertEqual(results[(first["device_id"], first["panel"])], "demo")
        self.assertEqual(results[(second["device_id"], second["panel"])], "acknowledged")
        self.assertEqual(list(results.values()).count("interrupted"), 2)

    def test_time_change_schedule_stays_aligned_and_coalesces_missed_minutes(self):
        config = self.store.config()
        device = config.devices[0]
        device.host, device.local_token = "192.168.1.10", 123456
        screen = Screen(kind="clock", enabled=True, schedule="on_time_change")
        device.screens[0] = screen
        self.store.save_config(config, self.store.revision(), now=1000)
        self.assertEqual(self.store.schedules()[0]["next_due"], 1020)
        self.store.save_config(config, self.store.revision(), now=1005)
        reopened = Store(self.store.path)
        self.assertEqual(reopened.schedules()[0]["next_due"], 1020)
        reopened.schedule_due(1019.99)
        self.assertEqual(reopened.jobs(), [])
        reopened.schedule_due(1020.4)
        self.assertEqual(len(reopened.jobs()), 1)
        self.assertEqual(reopened.schedules()[0]["next_due"], 1080)
        job = reopened.claim()
        reopened.finish(job["id"], "demo", "test")
        reopened.schedule_due(1301)
        self.assertEqual(len(reopened.jobs()), 2)
        self.assertEqual(reopened.schedules()[0]["next_due"], 1320)
        screen.enabled = False
        reopened.save_config(config, reopened.revision(), now=1302)
        self.assertEqual(reopened.schedules(), [])

    def test_queue_pressure_does_not_starve_older_screens(self):
        config = Configuration(devices=[
            Device(host=f"192.168.1.{index}", local_token=123456,
                   screens=[Screen(kind="clock", enabled=True, refresh_seconds=10)
                            for _ in range(5)])
            for index in range(1, 8)
        ])
        self.store.save_config(config, self.store.revision(), now=1000)
        self.store.schedule_due(1000)
        initial = {(job["device_id"], job["panel"]) for job in self.store.jobs()}
        self.assertEqual(len(initial), 30)
        job = self.store.claim()
        self.store.finish(job["id"], "demo", "test")
        self.store.schedule_due(2000)
        later = {(job["device_id"], job["panel"]) for job in self.store.jobs()}
        self.assertEqual(len(later - initial), 1)

    def test_full_queue_defers_strava_without_blocking_device_jobs(self):
        strava = Mock()
        strava.status.return_value = {"connected": True}
        engine = Engine(self.store, self.root, "http://nas.lan:8081", strava)
        for _ in range(30):
            self.store.enqueue("send", self.store.config().devices[0].id, 0)
        engine.tick(1000)
        job = self.store.claim()
        self.assertIsNotNone(job)
        self.store.finish(job["id"], "demo", "test")
        engine.tick(1000)
        self.assertTrue(self.store.queue_status()[1])
        self.assertEqual(sum(job["operation"] == "strava" for job in self.store.jobs()), 1)

    def test_engine_publishes_gif_and_makes_one_targeted_url_command(self):
        media = self.root / "media"
        media.mkdir()
        config = self.store.config()
        device = config.devices[0]
        device.host, device.local_token = "192.168.1.10", 123456
        device.screens[2] = Screen(kind="clock")
        self.store.save_config(config, self.store.revision())
        engine = Engine(self.store, media, "http://nas.lan:8081", Strava(self.store, "http://localhost:8080"))
        key = self.store.enqueue("send", device.id, 2)
        with patch("timesgate.engine.play_gif") as send:
            engine.process(self.store.claim())
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1], 2)
        name = send.call_args.args[2].rsplit("/", 1)[1]
        with Image.open(media / name) as image:
            self.assertEqual(image.size, (128, 128))
        self.assertEqual(self.store.jobs()[0]["status"], "acknowledged")
        self.assertEqual(self.store.jobs()[0]["id"], key)

    def test_screen_send_spacing_starts_at_completion_even_after_timeout(self):
        config = self.store.config()
        device = config.devices[0]
        device.host, device.local_token = "192.168.1.10", 123456
        for panel in (1, 2):
            device.screens[panel] = Screen(kind="clock", enabled=True)
        self.store.save_config(config, self.store.revision(), now=1000)
        self.store.schedule_due(1000)
        self.store.enqueue("send", device.id, 2)
        engine = Engine(self.store, self.root, "http://nas.lan:8081", Mock())
        now = 100.0
        starts = []

        def wait(seconds):
            nonlocal now
            now += seconds
            return False

        def send(device, panel, url):
            nonlocal now
            starts.append(now)
            now += 7
            if len(starts) == 2:
                raise DeviceError("Device command timeout; effect unknown")

        with patch.object(engine, "content", return_value=("test.gif", True)), patch(
            "timesgate.engine.time.monotonic", side_effect=lambda: now,
        ), patch.object(engine.stopping, "wait", side_effect=wait) as pause, patch(
            "timesgate.engine.play_gif", side_effect=send,
        ) as play:
            for _ in range(3):
                engine.process(self.store.claim())
        self.assertEqual(starts, [100.0, 109.0, 118.0])
        self.assertEqual([call.args[0] for call in pause.call_args_list], [2.0, 2.0])
        self.assertEqual([call.args[1] for call in play.call_args_list], [1, 2, 2])
        self.assertEqual([job["status"] for job in reversed(self.store.jobs())],
                         ["acknowledged", "error", "acknowledged"])

    def test_screen_send_spacing_is_per_host_and_shutdown_cancels_waiting_send(self):
        config = Configuration(devices=[
            Device(host=f"192.168.1.{index}", local_token=123456,
                   screens=[Screen(kind="clock"), Screen(), Screen(), Screen(), Screen()])
            for index in (10, 11)
        ])
        self.store.save_config(config, self.store.revision())
        first, second = config.devices
        for device in (first, second, first):
            self.store.enqueue("send", device.id, 0)
        engine = Engine(self.store, self.root, "http://nas.lan:8081", Mock())
        with patch.object(engine, "content", return_value=("test.gif", True)), patch(
            "timesgate.engine.time.monotonic", return_value=100.0,
        ), patch.object(engine.stopping, "wait", return_value=True) as pause, patch(
            "timesgate.engine.play_gif",
        ) as send:
            for _ in range(3):
                engine.process(self.store.claim())
        self.assertEqual([call.args[0].host for call in send.call_args_list],
                         [first.host, second.host])
        pause.assert_called_once_with(2.0)
        self.assertEqual(self.store.jobs()[0]["status"], "cancelled")
        self.assertIn("stopping", self.store.jobs()[0]["message"])


class HybridTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite")
        self.config = self.store.config()
        self.config.timezone = "Asia/Kathmandu"
        self.device = self.config.devices[0]
        self.device.host, self.device.local_token = "192.168.1.10", 123456
        self.device.screens[2] = Screen(kind="clock", title="TEST", accent="hot-pink")
        self.engine = Engine(self.store, self.root, "http://nas.lan:8081", Mock())
        pause = patch.object(self.engine.stopping, "wait", return_value=False)
        pause.start()
        self.addCleanup(pause.stop)
        post = patch("timesgate.protocol.requests.post")
        self.post = post.start()
        self.addCleanup(post.stop)
        self.post.return_value.status_code = 200
        self.post.return_value.json.return_value = {"ReturnCode": 0}

    def save(self, now=1000):
        self.store.save_config(self.config, self.store.revision(), now=now)

    def send(self):
        key = self.store.enqueue("send", self.device.id, 2)
        self.engine.process(self.store.claim())
        return next(job for job in self.store.jobs() if job["id"] == key)

    def test_jpeg_widgets_send_real_image_bytes_without_media_server_or_published_files(self):
        self.device.widget_transport = "jpeg"
        self.engine.media_base_url = ""
        screen_values = [
            {"kind": "clock"}, {"kind": "text"}, {"kind": "strava"},
            {"kind": "weather", "latitude": 1, "longitude": 2},
            {"kind": "countdown", "target": "2030-01-01T00:00"},
            {"kind": "rss", "url": "https://example.com/feed"}, {"kind": "custom"},
        ]
        ids = set()
        for values in screen_values:
            with self.subTest(kind=values["kind"]):
                self.device.screens[2] = Screen(**values)
                self.save()
                with patch.object(self.engine, "image", return_value=Image.new(
                    "RGB", (128, 128), "#55e6b0",
                )) as render:
                    job = self.send()
                self.assertEqual(job["status"], "acknowledged")
                self.assertIn("Direct JPEG", job["message"])
                render.assert_called_once_with(self.device.screens[2], self.config.timezone)
                self.assertEqual(self.post.call_args.args[0], "http://192.168.1.10:9000/divoom_api")
                data = self.post.call_args.kwargs["json"]
                self.assertEqual(data["Command"], "Draw/SendHttpGif")
                self.assertEqual(data["LcdArray"], [0, 0, 1, 0, 0])
                self.assertEqual((data["PicNum"], data["PicOffset"], data["PicWidth"]), (1, 0, 128))
                self.assertEqual(data["LocalToken"], 123456)
                ids.add(data["PicID"])
                with Image.open(io.BytesIO(base64.b64decode(data["PicData"], validate=True))) as image:
                    self.assertEqual((image.format, image.size), ("JPEG", (128, 128)))
                self.assertNotIn("FileName", data)
        self.assertEqual(len(ids), len(screen_values))
        self.assertEqual(list(self.root.glob("*.gif")), [])

    def test_uploaded_animation_uses_unchanged_url_and_bytes_with_both_options_enabled(self):
        self.device.widget_transport, self.device.clock_renderer = "jpeg", "native"
        key = "a" * 32
        animation = encode_full_frames(
            [Image.new("RGB", (128, 128), color) for color in ("red", "blue")], [90, 130],
        )
        (self.root / f"{key}.gif").write_bytes(animation)
        self.store.add_media(key, "Animated test", 2, 220)
        self.device.screens[2] = Screen(kind="media", media_id=key)
        self.save()
        job = self.send()
        self.assertEqual(job["status"], "acknowledged")
        data = self.post.call_args.kwargs["json"]
        self.assertEqual(data["Command"], "Device/PlayGif")
        self.assertEqual(data["FileName"], [f"http://nas.lan:8081/{key}.gif"])
        self.assertEqual(data["LcdArray"], [0, 0, 1, 0, 0])
        self.assertEqual((self.root / f"{key}.gif").read_bytes(), animation)
        self.assertEqual(self.post.call_count, 1)

    def test_native_clock_overrides_jpeg_and_sends_sync_then_screen_specific_elements(self):
        self.device.widget_transport, self.device.clock_renderer = "jpeg", "native"
        for hardware in (400, 402):
            with self.subTest(hardware=hardware):
                self.device.hardware = hardware
                self.save()
                self.post.reset_mock()
                with patch("timesgate.engine.time.time", return_value=1800000000):
                    job = self.send()
                self.assertEqual(job["status"], "acknowledged")
                self.assertIn("Native clock", job["message"])
                payloads = [call.kwargs["json"] for call in self.post.call_args_list]
                self.assertEqual([body["Command"] for body in payloads], [
                    "Device/SetUTC", "Sys/TimeZone", "Draw/SendHttpItemList",
                ])
                self.assertEqual(payloads[0]["Utc"], 1800000000)
                self.assertEqual(payloads[1]["TimeZoneValue"], "GMT+5.75")
                clock = payloads[2]
                self.assertEqual((clock["LcdIndex"], clock["NewFlag"]), (2, 1))
                self.assertEqual([item["type"] for item in clock["ItemList"]], [22, 5, 12])
                self.assertEqual(clock["ItemList"][0]["TextString"], "TEST")
                self.assertEqual(clock["ItemList"][0]["color"], "#FF6FAE")
                self.assertTrue(all(
                    item["align"] == (2 if hardware == 400 else 3)
                    and item["x"] + item["TextWidth"] <= 128
                    and item["y"] + item["Textheight"] <= 128
                    for item in clock["ItemList"]
                ))
                background = self.root / clock["BackgroudGif"].rsplit("/", 1)[1]
                with Image.open(background) as image:
                    self.assertEqual((image.format, image.size, image.n_frames), ("GIF", (128, 128), 1))
                    self.assertEqual(image.convert("RGB").getpixel((64, 64)), (8, 18, 30))

    def test_native_partial_failure_stops_and_never_falls_back(self):
        self.device.clock_renderer, self.device.widget_transport = "native", "jpeg"
        self.save()
        self.post.side_effect = [self.post.return_value, requests.Timeout()]
        with self.assertLogs("timesgate.engine", level="ERROR"):
            job = self.send()
        self.assertEqual(job["status"], "error")
        self.assertIn("1/3 commands acknowledged", job["message"])
        self.assertEqual(self.post.call_count, 2)
        self.assertGreater(self.engine.screen_send_ready[self.device.host], 0)

    def test_selected_background_reaches_jpeg_and_native_clock_transports(self):
        self.device.widget_transport = "jpeg"
        for background in ("#000000", "#0c2118"):
            for renderer in ("image", "native"):
                with self.subTest(background=background, renderer=renderer):
                    self.device.clock_renderer = renderer
                    self.device.screens[2].background = background
                    self.save()
                    self.post.reset_mock()
                    self.assertEqual(self.send()["status"], "acknowledged")
                    payload = self.post.call_args.kwargs["json"]
                    if renderer == "native":
                        filename = payload["BackgroudGif"].rsplit("/", 1)[1]
                        source = self.root / filename
                    else:
                        source = io.BytesIO(base64.b64decode(payload["PicData"], validate=True))
                    with Image.open(source) as image:
                        actual = image.convert("RGB").getpixel((0, 127))
                    expected = tuple(bytes.fromhex(background[1:]))
                    tolerance = 0 if renderer == "native" else 3
                    self.assertTrue(all(
                        abs(left - right) <= tolerance for left, right in zip(actual, expected)
                    ), (actual, expected))

    def test_jpeg_rejection_does_not_fall_back_to_url_playback(self):
        self.device.widget_transport = "jpeg"
        self.save()
        self.post.return_value.json.return_value = {"ReturnCode": 1}
        with self.assertLogs("timesgate.engine", level="ERROR"):
            job = self.send()
        self.assertEqual(job["status"], "error")
        self.assertEqual(self.post.call_count, 1)
        self.assertEqual(self.post.call_args.kwargs["json"]["Command"], "Draw/SendHttpGif")

    def test_native_requires_media_origin_before_any_time_sync_write(self):
        self.device.clock_renderer = "native"
        self.engine.media_base_url = ""
        self.save()
        with self.assertLogs("timesgate.engine", level="ERROR"):
            job = self.send()
        self.assertEqual(job["status"], "error")
        self.assertIn("MEDIA_BASE_URL", job["message"])
        self.post.assert_not_called()

    def test_native_and_jpeg_share_spacing_after_the_last_command_completes(self):
        now = 100.0
        starts = []

        def wait(seconds):
            nonlocal now
            now += seconds
            return False

        def post(*args, **kwargs):
            nonlocal now
            starts.append(now)
            now += 3
            return self.post.return_value

        self.post.side_effect = post
        self.device.clock_renderer = "native"
        self.save()
        with patch("timesgate.engine.time.monotonic", side_effect=lambda: now), patch.object(
            self.engine.stopping, "wait", side_effect=wait,
        ) as pause:
            self.assertEqual(self.send()["status"], "acknowledged")
            self.device.clock_renderer, self.device.widget_transport = "image", "jpeg"
            self.save()
            self.assertEqual(self.send()["status"], "acknowledged")
        self.assertEqual(starts, [100.0, 103.0, 106.0, 111.0])
        pause.assert_called_once_with(2.0)

    def test_new_transports_respect_sleep_pause_and_demo_without_writes(self):
        for renderer in ("image", "native"):
            self.device.clock_renderer, self.device.widget_transport = renderer, "jpeg"
            self.save()
            for guard in ("sleeping", "paused"):
                with self.subTest(renderer=renderer, guard=guard):
                    self.store.update_controls(self.device, **{"sleeping": False, "paused": False, guard: True})
                    self.assertEqual(self.send()["status"], "cancelled")
            self.store.update_controls(self.device, sleeping=False, paused=False)
            self.engine.demo = True
            self.assertEqual(self.send()["status"], "demo")
        self.post.assert_not_called()

    def test_native_schedule_is_immediate_then_hourly_and_image_schedule_is_preserved(self):
        screen = self.device.screens[2]
        screen.enabled, screen.schedule = True, "on_time_change"
        self.save()
        self.assertEqual(self.store.schedules()[0]["next_due"], 1020)
        self.device.clock_renderer = "native"
        self.save(now=1005)
        self.assertEqual(self.store.schedules()[0]["next_due"], 1005)
        self.store.schedule_due(1005)
        self.assertEqual(self.store.schedules()[0]["next_due"], 4605)
        job = self.store.claim()
        self.store.finish(job["id"], "error", "Uncertain setup")
        self.store.schedule_due(1060)
        self.assertEqual(len(self.store.jobs()), 1)
        self.save(now=1070)
        reopened = Store(self.store.path)
        self.assertEqual(reopened.schedules()[0]["next_due"], 4605)
        self.assertEqual(reopened.config().devices[0].screens[2].schedule, "on_time_change")
        reopened.schedule_due(10000)
        self.assertEqual(reopened.schedules()[0]["next_due"], 13600)
        self.assertEqual(len(reopened.jobs()), 2)
        self.device.clock_renderer = "image"
        self.save(now=10001)
        self.assertEqual(self.store.schedules()[0]["next_due"], 10020)

    def test_native_schedule_holds_until_wake_and_transport_changes_do_not_reset_media(self):
        key = "a" * 32
        self.store.add_media(key, "Test", 1, 100)
        self.device.screens[1] = Screen(kind="media", enabled=True, media_id=key)
        self.device.screens[2].enabled = True
        self.save()
        self.store.schedule_due(1000)
        while job := self.store.claim():
            self.store.finish(job["id"], "acknowledged", "Test")
        self.device.clock_renderer, self.device.widget_transport = "native", "jpeg"
        self.save(now=1010)
        schedules = {row["panel"]: row for row in self.store.schedules()}
        self.assertEqual(schedules[1]["next_due"], 1060)
        self.assertEqual(schedules[2]["next_due"], 1010)
        self.store.update_controls(self.device, sleeping=True)
        self.store.schedule_due(2000)
        self.assertIsNone(self.store.claim())
        with patch("timesgate.store.time.time", return_value=2000):
            self.store.update_controls(self.device, sleeping=False)
        self.store.schedule_due(2000)
        self.assertIsNotNone(self.store.claim())


class ProtocolTests(unittest.TestCase):
    def test_hardware_routes_and_token_never_goes_to_media_host(self):
        for hardware, endpoint in ((402, "http://192.168.1.10:9000/divoom_api"),
                                   (400, "http://192.168.1.10/post")):
            session = Mock()
            session.post.return_value.status_code = 200
            session.post.return_value.json.return_value = {"ReturnCode": 0}
            device = Device(host="192.168.1.10", hardware=hardware, local_token=123456)
            play_gif(device, 3, "http://nas.lan:8081/test.gif", session)
            self.assertEqual(session.post.call_args.args[0], endpoint)
            data = session.post.call_args.kwargs["json"]
            self.assertEqual(data["LcdArray"], [0, 0, 0, 1, 0])
            self.assertEqual(data["LocalToken"], 123456)
            self.assertNotIn("123456", data["FileName"][0])
            self.assertEqual(session.post.call_args.kwargs["timeout"], (5, 45))

    def test_rejections_do_not_retry_or_expose_credential_echoes(self):
        for body in ({}, {"ReturnCode": False}, {"ReturnCode": 0, "error_code": 1},
                     {"ReturnCode": 1, "ReturnMessage": "command timeout", "LocalToken": 123456}):
            session = Mock()
            session.post.return_value.status_code = 200
            session.post.return_value.json.return_value = body
            with self.assertRaises(DeviceError) as error:
                play_gif(Device(host="192.168.1.10", local_token=123456),
                         0, "http://nas.lan/file.gif", session)
            self.assertNotIn("123456", str(error.exception))
            session.post.assert_called_once()


class ScheduleTests(unittest.TestCase):
    def test_time_change_schedule_uses_the_next_local_minute(self):
        screen = Screen(kind="clock", schedule="on_time_change", refresh_seconds=300)
        for timezone in ("UTC", "Asia/Kathmandu"):
            zone = ZoneInfo(timezone)
            for second, microsecond in ((0, 0), (23, 500000), (59, 999999)):
                with self.subTest(timezone=timezone, second=second, microsecond=microsecond):
                    now = datetime(2026, 11, 18, 12, 34, second, microsecond, tzinfo=zone)
                    due = next_due(screen, timezone, now.timestamp())
                    self.assertEqual(
                        datetime.fromtimestamp(due, zone),
                        datetime(2026, 11, 18, 12, 35, tzinfo=zone),
                    )

    def test_time_change_schedule_handles_both_dst_transitions(self):
        timezone = "Europe/Lisbon"
        zone = ZoneInfo(timezone)
        screen = Screen(kind="clock", schedule="on_time_change")
        for month, day, hour, expected_hour, expected_fold in (
            (3, 29, 0, 2, 0),
            (10, 25, 1, 1, 1),
        ):
            with self.subTest(month=month):
                now = datetime(2026, month, day, hour, 59, 59, tzinfo=zone)
                due = next_due(screen, timezone, now.timestamp())
                local = datetime.fromtimestamp(due, zone)
                self.assertEqual(due, now.timestamp() + 1)
                self.assertEqual((local.hour, local.minute, local.second, local.fold),
                                 (expected_hour, 0, 0, expected_fold))

    def test_time_change_schedule_is_clock_only_even_when_disabled(self):
        for kind in ("unmanaged", "media", "text", "strava", "weather", "countdown", "rss", "custom"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "only available for clock"):
                    Screen(kind=kind, schedule="on_time_change")

    def test_daily_schedule_obeys_timezone_across_dst(self):
        zone = ZoneInfo("Europe/Lisbon")
        now = datetime(2026, 3, 28, 12, 0, tzinfo=zone).timestamp()
        due = next_due(Screen(schedule="daily", daily_time="08:30"), "Europe/Lisbon", now)
        self.assertEqual(datetime.fromtimestamp(due, zone).strftime("%Y-%m-%d %H:%M"),
                         "2026-03-29 08:30")
        self.assertEqual(due - now, 19.5 * 3600)

    def test_daily_schedule_handles_nonexistent_and_repeated_local_times(self):
        zone = ZoneInfo("Europe/Lisbon")
        screen = Screen(schedule="daily", daily_time="01:30")
        now = datetime(2026, 3, 29, 0, 0, tzinfo=zone).timestamp()
        due = next_due(screen, "Europe/Lisbon", now)
        self.assertEqual(datetime.fromtimestamp(due, zone).strftime("%Y-%m-%d %H:%M"),
                         "2026-03-29 02:30")
        now = datetime(2026, 10, 25, 0, 0, tzinfo=zone).timestamp()
        first = next_due(screen, "Europe/Lisbon", now)
        self.assertEqual(datetime.fromtimestamp(first, zone).fold, 0)
        following = next_due(screen, "Europe/Lisbon", first + 1)
        self.assertEqual(datetime.fromtimestamp(following, zone).strftime("%Y-%m-%d %H:%M"),
                         "2026-10-26 01:30")
