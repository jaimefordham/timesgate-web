from __future__ import annotations

from collections import deque
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import threading
import time

from .store import Store

COOKIE = "timesgate-session"
SESSION_SECONDS = 12 * 60 * 60


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def password_digest(password: str, salt: bytes) -> str:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=32768, r=8, p=3,
        maxmem=64 * 1024 * 1024, dklen=32,
    ).hex()


class Auth:
    def __init__(self, store: Store, data_dir: Path):
        self.store = store
        self.lock = threading.Lock()
        self.failures: deque[float] = deque()
        with store.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    digest TEXT PRIMARY KEY, expires REAL NOT NULL
                )
            """)
        # Remove the obsolete first-run code when upgrading an existing installation.
        (data_dir / "setup.code").unlink(missing_ok=True)

    def setup_required(self) -> bool:
        return self.store.get("admin_account") is None

    def authenticated(self, token: str) -> bool:
        if not token:
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.store.connect() as db:
            row = db.execute(
                "SELECT expires FROM admin_sessions WHERE digest=?", (digest,),
            ).fetchone()
        return row is not None and row["expires"] > time.time()

    def _check_attempts(self) -> None:
        cutoff = time.monotonic() - 60
        while self.failures and self.failures[0] <= cutoff:
            self.failures.popleft()
        if len(self.failures) >= 5:
            raise AuthError("Too many failed sign-in attempts. Try again in one minute.", 429)

    def _session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.store.connect() as db:
            db.execute("DELETE FROM admin_sessions WHERE expires<=?", (now,))
            db.execute(
                "INSERT INTO admin_sessions VALUES (?, ?)",
                (hashlib.sha256(token.encode()).hexdigest(), now + SESSION_SECONDS),
            )
        return token

    def setup(self, username: str, password: str) -> str:
        with self.lock:
            if not self.setup_required():
                raise AuthError("The administrator account has already been created.", 409)
            self._check_attempts()
            salt = secrets.token_bytes(16)
            self.store.put("admin_account", {
                "username": username, "salt": salt.hex(),
                "password_hash": password_digest(password, salt),
            })
            self.failures.clear()
            return self._session()

    def login(self, username: str, password: str) -> str:
        with self.lock:
            account = self.store.get("admin_account")
            if account is None:
                raise AuthError("Create the administrator account first.", 409)
            self._check_attempts()
            digest = password_digest(password, bytes.fromhex(account["salt"]))
            valid_password = hmac.compare_digest(digest, account["password_hash"])
            valid_username = hmac.compare_digest(username.encode(), account["username"].encode())
            if not (valid_password and valid_username):
                self.failures.append(time.monotonic())
                raise AuthError("Invalid username or password.")
            self.failures.clear()
            return self._session()

    def logout(self, token: str) -> None:
        with self.store.connect() as db:
            db.execute(
                "DELETE FROM admin_sessions WHERE digest=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )


if __name__ == "__main__":
    import argparse

    from filelock import FileLock

    parser = argparse.ArgumentParser(description="Reset the single administrator without deleting settings.")
    parser.add_argument("--reset-admin", action="store_true", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("DATA_DIR", "data")))
    args = parser.parse_args()
    directory = args.data_dir.resolve()
    if not (directory / "timesgate.sqlite").is_file():
        parser.error("No existing database in the supplied data directory.")
    with FileLock(directory / "controller.lock", timeout=0):
        store = Store(directory / "timesgate.sqlite")
        Auth(store, directory)
        with store.connect() as db:
            db.execute("DELETE FROM kv WHERE key='admin_account'")
            db.execute("DELETE FROM admin_sessions")
    print("Administrator reset. Restart the server and create the administrator in the web interface.")
