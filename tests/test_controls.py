from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from timesgate.controls import control_commands, night_next, night_phase
from timesgate.engine import Engine
from timesgate.lighting import target_signature
from timesgate.models import ControlRequest, LightingPreset, LightingSettings, NightMode
from timesgate.protocol import DeviceError
from timesgate.store import Store


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite")
        config = self.store.config()
        config.timezone = "UTC"
        self.device = config.devices[0]
        self.device.host, self.device.local_token = "192.168.1.10", 123456
        self.device.screens[0].kind = "clock"
        self.device.screens[1].kind = "text"
        self.store.save_config(config, self.store.revision())
        self.engine = Engine(self.store, self.root, "http://192.168.1.20:8081", Mock(), demo=False)

    def queue(self, action, **values):
        request = ControlRequest(
            device_id=self.device.id, control={"action": action, **values}, confirmed=True,
        )
        return self.store.enqueue("control", self.device.id, payload={
            "request": request.model_dump(), "target": target_signature(self.device),
        })

    def process(self, action, **values):
        key = self.queue(action, **values)
        self.engine.process(self.store.claim())
        return next(job for job in self.store.jobs() if job["id"] == key)

    def test_strict_controls_confirmation_and_exact_native_commands(self):
        for control in (
            {"action": "brightness", "value": True}, {"action": "brightness", "value": 101},
            {"action": "power", "on": "yes"}, {"action": "timer", "seconds": 60},
            {"action": "stopwatch", "state": "restart"}, {"action": "scoreboard", "red": -1, "blue": 0},
            {"action": "noise", "on": 1}, {"action": "raw", "Command": "anything"},
            {"action": "buzzer", "PlayTotalTime": 100000},
        ):
            with self.subTest(control=control), self.assertRaises(ValidationError):
                ControlRequest(device_id=self.device.id, control=control, confirmed=True)
        for action in ("reboot", "timer", "stopwatch", "scoreboard", "noise"):
            with self.subTest(action=action), self.assertRaises(ValidationError):
                ControlRequest(device_id=self.device.id, control={"action": action})
        with patch("timesgate.engine.device_command", return_value={"ReturnCode": 0}) as send:
            job = self.process("preferences", hour24=False, celsius=True, mirror=True)
            self.assertEqual(job["status"], "acknowledged")
            self.assertEqual([call.args[1] for call in send.call_args_list], [
                {"Command": "Device/SetTime24Flag", "Mode": 0},
                {"Command": "Device/SetDisTempMode", "Mode": 0},
                {"Command": "Device/SetMirrorMode", "Mode": 1},
            ])
        request = ControlRequest(device_id=self.device.id, control={"action": "sync_time"})
        now = datetime(2026, 7, 1, tzinfo=ZoneInfo("UTC")).timestamp()
        commands = control_commands(request, self.device, "Asia/Kathmandu", now)
        self.assertEqual(commands[0], {"Command": "Device/SetUTC", "Utc": int(now)})
        self.assertEqual(commands[1]["TimeZoneValue"], "GMT+5.75")
        with patch("timesgate.engine.device_command", side_effect=[{}, DeviceError("Effect unknown")]) as send:
            job = self.process("preferences", hour24=True, celsius=False, mirror=False)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(job["status"], "error")
        self.assertIn("1/3 commands acknowledged", job["message"])
        self.assertIsNone(self.store.control_state(self.device)["preferences"])

    def test_uncertain_sleep_blocks_sends_across_restart_and_wake_resumes_schedules(self):
        config = self.store.config()
        config.devices[0].screens[0].enabled = True
        self.store.save_config(config, self.store.revision(), now=100)
        with patch("timesgate.engine.device_command", side_effect=DeviceError("Effect unknown")):
            job = self.process("power", on=False)
        self.assertEqual(job["status"], "error")
        self.assertTrue(Store(self.store.path).control_state(self.device)["sleeping"])
        self.store.schedule_due(200)
        self.assertEqual(len(self.store.jobs()), 1)
        self.store.enqueue("send", self.device.id, 0)
        with patch("timesgate.engine.play_gif") as send:
            self.engine.process(self.store.claim())
            send.assert_not_called()
        self.assertEqual(self.store.jobs()[0]["status"], "cancelled")
        with patch("timesgate.engine.device_command", return_value={}) as send:
            self.process("power", on=True)
        send.assert_called_once_with(self.store.config().devices[0], {"Command": "Channel/OnOffScreen", "OnOff": 1})
        self.assertFalse(self.store.control_state(self.device)["sleeping"])
        self.store.schedule_due(datetime.now().timestamp() + 1)
        self.assertEqual(self.store.claim()["operation"], "send")

    def test_native_tools_pause_before_uncertain_writes_and_resume_only_managed_screens(self):
        with patch("timesgate.engine.device_command", side_effect=DeviceError("Rejected")):
            self.process("timer", minutes=2, seconds=3)
        self.assertTrue(self.store.control_state(self.device)["paused"])
        job = self.process("resume")
        self.assertEqual(job["status"], "done")
        self.assertFalse(self.store.control_state(self.device)["paused"])
        pending = [self.store.claim(), self.store.claim()]
        self.assertEqual({job["panel"] for job in pending}, {0, 1})
        self.assertTrue(all(job["operation"] == "send" for job in pending))
        self.assertIsNone(self.store.claim())
        for job in pending:
            self.store.finish(job["id"], "cancelled", "Test cleanup")
        self.engine.demo = True
        with patch("timesgate.engine.device_command") as send:
            self.assertEqual(self.process("scoreboard", red=12, blue=9)["status"], "demo")
            self.assertTrue(self.store.control_state(self.device)["paused"])
            self.assertEqual(self.process("brightness", value=40)["status"], "demo")
            self.assertIsNone(self.store.control_state(self.device)["brightness"])
            send.assert_not_called()

    def test_remaining_native_controls_use_bounded_payloads_and_never_send_in_demo(self):
        cases = [
            ("timer", {"minutes": 2, "seconds": 9}, {
                "Command": "Tools/SetTimer", "Minute": 2, "Second": 9, "Status": 1,
            }),
            ("timer", {"running": False}, {
                "Command": "Tools/SetTimer", "Minute": 0, "Second": 0, "Status": 0,
            }),
            ("stopwatch", {"state": "reset"}, {"Command": "Tools/SetStopWatch", "Status": 2}),
            ("scoreboard", {"red": 21, "blue": 8}, {
                "Command": "Tools/SetScoreBoard", "RedScore": 21, "BlueScore": 8,
            }),
            ("noise", {"on": False}, {"Command": "Tools/SetNoiseStatus", "NoiseStatus": 0}),
            ("buzzer", {}, {
                "Command": "Device/PlayBuzzer", "ActiveTimeInCycle": 150,
                "OffTimeInCycle": 150, "PlayTotalTime": 600,
            }),
            ("reboot", {}, {"Command": "Device/SysReboot"}),
        ]
        for action, values, payload in cases:
            with self.subTest(action=action, values=values), patch(
                "timesgate.engine.device_command", return_value={},
            ) as send:
                self.assertEqual(self.process(action, **values)["status"], "acknowledged")
                send.assert_called_once_with(self.device, payload)
        self.engine.demo = True
        with patch("timesgate.engine.device_command") as send:
            self.process("sync_time")
            self.process("read_settings")
            send.assert_not_called()
        self.assertFalse(self.store.control_state(self.device)["diagnostic"]["available"])

    def test_resume_queue_pressure_preserves_pause_and_sleep_prevents_restore(self):
        self.store.update_controls(self.device, paused=True, sleeping=True)
        self.assertEqual(self.process("resume")["status"], "error")
        self.assertTrue(self.store.control_state(self.device)["paused"])
        self.store.update_controls(self.device, sleeping=False)
        resume_id = self.queue("resume")
        resume = self.store.claim()
        for _ in range(28):
            self.store.enqueue("strava")
        self.engine.process(resume)
        self.assertEqual(next(job for job in self.store.jobs() if job["id"] == resume_id)["status"], "error")
        self.assertTrue(self.store.control_state(self.device)["paused"])
        self.assertFalse(any(job["operation"] == "send" for job in self.store.jobs()))

    def test_diagnostics_never_expose_raw_responses_or_mark_device_offline(self):
        with patch("timesgate.engine.device_command", return_value={
            "ReturnCode": 0, "Brightness": 75, "LightSwitch": 1, "LocalToken": 123456,
            "Message": "Secret 123456", "Firmware": "Secret 123456",
        }):
            self.process("read_settings")
        state = self.store.control_state(self.device)
        self.assertEqual(state["diagnostic"]["values"], {"Brightness": 75, "LightSwitch": 1})
        self.assertNotIn("123456", str(state))
        with patch("timesgate.engine.device_command", side_effect=DeviceError("command timeout")):
            self.process("read_settings")
        state = self.store.control_state(self.device)
        self.assertFalse(state["diagnostic"]["available"])
        self.assertFalse(state["paused"])
        self.assertFalse(state["sleeping"])
        self.assertNotIn("offline", state)
        self.queue("brightness", value=20)
        config = self.store.config()
        config.devices[0].host = "192.168.1.11"
        self.store.save_config(config, self.store.revision())
        with patch("timesgate.engine.device_command") as send:
            self.engine.process(self.store.claim())
            send.assert_not_called()
        self.assertIsNone(self.store.controls()[self.device.id]["diagnostic"])

    def test_night_schedule_coalesces_missed_boundaries_and_never_replays_interrupted_jobs(self):
        start = datetime(2026, 11, 18, 20, tzinfo=ZoneInfo("UTC")).timestamp()
        late = start + 3 * 3600
        morning = start + 12 * 3600
        config = self.store.config()
        config.devices[0].night = NightMode(enabled=True, mode="sleep")
        self.store.save_config(config, self.store.revision(), now=start)
        self.store.schedule_due(late)
        self.store.schedule_due(late)
        self.assertEqual(len(self.store.jobs()), 1)
        with patch("timesgate.engine.time.time", return_value=late), patch(
            "timesgate.engine.device_command", return_value={},
        ) as send:
            self.engine.process(self.store.claim())
        self.assertEqual(send.call_args.args[1], {"Command": "Channel/OnOffScreen", "OnOff": 0})
        self.assertTrue(self.store.control_state(self.device)["sleeping"])
        self.store.schedule_due(morning)
        with patch("timesgate.engine.time.time", return_value=morning), patch(
            "timesgate.engine.device_command", return_value={},
        ) as send:
            self.engine.process(self.store.claim())
        self.assertEqual([call.args[1] for call in send.call_args_list], [
            {"Command": "Channel/SetBrightness", "Brightness": 70},
            {"Command": "Channel/OnOffScreen", "OnOff": 1},
        ])
        self.assertFalse(self.store.control_state(self.device)["sleeping"])
        self.store.schedule_due(morning + 86400)
        job = self.store.claim()
        self.assertEqual(job["panel"], 1)
        self.store.recover()
        self.store.schedule_due(morning + 86400)
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.device_schedules()[1]["last_run"]["status"], "interrupted")
        for index in range(305):
            self.store.finish(self.store.enqueue("strava"), "done", str(index))
        self.assertEqual(self.store.device_schedules()[0]["last_run"]["status"], "acknowledged")
        self.assertEqual(self.store.device_schedules()[1]["last_run"]["status"], "interrupted")

    def test_night_dst_windows_changed_schedules_and_manual_override(self):
        settings = NightMode(enabled=True, start="22:00", end="07:00")
        now = datetime(2026, 3, 28, 23, tzinfo=ZoneInfo("Europe/Lisbon")).timestamp()
        due = night_next(settings, "end", "Europe/Lisbon", now)
        self.assertEqual(due - now, 7 * 3600)
        self.assertEqual(night_phase(settings, "Europe/Lisbon", now), "start")
        self.assertEqual(night_phase(settings, "Europe/Lisbon", due), "end")
        repeated = NightMode(enabled=True, start="01:30", end="02:30")
        second_hour = datetime(2026, 10, 25, 1, 10, fold=1, tzinfo=ZoneInfo("Europe/Lisbon")).timestamp()
        self.assertEqual(night_phase(repeated, "Europe/Lisbon", second_hour), "start")
        skipped = NightMode(enabled=True, start="01:30", end="02:15")
        after_gap = datetime(2026, 3, 29, 2, 35, tzinfo=ZoneInfo("Europe/Lisbon")).timestamp()
        self.assertEqual(night_phase(skipped, "Europe/Lisbon", after_gap), "end")
        settings = NightMode(enabled=True, start="09:00", end="17:00")
        self.assertEqual(night_phase(settings, "Europe/Lisbon", due), "end")
        with self.assertRaises(ValidationError):
            NightMode(enabled=True, start="22:00", end="22:00")
        config = self.store.config()
        config.devices[0].night = NightMode(enabled=True)
        start = datetime(2026, 11, 18, 20, tzinfo=ZoneInfo("UTC")).timestamp()
        self.store.save_config(config, self.store.revision(), now=start)
        self.store.schedule_due(start + 3 * 3600)
        claimed = self.store.claim()
        config.devices[0].night.enabled = False
        self.store.save_config(config, self.store.revision(), now=start + 3 * 3600)
        with patch("timesgate.engine.device_command") as send:
            self.engine.process(claimed)
            send.assert_not_called()
        self.assertEqual(self.store.jobs()[0]["status"], "cancelled")
        config.devices[0].night.enabled = True
        self.store.save_config(config, self.store.revision(), now=start)
        self.store.schedule_due(start + 3 * 3600)
        self.queue("read_settings")
        self.assertEqual(next(job for job in self.store.jobs()
                              if job["operation"] == "device_schedule")["status"], "pending")
        self.store.finish(self.store.jobs()[0]["id"], "cancelled", "Test cleanup")
        self.queue("brightness", value=90)
        self.assertTrue(all(job["status"] == "cancelled" for job in self.store.jobs()
                            if job["operation"] == "device_schedule"))

    def test_full_lighting_presets_persist_validate_and_do_not_send(self):
        settings = LightingSettings(color="#ABCDEF", brightness=32, zone=1, effect=8, keys=False)
        preset = LightingPreset(name=" Evening ", settings=settings)
        self.store.save_lighting_preset(preset)
        saved = Store(self.store.path).get("lighting_presets")
        self.assertEqual(saved, [preset.model_dump()])
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.store.save_lighting_preset(LightingPreset(name="EVENING", settings=settings))
        with self.assertRaises(ValidationError):
            LightingPreset(name="  ", settings=settings)
        self.assertEqual(self.store.jobs(), [])
        self.assertEqual(self.store.lighting(), {})
        self.store.delete_lighting_preset(preset.id)
        self.assertEqual(self.store.get("lighting_presets"), [])
