import io
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from timesgate.app import create_app
from timesgate.engine import Engine
from timesgate.models import (
    Configuration, DesignLayer, Device, DeviceProfile, LightingSettings, Screen, WIDGET_ACCENTS,
)
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

    def test_running_version_reports_release_tag_or_explicit_development_build(self):
        with patch.dict(os.environ, {"APP_VERSION": "v1.2.3-rc.1"}):
            self.assertEqual(self.client.get("/api/state").json()["version"], "v1.2.3-rc.1")
        with patch.dict(os.environ):
            os.environ.pop("APP_VERSION", None)
            self.assertEqual(self.client.get("/api/state").json()["version"], "development")

    def test_device_transport_options_round_trip_without_sending_and_reject_invalid_values(self):
        state = self.client.get("/api/state").json()
        device = state["config"]["devices"][0]
        self.assertEqual((device["widget_transport"], device["clock_renderer"]), ("gif", "image"))
        device.update(widget_transport="jpeg", clock_renderer="native")
        response = self.client.put("/api/config", json={
            "config": state["config"], "revision": state["revision"],
        })
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.client.get("/api/state").json()
        device = saved["config"]["devices"][0]
        self.assertEqual((device["widget_transport"], device["clock_renderer"]), ("jpeg", "native"))
        self.assertEqual(saved["jobs"], [])
        self.assertEqual(saved["schedules"], [])
        for field, value in (("widget_transport", "png"), ("clock_renderer", "automatic")):
            with self.subTest(field=field):
                invalid = Configuration.model_validate(saved["config"]).model_dump()
                invalid["devices"][0][field] = value
                response = self.client.put("/api/config", json={
                    "config": invalid, "revision": saved["revision"],
                })
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.app.state.store.public_config(), saved["config"])

    def test_device_profiles_persist_drafts_without_changing_configuration_or_sending(self):
        store = self.app.state.store
        config = store.config()
        device = config.devices[0]
        device.host, device.local_token = "192.168.1.10", 123456
        config.devices.append(Device(name="Other", host="192.168.1.11", local_token=654321))
        store.save_config(config, store.revision())
        store.remember_lighting(device, LightingSettings(color="#36D9FF"))
        store.put("strava_tokens", {"access_token": "private-strava-token"})
        media_id = "a" * 32
        store.add_media(media_id, "Example", 1, 100)
        screens = [
            Screen(kind="clock", enabled=True, schedule="on_time_change", accent="hot-pink"),
            Screen(kind="media", media_id=media_id),
            Screen(kind="custom", elements=[DesignLayer(type="image", media_id=media_id)]),
            Screen(kind="text", text="Evening"),
            Screen(),
        ]
        night = device.night.model_copy(update={"enabled": True, "mode": "dim", "brightness": 5})
        profile = DeviceProfile(
            name=" Evening ", device_id=device.id, screens=screens, night=night,
            lighting=LightingSettings(color="#FFD166", brightness=12, cycle=True, keys=False),
        )
        before = store.public_state()
        before_lighting = store.lighting()
        before_controls = store.controls()
        before_jobs = store.jobs()
        before_schedules = store.schedules()
        before_night = store.device_schedules()
        with patch("timesgate.engine.device_command") as send:
            response = self.client.put("/api/profiles", json=profile.model_dump())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["profiles"], [{
                "id": profile.id, "device_id": device.id, "name": "Evening",
            }])
            loaded = self.client.get(f"/api/profiles/{profile.id}")
            self.assertEqual(loaded.status_code, 200, loaded.text)
            self.assertEqual(loaded.json(), profile.model_dump())
            send.assert_not_called()
        self.assertEqual(Store(store.path).profile(profile.id), profile)
        self.assertEqual(store.public_state(), before)
        self.assertEqual(store.lighting(), before_lighting)
        self.assertEqual(store.controls(), before_controls)
        self.assertEqual(store.jobs(), before_jobs)
        self.assertEqual(store.schedules(), before_schedules)
        self.assertEqual(store.device_schedules(), before_night)
        self.assertEqual(store.get("strava_tokens"), {"access_token": "private-strava-token"})
        self.assertNotIn("123456", loaded.text)
        self.assertNotIn("private-strava-token", loaded.text)
        self.assertEqual(self.client.get("/api/state").json()["profiles"], response.json()["profiles"])

    def test_device_profiles_validate_ownership_names_snapshots_and_access(self):
        store = self.app.state.store
        config = store.config()
        other = Device(name="Other")
        config.devices.append(other)
        store.save_config(config, store.revision())
        device = config.devices[0]
        values = DeviceProfile(
            name="Desk", device_id=device.id, screens=device.screens,
            night=device.night, lighting=LightingSettings(),
        ).model_dump()
        self.assertEqual(self.client.put("/api/profiles", json=values, headers={
            "Cookie": "",
        }).status_code, 401)
        self.assertEqual(self.client.put("/api/profiles", json=values, headers={
            "Origin": "http://untrusted.invalid",
        }).status_code, 403)
        for invalid in (
            {**values, "name": "   "},
            {**values, "device_id": "b" * 32},
            {**values, "screens": values["screens"][:4]},
            {**values, "lighting": {**values["lighting"], "brightness": 101}},
            {**values, "local_token": 123456},
            {**values, "host": "192.168.1.10"},
        ):
            response = self.client.put("/api/profiles", json=invalid)
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.client.put("/api/profiles", json=values).status_code, 200)
        key = values["id"]
        path = f"/api/profiles/{key}"
        duplicate = {**values, "id": "c" * 32, "name": " desk "}
        self.assertEqual(self.client.put("/api/profiles", json=duplicate).status_code, 400)
        self.assertEqual(self.client.put("/api/profiles", json={
            **values, "device_id": other.id,
        }).status_code, 400)
        self.assertEqual(self.client.put("/api/profiles", json={
            **duplicate, "device_id": other.id,
        }).status_code, 200)
        for method in ("get", "patch", "delete"):
            arguments = {"json": {"name": "Renamed"}} if method == "patch" else {}
            self.assertEqual(getattr(self.client, method)(
                path, headers={"Cookie": ""}, **arguments,
            ).status_code, 401)
        self.assertEqual(self.client.patch(path, json={"name": "   "}).status_code, 400)
        self.assertEqual(self.client.patch(path, json={"name": " Work "}).status_code, 200)
        self.assertEqual(self.client.get(path).json()["name"], "Work")
        updated = {**values, "name": "Work", "lighting": {**values["lighting"], "keys": False}}
        self.assertEqual(self.client.put("/api/profiles", json=updated).status_code, 200)
        self.assertFalse(self.client.get(path).json()["lighting"]["keys"])
        self.assertEqual(self.client.delete(path).status_code, 200)
        self.assertEqual(self.client.get(path).status_code, 400)
        self.assertEqual(self.client.delete(path).status_code, 400)
        self.assertEqual(len(store.profiles()), 1)

    def test_device_profiles_check_media_on_save_and_restore(self):
        store = self.app.state.store
        device = store.config().devices[0]
        media_id = "d" * 32
        profile = DeviceProfile(
            name="Media", device_id=device.id,
            screens=[Screen(kind="media", media_id=media_id), *device.screens[1:]],
            night=device.night, lighting=LightingSettings(),
        )
        response = self.client.put("/api/profiles", json=profile.model_dump())
        self.assertEqual(response.status_code, 400)
        self.assertIn("media does not exist", response.text)
        profile.screens[0] = Screen(kind="custom", elements=[
            DesignLayer(type="image", media_id=media_id),
        ])
        self.assertEqual(self.client.put("/api/profiles", json=profile.model_dump()).status_code, 400)
        store.add_media(media_id, "Example", 1, 100)
        self.assertEqual(self.client.put("/api/profiles", json=profile.model_dump()).status_code, 200)
        with store.connect() as db:
            db.execute("DELETE FROM media WHERE id=?", (media_id,))
        self.assertEqual(self.client.get(f"/api/profiles/{profile.id}").status_code, 400)
        self.assertEqual(self.client.patch(f"/api/profiles/{profile.id}", json={
            "name": "Missing media",
        }).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/profiles/{profile.id}").status_code, 200)

    def test_device_profile_limit_allows_update_and_rejects_duplicate_renames(self):
        store = self.app.state.store
        device = store.config().devices[0]
        profile = DeviceProfile(
            name="Profile 0", device_id=device.id, screens=device.screens,
            night=device.night, lighting=LightingSettings(),
        )
        store.save_profile(profile)
        for index in range(1, 20):
            store.save_profile(profile.model_copy(update={"id": f"{index:032x}", "name": f"Profile {index}"}))
        self.assertEqual(len(store.profiles()), 20)
        profile.lighting.brightness = 25
        self.assertEqual(self.client.put("/api/profiles", json=profile.model_dump()).status_code, 200)
        values = {**profile.model_dump(), "id": "e" * 32, "name": "Overflow"}
        self.assertEqual(self.client.put("/api/profiles", json=values).status_code, 400)
        self.assertEqual(self.client.post(f"/api/profiles/{profile.id}/clone", json={
            "name": "Overflow clone",
        }).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/profiles/{profile.id}", json={
            "name": "PROFILE 1",
        }).status_code, 400)
        self.assertEqual(store.profile(profile.id).name, "Profile 0")

    def test_saving_configuration_updates_loaded_profile_and_remembers_selection_without_applying_lighting(self):
        store = self.app.state.store
        config = store.config()
        device = config.devices[0]
        device.host, device.local_token = "192.168.1.10", 123456
        store.save_config(config, store.revision())
        profile = DeviceProfile(
            name="Desk", device_id=device.id, screens=device.screens,
            night=device.night, lighting=LightingSettings(),
        )
        store.save_profile(profile)
        config.devices[0].screens[0] = Screen(kind="text", text="Edited layout")
        config.devices[0].night.day_brightness = 45
        changed = DeviceProfile(
            id=profile.id, name=profile.name, device_id=device.id,
            screens=config.devices[0].screens, night=config.devices[0].night,
            lighting=LightingSettings(color="#FFD166", brightness=25),
        )
        before_jobs, before_lighting = store.jobs(), store.lighting()
        with patch("timesgate.engine.device_command") as send:
            response = self.client.put("/api/config", json={
                "config": config.model_dump(), "revision": store.revision(),
                "profiles": [changed.model_dump()],
            })
            self.assertEqual(response.status_code, 200, response.text)
            send.assert_not_called()
        self.assertEqual(store.config().devices[0].screens[0].text, "Edited layout")
        self.assertEqual(store.profile(profile.id), changed)
        self.assertEqual(Store(store.path).active_profiles(), {device.id: changed.model_dump()})
        state = self.client.get("/api/state").json()
        self.assertEqual(state["active_profiles"][device.id], changed.model_dump())
        self.assertNotIn("local_token", str(state["active_profiles"]))
        self.assertEqual(store.jobs(), before_jobs)
        self.assertEqual(store.lighting(), before_lighting)
        store.rename_profile(profile.id, "Evening")
        self.assertEqual(store.active_profiles()[device.id]["name"], "Evening")
        before = store.public_state()
        store.delete_profile(profile.id)
        self.assertEqual(store.active_profiles(), {})
        self.assertEqual(store.public_state(), before)

    def test_failed_loaded_profile_save_rolls_back_configuration_and_all_profile_updates(self):
        store = self.app.state.store
        config = store.config()
        config.devices[0].host, config.devices[0].local_token = "192.168.1.10", 123456
        config.devices.append(Device(name="Other", host="192.168.1.11", local_token=654321))
        store.save_config(config, store.revision())
        profiles = [
            DeviceProfile(
                name="Desk", device_id=device.id, screens=device.screens,
                night=device.night, lighting=LightingSettings(),
            ) for device in config.devices
        ]
        for profile in profiles:
            store.save_profile(profile)
        before = store.public_state()
        config.devices[0].screens[0] = Screen(kind="clock", enabled=True)
        changed = [
            profile.model_copy(update={"screens": device.screens, "night": device.night})
            for profile, device in zip(profiles, config.devices)
        ]
        changed[0].lighting.color = "#FFD166"
        store.delete_profile(profiles[1].id)
        response = self.client.put("/api/config", json={
            "config": config.model_dump(), "revision": store.revision(),
            "profiles": [profile.model_dump() for profile in changed],
        })
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("no longer exists", response.text)
        self.assertEqual(store.public_state(), before)
        self.assertEqual(store.profile(profiles[0].id).lighting.color, "#55e6b0")
        self.assertEqual(store.schedules(), [])
        self.assertEqual(store.active_profiles(), {})
        self.assertEqual(len(store.profiles()), 1)
        mismatch = changed[0].model_copy(update={"night": config.devices[1].night.model_copy(
            update={"day_brightness": 1},
        )})
        response = self.client.put("/api/config", json={
            "config": config.model_dump(), "revision": store.revision(),
            "profiles": [mismatch.model_dump()],
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(store.public_state(), before)

    def test_cloning_profiles_copies_saved_snapshot_and_keeps_source_and_active_settings_independent(self):
        store = self.app.state.store
        config = store.config()
        device = config.devices[0]
        source = DeviceProfile(
            name="Desk", device_id=device.id,
            screens=[Screen(kind="text", text="Saved snapshot"), *device.screens[1:]],
            night=device.night, lighting=LightingSettings(color="#FFD166"),
        )
        store.save_profile(source)
        before = store.public_state()
        path = f"/api/profiles/{source.id}/clone"
        for headers, status in (
            ({"Cookie": ""}, 401),
            ({"Origin": "http://untrusted.invalid"}, 403),
        ):
            self.assertEqual(self.client.post(path, json={"name": "Copy"}, headers=headers).status_code, status)
        response = self.client.post(path, json={"name": " Evening "})
        self.assertEqual(response.status_code, 200, response.text)
        clone = DeviceProfile.model_validate(response.json()["profile"])
        self.assertNotEqual(clone.id, source.id)
        self.assertEqual(clone.name, "Evening")
        self.assertEqual(clone.screens, source.screens)
        self.assertEqual(clone.night, source.night)
        self.assertEqual(clone.lighting, source.lighting)
        self.assertEqual(clone.device_id, device.id)
        self.assertEqual(store.public_state(), before)
        self.assertEqual(store.active_profiles(), {})
        self.assertEqual(store.jobs(), [])
        source.screens[0].text = "Changed source"
        store.save_profile(source)
        self.assertEqual(store.profile(clone.id).screens[0].text, "Saved snapshot")
        self.assertEqual(self.client.post(path, json={"name": "evening"}).status_code, 400)
        self.assertEqual(self.client.post(path, json={"name": " "}).status_code, 400)
        self.assertEqual(self.client.post("/api/profiles/missing/clone", json={"name": "Copy"}).status_code, 400)

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
