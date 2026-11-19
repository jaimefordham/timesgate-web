from contextlib import closing
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from timesgate.engine import Engine
from timesgate.lighting import lighting_payload, target_signature
from timesgate.models import LightingSettings
from timesgate.protocol import DeviceError, device_command
from timesgate.store import Store


class LightingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state.sqlite")
        config = self.store.config()
        self.device = config.devices[0]
        self.device.host, self.device.local_token = "192.168.1.10", 123456
        self.store.save_config(config, self.store.revision())
        self.settings = LightingSettings(color="#ABCDEF", brightness=73, zone=2, effect=11)

    def queue_lighting(self):
        return self.store.enqueue("lighting", self.device.id, payload={
            "settings": self.settings.model_dump(), "target": target_signature(self.device),
        })

    def test_zone_addressing_and_transport_preserve_screen_state_and_redact_errors(self):
        for zone in range(3):
            settings = self.settings.model_copy(update={"zone": zone})
            payload = lighting_payload(settings)
            self.assertEqual(payload, {
                "Command": "Channel/SetRGBInfo", "Color": "#abcdef", "Brightness": 73,
                "SelectLightIndex": zone,
                "LightList": [{"SelectEffect": 11 if index == zone else 0} for index in range(3)],
                "OnOff": 1, "ColorCycle": 0, "KeyOnOff": 1,
            })
            for hardware, endpoint in (
                (402, "http://192.168.1.10:9000/divoom_api"),
                (400, "http://192.168.1.10/post"),
            ):
                session = Mock()
                session.post.return_value.status_code = 200
                session.post.return_value.json.return_value = {"ReturnCode": 0}
                device_command(self.device.model_copy(update={"hardware": hardware}), payload, session)
                self.assertEqual(session.post.call_args.args[0], endpoint)
                self.assertEqual(session.post.call_args.kwargs["json"]["LocalToken"], 123456)
                self.assertNotIn("LocalToken", payload)
        session.post.return_value.json.return_value = {
            "ReturnCode": 1, "ReturnMessage": "command timeout", "LocalToken": 123456,
        }
        session.post.reset_mock()
        with self.assertRaisesRegex(DeviceError, "effect unknown") as error:
            device_command(self.device, lighting_payload(self.settings), session)
        session.post.assert_called_once()
        self.assertNotIn("123456", str(error.exception))

    def test_only_acknowledged_lighting_is_persisted_without_media_or_schedule_changes(self):
        original_config = self.store.public_state()
        engine = Engine(self.store, self.root, "", Mock())
        key = self.queue_lighting()
        with patch("timesgate.engine.device_command", side_effect=DeviceError("Rejected")) as send:
            engine.process(self.store.claim())
        send.assert_called_once()
        self.assertEqual(self.store.jobs()[0]["status"], "error")
        self.assertEqual(self.store.lighting(), {})
        self.queue_lighting()
        with patch("timesgate.engine.device_command") as send:
            engine.process(self.store.claim())
        send.assert_called_once_with(self.device, lighting_payload(self.settings))
        self.assertEqual(self.store.jobs()[0]["status"], "acknowledged")
        saved = Store(self.store.path).lighting()[self.device.id]
        self.assertEqual(saved["settings"], self.settings.model_dump())
        self.assertGreater(saved["applied_at"], 0)
        self.assertEqual(self.store.public_state(), original_config)
        self.assertEqual(self.store.schedules(), [])
        self.assertNotIn("payload", self.store.jobs()[0])
        self.assertNotIn("123456", str(self.store.lighting()))
        self.assertNotEqual(self.store.jobs()[0]["id"], key)

    def test_demo_changed_targets_and_restart_do_not_send_or_restore_lighting(self):
        self.queue_lighting()
        with patch("timesgate.engine.device_command") as send:
            Engine(self.store, self.root, "", Mock(), demo=True).process(self.store.claim())
        send.assert_not_called()
        self.assertEqual(self.store.lighting(), {})
        self.assertEqual(self.store.jobs()[0]["status"], "demo")
        self.store.remember_lighting(self.device, self.settings)
        self.queue_lighting()
        config = self.store.config()
        config.devices[0].host = "192.168.1.11"
        self.store.save_config(config, self.store.revision())
        self.assertEqual(self.store.lighting(), {})
        with patch("timesgate.engine.device_command") as send:
            Engine(self.store, self.root, "", Mock()).process(self.store.claim())
        send.assert_not_called()
        self.assertIn("connection changed", self.store.jobs()[0]["message"])
        self.queue_lighting()
        self.store.recover()
        reopened = Store(self.store.path)
        self.assertIsNone(reopened.claim())
        self.assertEqual(reopened.jobs()[0]["status"], "interrupted")

    def test_invalid_lighting_and_legacy_job_migration(self):
        for key, invalid in (
            ("color", "red"), ("color", "#fff"), ("brightness", True), ("brightness", -1),
            ("brightness", 101), ("zone", "2"), ("zone", 3), ("effect", 12),
            ("effect", False), ("on", 1), ("cycle", "false"), ("keys", None),
        ):
            with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                LightingSettings.model_validate({**self.settings.model_dump(), key: invalid})
        path = self.root / "legacy.sqlite"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY, operation TEXT NOT NULL, device_id TEXT,
                    panel INTEGER, automatic INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL, created REAL NOT NULL, finished REAL,
                    message TEXT NOT NULL DEFAULT ''
                )
            """)
            db.execute("INSERT INTO jobs (id,operation,status,created) VALUES ('old','send','pending',1)")
        migrated = Store(path)
        self.assertEqual(migrated.claim()["payload"], "{}")
        self.assertEqual(Store(path).jobs()[0]["id"], "old")

    def test_startup_restore_is_opt_in_once_per_start_and_keeps_screen_config(self):
        with self.assertRaisesRegex(ValueError, "Apply lighting successfully"):
            self.store.configure_lighting_restore(self.device.id, True)
        self.store.remember_lighting(self.device, self.settings)
        self.store.queue_startup_lighting()
        self.assertEqual(self.store.jobs(), [])
        before = self.store.public_state()
        self.store.configure_lighting_restore(self.device.id, True)
        self.assertEqual(self.store.jobs(), [])
        self.store.queue_startup_lighting()
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(self.store.jobs()[0]["automatic"], 1)
        engine = Engine(self.store, self.root, "", Mock(), demo=True)
        for now in range(1000, 1005):
            engine.tick(now)
        self.assertEqual(len(self.store.jobs()), 1)
        engine.demo = False
        with patch("timesgate.engine.device_command") as send:
            engine.process(self.store.claim())
        send.assert_called_once_with(self.device, lighting_payload(self.settings))
        self.assertEqual(self.store.public_state(), before)
        self.assertEqual(self.store.lighting_restoration()[0]["last_run"]["status"], "acknowledged")
        self.assertTrue(self.store.lighting()[self.device.id]["restore_on_startup"])
        reopened = Store(self.store.path)
        reopened.recover()
        reopened.queue_startup_lighting()
        self.assertEqual(sum(job["status"] == "pending" for job in reopened.jobs()), 1)

    def test_failed_or_interrupted_restore_is_held_until_a_fresh_manual_acknowledgement(self):
        for failure in ("error", "interrupted"):
            with self.subTest(failure=failure):
                self.store.remember_lighting(self.device, self.settings)
                self.store.configure_lighting_restore(self.device.id, True)
                self.store.queue_startup_lighting()
                job = self.store.claim()
                if failure == "error":
                    engine = Engine(self.store, self.root, "", Mock())
                    with patch("timesgate.engine.device_command", side_effect=DeviceError("Effect unknown")) as send:
                        engine.process(job)
                    send.assert_called_once()
                else:
                    self.store.recover()
                self.assertTrue(self.store.lighting_restoration()[0]["blocked"])
                self.store.configure_lighting_restore(self.device.id, False)
                self.store.configure_lighting_restore(self.device.id, True)
                self.store.queue_startup_lighting()
                self.assertIsNone(self.store.claim())
                self.queue_lighting()
                with patch("timesgate.engine.device_command"):
                    Engine(self.store, self.root, "", Mock()).process(self.store.claim())
                status = self.store.lighting_restoration()[0]
                self.assertFalse(status["blocked"])
                self.assertIsNone(status["last_run"])
                self.store.queue_startup_lighting()
                self.assertIsNotNone(self.store.claim())
                self.store.recover()

    def test_manual_apply_and_disabling_restoration_cancel_waiting_or_superseded_work(self):
        self.store.remember_lighting(self.device, self.settings)
        self.store.configure_lighting_restore(self.device.id, True)
        engine = Engine(self.store, self.root, "", Mock())
        self.store.queue_startup_lighting()
        restore_id = self.store.jobs()[0]["id"]
        manual_id = self.queue_lighting()
        self.assertEqual(next(job for job in self.store.jobs() if job["id"] == restore_id)["status"],
                         "cancelled")
        self.assertEqual(self.store.claim()["id"], manual_id)
        self.store.finish(manual_id, "error", "Not sent in this test")
        self.store.queue_startup_lighting()
        claimed = self.store.claim()
        self.queue_lighting()
        with patch("timesgate.engine.device_command") as send:
            engine.process(claimed)
        send.assert_not_called()
        self.assertEqual(next(job for job in self.store.jobs() if job["id"] == claimed["id"])["status"],
                         "cancelled")
        with patch("timesgate.engine.device_command"):
            engine.process(self.store.claim())
        self.store.queue_startup_lighting()
        self.store.configure_lighting_restore(self.device.id, False)
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.lighting_restoration()[0]["last_run"]["status"], "cancelled")

    def test_restoration_rag_survives_history_cleanup_and_connection_changes_require_approval(self):
        self.store.remember_lighting(self.device, self.settings)
        self.store.configure_lighting_restore(self.device.id, True)
        self.store.queue_startup_lighting()
        automatic = self.store.claim()
        with patch("timesgate.engine.device_command"):
            Engine(self.store, self.root, "", Mock()).process(automatic)
        with self.store.connect() as db:
            db.executemany(
                "INSERT INTO jobs (id,operation,status,created) VALUES (?,'strava','done',?)",
                [(f"history-{index}", 10000000000 + index) for index in range(305)],
            )
        self.store.finish("history-304", "done", "Prune history")
        self.assertNotIn(automatic["id"], [job["id"] for job in self.store.jobs()])
        reopened = Store(self.store.path)
        self.assertEqual(reopened.lighting_restoration()[0]["last_run"]["id"], automatic["id"])
        self.assertNotIn("payload", reopened.lighting_restoration()[0]["last_run"])
        reopened.queue_startup_lighting()
        queued = reopened.claim()
        config = reopened.config()
        config.devices[0].host = "192.168.1.11"
        reopened.save_config(config, reopened.revision())
        self.assertFalse(reopened.lighting_restoration()[0]["eligible"])
        self.assertFalse(reopened.lighting_restoration()[0]["enabled"])
        with patch("timesgate.engine.device_command") as send:
            Engine(reopened, self.root, "", Mock()).process(queued)
        send.assert_not_called()
        self.assertIsNone(reopened.lighting_restoration()[0]["last_run"])


if __name__ == "__main__":
    unittest.main()
