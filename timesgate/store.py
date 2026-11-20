from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .lighting import lighting_approval, target_signature
from .controls import night_next, night_phase, night_signature
from .models import (
    Configuration, Device, DeviceProfile, LightingPreset, LightingSettings, Screen,
    canonical, next_due,
)


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS media (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL, frames INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedules (
                    key TEXT PRIMARY KEY, device_id TEXT NOT NULL, panel INTEGER NOT NULL,
                    signature TEXT NOT NULL, next_due REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, operation TEXT NOT NULL, device_id TEXT,
                    panel INTEGER, automatic INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL, created REAL NOT NULL, finished REAL,
                    message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS renders (name TEXT PRIMARY KEY, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS device_schedules (
                    device_id TEXT NOT NULL, phase TEXT NOT NULL, signature TEXT NOT NULL,
                    next_due REAL NOT NULL, PRIMARY KEY (device_id, phase)
                );
                CREATE TABLE IF NOT EXISTS device_profiles (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
                    name TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS active_profiles (
                    device_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
            if "payload" not in columns:
                db.execute("ALTER TABLE jobs ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'")
            db.execute("INSERT OR IGNORE INTO kv VALUES ('config', ?)",
                       (canonical(Configuration().model_dump()),))
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key: str, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value: object) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, canonical(value)))

    def config(self) -> Configuration:
        return Configuration.model_validate(self.get("config"))

    def revision(self) -> str:
        return hashlib.sha256(canonical(self.get("config")).encode()).hexdigest()

    def save_config(self, value: Configuration, revision: str, now: float | None = None, *,
                    profiles: list[DeviceProfile] | None = None) -> str:
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old_json = db.execute("SELECT value FROM kv WHERE key='config'").fetchone()["value"]
            if hashlib.sha256(old_json.encode()).hexdigest() != revision:
                raise Conflict("Configuration changed; reload before saving")
            previous = {d["id"]: d for d in json.loads(old_json)["devices"]}
            for device in value.devices:
                old = previous.get(device.id, {})
                if "local_token" not in device.model_fields_set:
                    same_target = (old.get("host"), old.get("hardware")) == (device.host, device.hardware)
                    device.local_token = old.get("local_token") if same_target else None
                if any(s.enabled for s in device.screens) or device.night.enabled:
                    if not device.host or (device.hardware == 402 and device.local_token is None):
                        raise ValueError("Scheduled devices require an IP and a LocalToken for hardware 402")
                self._validate_screen_media(db, device.screens)
            new_json = canonical(value.model_dump())
            db.execute("UPDATE kv SET value=? WHERE key='config'", (new_json,))
            profile_devices = set()
            for profile in profiles or []:
                device = next((item for item in value.devices if item.id == profile.device_id), None)
                if device is None or profile.screens != device.screens or profile.night != device.night:
                    raise ValueError("Profile snapshot must match this device's configuration")
                if device.id in profile_devices:
                    raise ValueError("Save only one loaded profile per device")
                profile_devices.add(device.id)
                self._save_profile(db, profile, require_existing=True)
                db.execute("INSERT OR REPLACE INTO active_profiles VALUES (?,?)",
                           (device.id, profile.id))
            keys = set()
            for device in value.devices:
                for panel, screen in enumerate(device.screens):
                    if not screen.enabled:
                        continue
                    key = f"{device.id}:{panel}"
                    keys.add(key)
                    signature = hashlib.sha256(canonical({
                        "screen": screen.model_dump(), "host": device.host,
                        "hardware": device.hardware, "token": device.local_token,
                        "timezone": value.timezone,
                    }).encode()).hexdigest()
                    existing = db.execute("SELECT signature FROM schedules WHERE key=?", (key,)).fetchone()
                    if existing is None or existing["signature"] != signature:
                        due = now if screen.schedule == "interval" else next_due(screen, value.timezone, now)
                        db.execute("INSERT OR REPLACE INTO schedules VALUES (?, ?, ?, ?, ?)",
                                   (key, device.id, panel, signature, due))
            for row in db.execute("SELECT key FROM schedules").fetchall():
                if row["key"] not in keys:
                    db.execute("DELETE FROM schedules WHERE key=?", (row["key"],))
            night_devices = set()
            for device in value.devices:
                if not device.night.enabled:
                    continue
                night_devices.add(device.id)
                signature = night_signature(device, value.timezone)
                for phase in ("start", "end"):
                    existing = db.execute(
                        "SELECT signature FROM device_schedules WHERE device_id=? AND phase=?",
                        (device.id, phase),
                    ).fetchone()
                    if existing is None or existing["signature"] != signature:
                        db.execute("INSERT OR REPLACE INTO device_schedules VALUES (?,?,?,?)", (
                            device.id, phase, signature, night_next(device.night, phase, value.timezone, now),
                        ))
            for row in db.execute("SELECT DISTINCT device_id FROM device_schedules").fetchall():
                if row["device_id"] not in night_devices:
                    db.execute("DELETE FROM device_schedules WHERE device_id=?", (row["device_id"],))
        return hashlib.sha256(new_json.encode()).hexdigest()

    def public_state(self) -> dict:
        raw = self.get("config")
        revision = hashlib.sha256(canonical(raw).encode()).hexdigest()
        data = Configuration.model_validate(raw).model_dump()
        for device in data["devices"]:
            device["local_token_set"] = device.pop("local_token") is not None
        return {"config": data, "revision": revision}

    def public_config(self) -> dict:
        return self.public_state()["config"]

    @staticmethod
    def _validate_screen_media(db: sqlite3.Connection, screens: list[Screen]) -> None:
        for screen in screens:
            media_ids = []
            if screen.kind == "media":
                media_ids.append(screen.media_id)
            elif screen.kind == "custom":
                media_ids.extend(
                    layer.media_id for layer in screen.elements if layer.type == "image"
                )
            for media_id in filter(None, media_ids):
                if not db.execute("SELECT 1 FROM media WHERE id=?", (media_id,)).fetchone():
                    raise ValueError("Selected media does not exist")

    def profiles(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id,device_id,name FROM device_profiles ORDER BY name COLLATE NOCASE,id",
            )]

    def active_profiles(self) -> dict[str, dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT p.* FROM device_profiles p JOIN active_profiles a
                ON p.id=a.profile_id AND p.device_id=a.device_id
            """).fetchall()
        return {
            row["device_id"]: DeviceProfile(
                id=row["id"], device_id=row["device_id"], name=row["name"],
                **json.loads(row["data"]),
            ).model_dump() for row in rows
        }

    @staticmethod
    def _validate_profile_name(db: sqlite3.Connection, device_id: str, name: str, key: str) -> None:
        for row in db.execute(
            "SELECT id,name FROM device_profiles WHERE device_id=?", (device_id,),
        ):
            if row["id"] != key and row["name"].casefold() == name.casefold():
                raise ValueError("A profile with this name already exists for this device")

    def save_profile(self, profile: DeviceProfile) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._save_profile(db, profile)

    def _save_profile(self, db: sqlite3.Connection, profile: DeviceProfile, *,
                      require_existing: bool = False) -> None:
        config = Configuration.model_validate_json(db.execute(
            "SELECT value FROM kv WHERE key='config'",
        ).fetchone()["value"])
        if not any(device.id == profile.device_id for device in config.devices):
            raise ValueError("Save the device before creating a profile")
        existing = db.execute(
            "SELECT device_id FROM device_profiles WHERE id=?", (profile.id,),
        ).fetchone()
        if require_existing and existing is None:
            raise ValueError("Loaded profile no longer exists; choose another profile before saving")
        if existing and existing["device_id"] != profile.device_id:
            raise ValueError("A profile cannot be moved to another device")
        count = db.execute(
            "SELECT count(*) FROM device_profiles WHERE device_id=?", (profile.device_id,),
        ).fetchone()[0]
        if not existing and count >= 20:
            raise ValueError("Keep at most 20 saved profiles per device")
        self._validate_profile_name(db, profile.device_id, profile.name, profile.id)
        self._validate_screen_media(db, profile.screens)
        db.execute("INSERT OR REPLACE INTO device_profiles VALUES (?,?,?,?)", (
            profile.id, profile.device_id, profile.name,
            canonical(profile.model_dump(exclude={"id", "device_id", "name"})),
        ))

    def profile(self, key: str) -> DeviceProfile:
        with self.connect() as db:
            return self._profile(db, key)

    def _profile(self, db: sqlite3.Connection, key: str) -> DeviceProfile:
        row = db.execute("SELECT * FROM device_profiles WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError("Unknown device profile")
        profile = DeviceProfile(
            id=row["id"], device_id=row["device_id"], name=row["name"],
            **json.loads(row["data"]),
        )
        config = json.loads(db.execute(
            "SELECT value FROM kv WHERE key='config'",
        ).fetchone()["value"])
        if not any(device["id"] == profile.device_id for device in config["devices"]):
            raise ValueError("The device for this profile no longer exists")
        self._validate_screen_media(db, profile.screens)
        return profile

    def clone_profile(self, key: str, name: str) -> DeviceProfile:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            source = self._profile(db, key)
            clone = DeviceProfile(name=name, **source.model_dump(exclude={"id", "name"}))
            self._save_profile(db, clone)
            return clone

    def rename_profile(self, key: str, name: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT device_id FROM device_profiles WHERE id=?", (key,)).fetchone()
            if row is None:
                raise ValueError("Unknown device profile")
            self._validate_profile_name(db, row["device_id"], name, key)
            db.execute("UPDATE device_profiles SET name=? WHERE id=?", (name, key))

    def delete_profile(self, key: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("DELETE FROM device_profiles WHERE id=?", (key,)).rowcount == 0:
                raise ValueError("Unknown device profile")
            db.execute("DELETE FROM active_profiles WHERE profile_id=?", (key,))

    def enqueue(self, operation: str, device_id: str | None = None,
                panel: int | None = None, *, payload: dict | None = None) -> str:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT count(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]
            if count >= 30:
                raise ValueError("Job queue full; wait for current work")
            if operation == "lighting":
                db.execute(
                    "UPDATE jobs SET status='cancelled',finished=?,message=? "
                    "WHERE operation='lighting_restore' AND device_id=? AND status='pending'",
                    (time.time(), "Superseded by a manual lighting Apply.", device_id),
                )
            if operation == "control" and payload["request"]["control"]["action"] in ("brightness", "power"):
                db.execute(
                    "UPDATE jobs SET status='cancelled',finished=?,message=? "
                    "WHERE operation='device_schedule' AND device_id=? AND status='pending'",
                    (time.time(), "Superseded by a manual device control.", device_id),
                )
            key = uuid.uuid4().hex
            db.execute(
                "INSERT INTO jobs (id,operation,device_id,panel,automatic,status,created,payload) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (key, operation, device_id, panel, int(operation == "lighting_restore"),
                 "pending", time.time(), canonical(payload or {})),
            )
        return key

    def schedule_due(self, now: float) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            config = Configuration.model_validate(json.loads(
                db.execute("SELECT value FROM kv WHERE key='config'").fetchone()["value"]))
            devices = {d.id: d for d in config.devices}
            self._schedule_controls(db, config, now)
            for row in db.execute(
                "SELECT * FROM schedules WHERE next_due<=? ORDER BY next_due,key", (now,)
            ).fetchall():
                device = devices[row["device_id"]]
                screen = device.screens[row["panel"]]
                controls = self._control_state(db, device)
                if controls["sleeping"] or controls["paused"]:
                    continue
                busy = db.execute(
                    "SELECT 1 FROM jobs WHERE device_id=? AND panel=? AND status IN ('pending','running')",
                    (device.id, row["panel"]),
                ).fetchone()
                count = db.execute("SELECT count(*) FROM jobs WHERE status IN ('pending','running')").fetchone()[0]
                if busy or count >= 30:
                    continue
                db.execute(
                    "INSERT INTO jobs (id,operation,device_id,panel,automatic,status,created) VALUES (?,?,?,?,1,'pending',?)",
                    (uuid.uuid4().hex, "send", device.id, row["panel"], now),
                )
                db.execute("UPDATE schedules SET next_due=? WHERE key=?",
                           (next_due(screen, config.timezone, now), row["key"]))

    def recover(self) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status='interrupted', finished=?, message=? WHERE status IN ('pending','running')",
                (time.time(), "Controller restarted; old job not replayed. Device effect may be unknown."),
            )

    def claim(self) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created,rowid LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
            return dict(row)

    def finish(self, key: str, status: str, message: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?, finished=?, message=? WHERE id=?",
                       (status, time.time(), message, key))
            devices = json.loads(db.execute(
                "SELECT value FROM kv WHERE key='config'",
            ).fetchone()["value"])["devices"]
            placeholders = ",".join("?" for _ in devices)
            # Keep latest automatic results beyond the bounded recent history.
            db.execute(f"""
                DELETE FROM jobs WHERE status NOT IN ('running','pending')
                AND id NOT IN (SELECT id FROM jobs ORDER BY created DESC LIMIT 300)
                AND id NOT IN (
                    SELECT j.id FROM schedules s JOIN jobs j ON j.id=(
                        SELECT id FROM jobs
                        WHERE device_id=s.device_id AND panel=s.panel
                              AND automatic=1 AND operation='send'
                        ORDER BY created DESC,rowid DESC LIMIT 1
                    )
                )
                AND id NOT IN (
                    SELECT j.id FROM jobs j
                    WHERE j.operation='lighting_restore' AND j.device_id IN ({placeholders})
                    AND j.id=(
                        SELECT id FROM jobs
                        WHERE operation='lighting_restore' AND device_id=j.device_id
                        ORDER BY created DESC,rowid DESC LIMIT 1
                    )
                )
                AND id NOT IN (
                    SELECT j.id FROM device_schedules s JOIN jobs j ON j.id=(
                        SELECT id FROM jobs
                        WHERE operation='device_schedule' AND device_id=s.device_id
                              AND panel=CASE s.phase WHEN 'start' THEN 0 ELSE 1 END
                        ORDER BY created DESC,rowid DESC LIMIT 1
                    )
                )
            """, [device["id"] for device in devices])

    def jobs(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,operation,device_id,panel,automatic,status,created,finished,message,payload "
                "FROM jobs ORDER BY created DESC,rowid DESC LIMIT 40"
            ).fetchall()
        result = []
        for row in rows:
            job = dict(row)
            payload = json.loads(job.pop("payload"))
            if job["operation"] == "control":
                job["action"] = payload["request"]["control"]["action"]
            elif job["operation"] == "device_schedule":
                job["action"] = f"night_{payload['phase']}"
            result.append(job)
        return result

    def _control_state(self, db: sqlite3.Connection, device: Device) -> dict:
        row = db.execute("SELECT value FROM kv WHERE key=?", (f"controls:{device.id}",)).fetchone()
        saved = json.loads(row["value"]) if row else None
        values = saved["state"] if saved and saved["target"] == target_signature(device) else {}
        return {
            "sleeping": False, "paused": False, "brightness": None, "preferences": None,
            "diagnostic": None, "acknowledged_at": None, "simulated_at": None, **values,
        }

    def control_state(self, device: Device) -> dict:
        with self.connect() as db:
            return self._control_state(db, device)

    def controls(self) -> dict:
        with self.connect() as db:
            return {device.id: self._control_state(db, device) for device in self.config().devices}

    def update_controls(self, device: Device, **changes) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = {**self._control_state(db, device), **changes}
            db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (
                f"controls:{device.id}", canonical({"target": target_signature(device), "state": state}),
            ))
            if changes.get("sleeping") is False and not state["paused"]:
                db.execute("UPDATE schedules SET next_due=? WHERE device_id=?", (time.time(), device.id))

    def resume_device(self, device: Device) -> int:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._control_state(db, device)
            if state["sleeping"]:
                raise ValueError("Wake the displays before restoring the layout")
            panels = [
                panel for panel, screen in enumerate(device.screens) if screen.kind != "unmanaged"
                and not db.execute(
                    "SELECT 1 FROM jobs WHERE operation='send' AND device_id=? AND panel=? "
                    "AND status IN ('pending','running')", (device.id, panel),
                ).fetchone()
            ]
            active = db.execute(
                "SELECT count(*) FROM jobs WHERE status IN ('pending','running')",
            ).fetchone()[0]
            if active + len(panels) > 30:
                raise ValueError("Job queue full; wait before restoring the layout")
            state["paused"] = False
            db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (
                f"controls:{device.id}", canonical({"target": target_signature(device), "state": state}),
            ))
            for panel in panels:
                db.execute(
                    "INSERT INTO jobs (id,operation,device_id,panel,status,created) VALUES (?,'send',?,?,'pending',?)",
                    (uuid.uuid4().hex, device.id, panel, time.time()),
                )
        return len(panels)

    def _schedule_controls(self, db: sqlite3.Connection, config: Configuration, now: float) -> None:
        devices = {device.id: device for device in config.devices}
        for row in db.execute(
            "SELECT * FROM device_schedules WHERE next_due<=? ORDER BY next_due",
            (now,),
        ).fetchall():
            device = devices[row["device_id"]]
            phase = row["phase"]
            if phase == night_phase(device.night, config.timezone, now):
                busy = db.execute(
                    "SELECT 1 FROM jobs WHERE operation='device_schedule' AND device_id=? "
                    "AND status IN ('pending','running')", (device.id,),
                ).fetchone()
                count = db.execute(
                    "SELECT count(*) FROM jobs WHERE status IN ('pending','running')",
                ).fetchone()[0]
                if busy or count >= 30:
                    continue
                db.execute(
                    "INSERT INTO jobs (id,operation,device_id,panel,automatic,status,created,payload) "
                    "VALUES (?,'device_schedule',?,?,1,'pending',?,?)",
                    (uuid.uuid4().hex, device.id, 0 if phase == "start" else 1, now, canonical({
                        "signature": row["signature"], "target": target_signature(device), "phase": phase,
                    })),
                )
            db.execute(
                "UPDATE device_schedules SET next_due=? WHERE device_id=? AND phase=?",
                (night_next(device.night, phase, config.timezone, now), device.id, phase),
            )

    def device_schedules(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT s.*,j.id,j.status,j.created,j.finished,j.message,j.payload
                FROM device_schedules s LEFT JOIN jobs j ON j.id=(
                    SELECT id FROM jobs WHERE operation='device_schedule' AND device_id=s.device_id
                          AND panel=CASE s.phase WHEN 'start' THEN 0 ELSE 1 END
                    ORDER BY created DESC,rowid DESC LIMIT 1
                ) ORDER BY s.device_id,s.phase DESC
            """).fetchall()
        return [{
            "device_id": row["device_id"], "phase": row["phase"], "next_due": row["next_due"],
            "last_run": {
                key: row[key] for key in ("id", "status", "created", "finished", "message")
            } if row["id"] and json.loads(row["payload"])["signature"] == row["signature"] else None,
        } for row in rows]

    def save_lighting_preset(self, preset: LightingPreset) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM kv WHERE key='lighting_presets'").fetchone()
            presets = json.loads(row["value"]) if row else []
            if any(item["name"].casefold() == preset.name.casefold() and item["id"] != preset.id for item in presets):
                raise ValueError("A lighting preset with this name already exists")
            presets = [item for item in presets if item["id"] != preset.id]
            if len(presets) >= 20:
                raise ValueError("Keep at most 20 saved lighting presets")
            presets.append(preset.model_dump())
            db.execute("INSERT OR REPLACE INTO kv VALUES ('lighting_presets',?)", (canonical(presets),))

    def delete_lighting_preset(self, key: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM kv WHERE key='lighting_presets'").fetchone()
            presets = json.loads(row["value"]) if row else []
            if not any(item["id"] == key for item in presets):
                raise ValueError("Unknown lighting preset")
            db.execute("UPDATE kv SET value=? WHERE key='lighting_presets'", (
                canonical([item for item in presets if item["id"] != key]),
            ))

    def remember_lighting(self, device: Device, settings: LightingSettings, *,
                          restored: bool = False) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM kv WHERE key=?", (f"lighting:{device.id}",)).fetchone()
            previous = json.loads(row["value"]) if row else None
            same_target = previous is not None and previous["target"] == target_signature(device)
            now = time.time()
            saved = {
                "target": target_signature(device), "settings": settings.model_dump(),
                "applied_at": now,
                "approval": lighting_approval(previous)
                if restored and same_target else uuid.uuid4().hex,
                "restore_on_startup": previous.get("restore_on_startup", False) if same_target else False,
            }
            db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",
                       (f"lighting:{device.id}", canonical(saved)))

    def lighting(self) -> dict:
        result = {}
        for device in self.config().devices:
            saved = self.get(f"lighting:{device.id}")
            if saved is not None and saved["target"] == target_signature(device):
                result[device.id] = {
                    "settings": saved["settings"], "applied_at": saved["applied_at"],
                    "restore_on_startup": saved.get("restore_on_startup", False),
                }
        return result

    def configure_lighting_restore(self, device_id: str, enabled: bool) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            config = Configuration.model_validate_json(db.execute(
                "SELECT value FROM kv WHERE key='config'",
            ).fetchone()["value"])
            device = next((item for item in config.devices if item.id == device_id), None)
            if device is None:
                raise ValueError("Unknown device")
            row = db.execute("SELECT value FROM kv WHERE key=?", (f"lighting:{device_id}",)).fetchone()
            saved = json.loads(row["value"]) if row else None
            if saved is None or saved["target"] != target_signature(device):
                raise ValueError("Apply lighting successfully to this device before enabling restoration")
            saved["restore_on_startup"] = enabled
            db.execute("UPDATE kv SET value=? WHERE key=?", (canonical(saved), f"lighting:{device_id}"))
            if not enabled:
                db.execute(
                    "UPDATE jobs SET status='cancelled',finished=?,message=? "
                    "WHERE operation='lighting_restore' AND device_id=? AND status='pending'",
                    (time.time(), "Startup restoration was disabled.", device_id),
                )

    def lighting_restoration(self) -> list[dict]:
        result = []
        with self.connect() as db:
            config = Configuration.model_validate_json(db.execute(
                "SELECT value FROM kv WHERE key='config'",
            ).fetchone()["value"])
            for device in config.devices:
                row = db.execute("SELECT value FROM kv WHERE key=?", (f"lighting:{device.id}",)).fetchone()
                saved = json.loads(row["value"]) if row else None
                eligible = saved is not None and saved["target"] == target_signature(device)
                latest = db.execute(
                    "SELECT id,status,created,finished,message,payload FROM jobs "
                    "WHERE operation='lighting_restore' AND device_id=? "
                    "ORDER BY created DESC,rowid DESC LIMIT 1", (device.id,),
                ).fetchone()
                run = None
                if eligible and latest is not None:
                    payload = json.loads(latest["payload"])
                    if (
                        payload["target"] == saved["target"]
                        and payload["approval"] == lighting_approval(saved)
                    ):
                        run = {key: latest[key] for key in ("id", "status", "created", "finished", "message")}
                result.append({
                    "device_id": device.id, "eligible": eligible,
                    "enabled": saved.get("restore_on_startup", False) if eligible else False,
                    "blocked": run is not None and run["status"] in ("error", "interrupted"),
                    "last_run": run,
                })
        return result

    def queue_startup_lighting(self) -> None:
        for item in self.lighting_restoration():
            if not item["enabled"] or item["blocked"]:
                continue
            saved = self.get(f"lighting:{item['device_id']}")
            self.enqueue("lighting_restore", item["device_id"], payload={
                "target": saved["target"], "settings": saved["settings"],
                "approval": lighting_approval(saved),
            })

    def queue_status(self) -> tuple[int, bool]:
        with self.connect() as db:
            row = db.execute(
                "SELECT count(*) AS active, coalesce(sum(operation='strava'),0) AS strava "
                "FROM jobs WHERE status IN ('pending','running')"
            ).fetchone()
        return row["active"], bool(row["strava"])

    def schedules(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT s.device_id, s.panel, s.next_due,
                       j.id, j.status, j.created, j.finished, j.message
                FROM schedules s LEFT JOIN jobs j ON j.id=(
                    SELECT id FROM jobs
                    WHERE device_id=s.device_id AND panel=s.panel
                          AND automatic=1 AND operation='send'
                    ORDER BY created DESC,rowid DESC LIMIT 1
                )
                ORDER BY s.device_id,s.panel
            """).fetchall()
        return [
            {
                "device_id": row["device_id"], "panel": row["panel"],
                "next_due": row["next_due"],
                "last_run": {
                    key: row[key] for key in ("id", "status", "created", "finished", "message")
                } if row["id"] is not None else None,
            }
            for row in rows
        ]

    def add_media(self, key: str, label: str, frames: int, duration: int) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO media VALUES (?,?,?,?,?)",
                       (key, label[:100], frames, duration, time.time()))

    def media(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM media ORDER BY created DESC")]

    def remember_render(self, name: str) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO renders VALUES (?,?)", (name, time.time()))

    def expire_renders(self, directory: Path, now: float) -> None:
        with self.connect() as db:
            for row in db.execute("SELECT name FROM renders WHERE created<?", (now - 86400,)).fetchall():
                (directory / row["name"]).unlink(missing_ok=True)
                db.execute("DELETE FROM renders WHERE name=?", (row["name"],))
