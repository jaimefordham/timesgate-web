from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import io
import os
from pathlib import Path
import re
import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .auth import Auth, AuthError, COOKIE
from .engine import Engine
from .lighting import BACK_EFFECTS, EDGE_EFFECTS, target_signature
from .media import MAX_UPLOAD, normalize, publish
from .models import (
    ConfigUpdate, ControlRequest, DeviceProfile, LightingPreset, LightingRequest,
    LightingRestoreRequest, LightingSettings, ProfileName, SendRequest, StravaSettings,
    WIDGET_ACCENTS, http_base,
)
from .store import Conflict, Store
from .strava import Strava, StravaError

STATIC = Path(__file__).parent / "static"


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class SetupInput(LoginInput):
    username: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
    password: str = Field(min_length=8, max_length=1024)


def create_app(data_dir: Path | None = None, media_dir: Path | None = None, *,
               demo: bool | None = None) -> FastAPI:
    data_dir = data_dir or Path(os.getenv("DATA_DIR", "data"))
    media_dir = media_dir or Path(os.getenv("MEDIA_DIR", "media"))
    data_dir = data_dir.resolve()
    media_dir = media_dir.resolve()
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)
    app_base = http_base(os.getenv("APP_BASE_URL", "http://localhost:8080"), http_only=False)
    media_base = os.getenv("MEDIA_BASE_URL", "")
    if media_base:
        media_base = http_base(media_base, http_only=True)
    lock = FileLock(data_dir / "controller.lock", timeout=0)
    conversion_lock = threading.Lock()
    demo = os.getenv("DEMO", "0") == "1" if demo is None else demo

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with lock:
            store = Store(data_dir / "timesgate.sqlite")
            app.state.auth = Auth(store, data_dir)
            store.recover()
            strava = Strava(store, app_base)
            engine = Engine(store, media_dir, media_base, strava, demo=demo)
            app.state.store, app.state.engine, app.state.strava = store, engine, strava
            store.queue_startup_lighting()
            engine.start()
            try:
                yield
            finally:
                engine.stop()
                await asyncio.to_thread(engine.join)

    app = FastAPI(title="Timesgate Web", lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        response = None
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if (origin and origin != app_base) or (
                request.method not in ("GET", "HEAD", "OPTIONS") and origin != app_base
            ):
                response = JSONResponse({"error": "Unexpected browser origin; check APP_BASE_URL"}, status_code=403)
            public = request.url.path in ("/api/auth/status", "/api/auth/setup", "/api/auth/login")
            if response is None and not public and not await asyncio.to_thread(
                app.state.auth.authenticated, request.cookies.get(COOKIE, ""),
            ):
                response = JSONResponse({"error": "Please sign in to continue."}, status_code=401)
        if response is None:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        if request.url.path.startswith(("/api/", "/oauth/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request: Request, error: ValueError):
        if isinstance(error, ValidationError):
            message = "; ".join(
                f"{'.'.join(map(str, entry['loc']))}: {entry['msg']}"
                for entry in error.errors(include_input=False, include_context=False)
            )
        else:
            message = str(error)
        return JSONResponse({"error": message}, status_code=409 if isinstance(error, Conflict) else 400)

    @app.exception_handler(StravaError)
    async def strava_error(request: Request, error: StravaError):
        return JSONResponse({"error": str(error)}, status_code=502)

    @app.exception_handler(AuthError)
    async def authentication_error(request: Request, error: AuthError):
        headers = {"Retry-After": "60"} if error.status == 429 else None
        return JSONResponse({"error": str(error)}, status_code=error.status, headers=headers)

    async def body(request: Request, limit: int = 128 * 1024) -> bytes:
        result = bytearray()
        async for chunk in request.stream():
            result.extend(chunk)
            if len(result) > limit:
                raise HTTPException(413, "Request too large")
        return bytes(result)

    @app.get("/healthz")
    def health():
        return {"ok": app.state.engine.is_alive(), "demo": demo}

    def signed_in(token: str) -> JSONResponse:
        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            COOKIE, token, httponly=True, secure=app_base.startswith("https://"),
            samesite="strict", path="/",
        )
        return response

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        auth = app.state.auth
        return {
            "setup_required": auth.setup_required(),
            "authenticated": auth.authenticated(request.cookies.get(COOKIE, "")),
        }

    @app.post("/api/auth/setup")
    async def setup_admin(request: Request):
        values = SetupInput.model_validate_json(await body(request, 16 * 1024))
        token = await asyncio.to_thread(
            app.state.auth.setup, values.username, values.password,
        )
        return signed_in(token)

    @app.post("/api/auth/login")
    async def login_admin(request: Request):
        values = LoginInput.model_validate_json(await body(request, 16 * 1024))
        token = await asyncio.to_thread(app.state.auth.login, values.username, values.password)
        return signed_in(token)

    @app.post("/api/auth/logout")
    def logout_admin(request: Request):
        app.state.auth.logout(request.cookies.get(COOKIE, ""))
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(
            COOKIE, path="/", httponly=True, secure=app_base.startswith("https://"),
            samesite="strict",
        )
        return response

    @app.get("/api/state")
    def state():
        store = app.state.store
        return {**store.public_state(),
                "media": store.media(), "jobs": store.jobs(), "schedules": store.schedules(),
                "strava": app.state.strava.status(), "demo": demo,
                "widget_accents": WIDGET_ACCENTS,
                "lighting": store.lighting(),
                "lighting_restoration": store.lighting_restoration(),
                "controls": store.controls(), "device_schedules": store.device_schedules(),
                "lighting_presets": store.get("lighting_presets", []),
                "profiles": store.profiles(),
                "active_profiles": store.active_profiles(),
                "lighting_defaults": LightingSettings().model_dump(),
                "lighting_effects": {"edges": EDGE_EFFECTS, "back": BACK_EFFECTS},
                "media_base_url": media_base, "version": "0.1.0"}

    @app.put("/api/config")
    async def save_config(request: Request):
        values = ConfigUpdate.model_validate_json(await body(request, 2 * 1024 * 1024))
        revision = app.state.store.save_config(
            values.config, values.revision, profiles=values.profiles,
        )
        return {"revision": revision}

    @app.post("/api/send")
    async def send(request: Request):
        values = SendRequest.model_validate_json(await body(request))
        device, screen, _ = app.state.engine.select(values.device_id, values.panel)
        if screen.kind == "unmanaged":
            raise ValueError("Choose content before sending")
        controls = app.state.store.control_state(device)
        if controls["sleeping"] or controls["paused"]:
            raise ValueError("Wake the displays and resume the controller before sending")
        return {"job": app.state.store.enqueue("send", values.device_id, values.panel)}

    @app.put("/api/profiles")
    async def save_profile(request: Request):
        values = DeviceProfile.model_validate_json(await body(request, 256 * 1024))
        app.state.store.save_profile(values)
        return {"profiles": app.state.store.profiles(), "profile": values.model_dump()}

    @app.post("/api/profiles/{key}/clone")
    async def clone_profile(key: str, request: Request):
        values = ProfileName.model_validate_json(await body(request, 16 * 1024))
        clone = app.state.store.clone_profile(key, values.name)
        return {"profiles": app.state.store.profiles(), "profile": clone.model_dump()}

    @app.get("/api/profiles/{key}")
    def get_profile(key: str):
        return app.state.store.profile(key).model_dump()

    @app.patch("/api/profiles/{key}")
    async def rename_profile(key: str, request: Request):
        values = ProfileName.model_validate_json(await body(request, 16 * 1024))
        app.state.store.rename_profile(key, values.name)
        return {"profiles": app.state.store.profiles()}

    @app.delete("/api/profiles/{key}")
    def delete_profile(key: str):
        app.state.store.delete_profile(key)
        return {"profiles": app.state.store.profiles()}

    @app.post("/api/device/control")
    async def device_control(request: Request):
        values = ControlRequest.model_validate_json(await body(request, 16 * 1024))
        device, _, _ = app.state.engine.select(values.device_id, 0)
        if not device.host or (device.hardware == 402 and device.local_token is None):
            raise ValueError("Save the device IP and LocalToken before using device controls")
        return {"job": app.state.store.enqueue("control", device.id, payload={
            "request": values.model_dump(), "target": target_signature(device),
        })}

    @app.post("/api/lighting")
    async def lighting(request: Request):
        values = LightingRequest.model_validate_json(await body(request, 16 * 1024))
        device, _, _ = app.state.engine.select(values.device_id, 0)
        if not device.host or (device.hardware == 402 and device.local_token is None):
            raise ValueError("Save the device IP and LocalToken before applying lighting")
        return {"job": app.state.store.enqueue("lighting", device.id, payload={
            "settings": values.settings.model_dump(), "target": target_signature(device),
        })}

    @app.put("/api/lighting/restore")
    async def lighting_restore(request: Request):
        values = LightingRestoreRequest.model_validate_json(await body(request, 16 * 1024))
        app.state.store.configure_lighting_restore(values.device_id, values.enabled)
        return {
            "lighting": app.state.store.lighting(),
            "lighting_restoration": app.state.store.lighting_restoration(),
        }

    @app.put("/api/lighting/presets")
    async def save_lighting_preset(request: Request):
        values = LightingPreset.model_validate_json(await body(request, 16 * 1024))
        app.state.store.save_lighting_preset(values)
        return {"lighting_presets": app.state.store.get("lighting_presets", [])}

    @app.delete("/api/lighting/presets/{key}")
    def delete_lighting_preset(key: str):
        if not re.fullmatch(r"[a-f0-9]{32}", key):
            raise ValueError("Invalid lighting preset ID")
        app.state.store.delete_lighting_preset(key)
        return {"lighting_presets": app.state.store.get("lighting_presets", [])}

    @app.post("/api/media")
    async def upload(request: Request, name: str = "image"):
        blob = await body(request, MAX_UPLOAD)
        def convert():
            if not conversion_lock.acquire(blocking=False):
                raise ValueError("Another image is being converted; try again shortly")
            try:
                result, frames, duration = normalize(blob)
                filename = publish(media_dir, result)
                try:
                    app.state.store.add_media(filename[:-4], Path(name).name, frames, duration)
                except Exception:
                    (media_dir / filename).unlink(missing_ok=True)
                    raise
                return {"id": filename[:-4], "frames": frames, "duration_ms": duration}
            finally:
                conversion_lock.release()
        return await asyncio.to_thread(convert)

    @app.get("/api/media/{key}")
    def media_preview(key: str):
        if not re.fullmatch(r"[a-f0-9]{32}", key):
            raise HTTPException(404)
        if not any(item["id"] == key for item in app.state.store.media()):
            raise HTTPException(404)
        path = media_dir / (key + ".gif")
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="image/gif")

    @app.get("/api/preview/{device_id}/{panel}")
    def preview(device_id: str, panel: int):
        _, screen, timezone = app.state.engine.select(device_id, panel)
        if screen.kind == "media":
            return media_preview(screen.media_id)
        image = app.state.engine.image(screen, timezone, preview=True)
        output = io.BytesIO()
        image.save(output, "PNG")
        return Response(output.getvalue(), media_type="image/png")

    @app.put("/api/strava")
    async def configure_strava(request: Request):
        settings = StravaSettings.model_validate_json(await body(request))
        app.state.strava.configure(settings)
        return app.state.strava.status()

    @app.post("/api/strava/authorize")
    def authorize():
        if demo:
            raise ValueError("Strava network access is disabled in demo mode")
        return {"url": app.state.strava.authorize()}

    @app.get("/oauth/strava/callback")
    def callback(state: str = "", code: str = "", scope: str = "", error: str = ""):
        if demo:
            raise ValueError("Strava network access is disabled in demo mode")
        app.state.strava.complete(state, code, scope, error)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/strava/refresh")
    def refresh_strava():
        return {"job": app.state.store.enqueue("strava")}

    @app.delete("/api/strava")
    def disconnect_strava():
        app.state.strava.disconnect()
        return {"disconnected": True}

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="frontend")
    return app
