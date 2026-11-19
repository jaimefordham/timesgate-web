from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from timesgate.app import create_app
from timesgate.auth import COOKIE

ORIGIN = "http://localhost:8080"
PASSWORD = "testpass"


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict("os.environ", {"APP_BASE_URL": ORIGIN, "MEDIA_BASE_URL": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = create_app(self.root / "data", self.root / "media", demo=True)
        self.client = TestClient(self.app).__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.headers["Origin"] = ORIGIN

    def setup_admin(self):
        return self.client.post("/api/auth/setup", json={
            "username": "admin", "password": PASSWORD,
        })

    def test_setup_without_code_requires_eight_characters_and_only_one_account(self):
        self.assertEqual(self.client.get("/api/auth/status").json(), {
            "setup_required": True, "authenticated": False,
        })
        path = self.root / "data" / "setup.code"
        self.assertFalse(path.exists())
        for password in ("", "short", "1234567"):
            with self.subTest(password_length=len(password)):
                response = self.client.post("/api/auth/setup", json={
                    "username": "admin", "password": password,
                })
                self.assertEqual(response.status_code, 400)
                if password:
                    self.assertNotIn(password, response.text)
        self.assertTrue(self.app.state.auth.setup_required())
        response = self.setup_admin()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(path.exists())
        account = self.app.state.store.get("admin_account")
        self.assertNotIn(PASSWORD, str(account))
        self.assertEqual(account["username"], "admin")
        self.assertEqual(self.client.post("/api/auth/setup", json={
            "username": "other", "password": PASSWORD,
        }).status_code, 409)
        self.assertEqual(self.client.get("/setup.code").status_code, 404)
        self.assertNotIn("admin_account", self.client.get("/api/state").json())

    def test_cookie_login_logout_revokes_session_and_rejects_old_bearer_auth(self):
        response = self.setup_admin()
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertNotIn("Max-Age", cookie)
        token = self.client.cookies.get(COOKIE)
        with self.app.state.store.connect() as db:
            row = db.execute("SELECT digest FROM admin_sessions").fetchone()
        self.assertNotEqual(row["digest"], token)
        self.assertEqual(self.client.get("/api/state").status_code, 200)
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        denied = self.client.get("/api/state", headers={
            "Cookie": f"{COOKIE}={token}",
        })
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/api/state", headers={
            "Authorization": "Bearer " + "old-token" * 8,
        }).status_code, 401)
        response = self.client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(self.client.cookies.get(COOKIE), token)
        self.assertEqual(self.client.get("/api/state").status_code, 200)

    def test_cookie_auth_requires_same_origin_for_all_writes_including_login(self):
        self.setup_admin()
        for origin in ("", "http://untrusted.invalid"):
            for path in ("/api/auth/login", "/api/auth/setup", "/api/auth/logout", "/api/send"):
                with self.subTest(origin=origin, path=path):
                    response = self.client.post(path, headers={"Origin": origin}, json={})
                    self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/api/state").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/status", headers={
            "Origin": "http://untrusted.invalid",
        }).status_code, 403)

    def test_failed_logins_are_generic_and_rate_limited(self):
        self.setup_admin()
        for username in ("admin", "unknown", "admin", "unknown", "admin"):
            response = self.client.post("/api/auth/login", json={
                "username": username, "password": "wrong-password",
            })
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json(), {"error": "Invalid username or password."})
        response = self.client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["retry-after"], "60")
        self.app.state.auth.failures = type(self.app.state.auth.failures)(
            value - 61 for value in self.app.state.auth.failures
        )
        self.assertEqual(self.client.post("/api/auth/login", json={
            "username": "admin", "password": PASSWORD,
        }).status_code, 200)

    def test_account_config_and_sessions_survive_restart_but_sessions_expire(self):
        data, media = self.root / "restart-data", self.root / "restart-media"
        with TestClient(create_app(data, media, demo=True), headers={"Origin": ORIGIN}) as client:
            response = client.post("/api/auth/setup", json={
                "username": "owner", "password": PASSWORD,
            })
            self.assertEqual(response.status_code, 200)
            token = client.cookies.get(COOKIE)
            state = client.get("/api/state").json()
            state["config"]["timezone"] = "Europe/Lisbon"
            self.assertEqual(client.put("/api/config", json={
                "config": state["config"], "revision": state["revision"],
            }).status_code, 200)
            reset = subprocess.run(
                [sys.executable, "-m", "timesgate.auth", "--reset-admin", "--data-dir", str(data)],
                capture_output=True, text=True,
            )
            self.assertNotEqual(reset.returncode, 0)
            self.assertTrue(client.get("/api/auth/status").json()["authenticated"])
        (data / "setup.code").write_text("obsolete-setup-code\n")
        app = create_app(data, media, demo=True)
        with TestClient(app, headers={"Origin": ORIGIN}) as client:
            self.assertFalse((data / "setup.code").exists())
            client.cookies.set(COOKIE, token)
            self.assertEqual(client.get("/api/auth/status").json(), {
                "authenticated": True, "setup_required": False,
            })
            self.assertEqual(client.get("/api/state").json()["config"]["timezone"], "Europe/Lisbon")
            with app.state.store.connect() as db:
                db.execute("UPDATE admin_sessions SET expires=?", (time.time() - 1,))
            self.assertEqual(client.get("/api/state").status_code, 401)
            self.assertFalse(client.get("/api/auth/status").json()["authenticated"])
        reset = subprocess.run(
            [sys.executable, "-m", "timesgate.auth", "--reset-admin", "--data-dir", str(data)],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("Administrator reset", reset.stdout)
        with TestClient(create_app(data, media, demo=True), headers={"Origin": ORIGIN}) as client:
            client.cookies.set(COOKIE, token)
            self.assertEqual(client.get("/api/auth/status").json(), {
                "authenticated": False, "setup_required": True,
            })
            self.assertEqual(client.get("/api/state").status_code, 401)
            self.assertFalse((data / "setup.code").exists())
            self.assertEqual(client.post("/api/auth/setup", json={
                "username": "replacement", "password": PASSWORD,
            }).status_code, 200)
            self.assertEqual(client.get("/api/state").json()["config"]["timezone"], "Europe/Lisbon")

    def test_https_uses_secure_cookie_and_media_stays_separate(self):
        with patch.dict("os.environ", {"APP_BASE_URL": "https://controller.example"}):
            app = create_app(self.root / "https-data", self.root / "https-media", demo=True)
        with TestClient(app, base_url="https://controller.example", headers={
            "Origin": "https://controller.example",
        }) as client:
            response = client.post("/api/auth/setup", json={
                "username": "admin", "password": PASSWORD,
            })
            self.assertEqual(response.status_code, 200)
            self.assertIn("Secure", response.headers["set-cookie"])
            self.assertEqual(client.get("/api/state").status_code, 200)
            self.assertNotIn(COOKIE, client.get("/api/state").text)
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/media/file.gif").status_code, 404)
        self.assertEqual(self.client.get("/api/media/" + "a" * 32).status_code, 401)


if __name__ == "__main__":
    unittest.main()
