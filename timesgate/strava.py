from __future__ import annotations

import hmac
import math
import secrets
import threading
import time
from urllib.parse import urlencode

import requests

from .models import StravaSettings
from .store import Store

API = "https://www.strava.com/api/v3"
TOKEN_URL = "https://www.strava.com/oauth/token"
FIELDS = {"ytd": "ytd_ride_totals", "all-time": "all_ride_totals"}


class StravaError(RuntimeError):
    pass


class Strava:
    def __init__(self, store: Store, app_base_url: str, session=requests):
        self.store, self.app_base_url, self.session = store, app_base_url, session
        self.lock = threading.RLock()

    def status(self) -> dict:
        settings = self.store.get("strava_settings", {})
        cache = self.store.get("strava_cache")
        return {
            "client_id": settings.get("client_id", ""),
            "client_secret_set": bool(settings.get("client_secret")),
            "poll_minutes": settings.get("poll_minutes", 30),
            "connected": bool(self.store.get("strava_tokens")),
            "fetched_at": cache["fetched_at"] if cache else None,
            "next_poll": self.store.get("strava_next_poll"),
            "redirect_uri": self.app_base_url + "/oauth/strava/callback",
        }

    def configure(self, incoming: StravaSettings) -> None:
        with self.lock:
            previous = self.store.get("strava_settings", {})
            changed_id = previous.get("client_id") != incoming.client_id
            secret = incoming.client_secret
            if secret is None and not changed_id:
                secret = previous.get("client_secret")
            if not secret:
                raise ValueError("Provide the Strava client secret for this client ID")
            if changed_id:
                self.store.put("strava_tokens", None)
                self.store.put("strava_cache", None)
            self.store.put("strava_oauth", None)
            self.store.put("strava_settings", {
                "client_id": incoming.client_id, "client_secret": secret,
                "poll_minutes": incoming.poll_minutes,
            })

    def authorize(self) -> str:
        with self.lock:
            settings = self.store.get("strava_settings", {})
            if not settings.get("client_secret"):
                raise ValueError("Save Strava client settings first")
            state = secrets.token_urlsafe(32)
            self.store.put("strava_oauth", {"state": state, "expires": time.time() + 600})
            query = urlencode({
                "client_id": settings["client_id"],
                "redirect_uri": self.status()["redirect_uri"],
                "response_type": "code", "approval_prompt": "force",
                "scope": "read,activity:read", "state": state,
            })
            return "https://www.strava.com/oauth/authorize?" + query

    def _request(self, method: str, url: str, **kwargs) -> dict:
        try:
            response = self.session.request(method, url, timeout=(5, 20),
                                            allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise StravaError("Strava connection failed or timed out") from None
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "")
            seconds = min(86400, max(60, int(retry))) if retry.isdigit() else 900
            self.store.put("strava_next_poll", time.time() + seconds)
            raise StravaError(f"Strava rate limit reached; polling paused for {seconds} seconds")
        if not 200 <= response.status_code < 300:
            raise StravaError(f"Strava HTTP {response.status_code}; check authorization and API limits")
        try:
            body = response.json()
        except ValueError:
            raise StravaError("Strava returned invalid JSON") from None
        if not isinstance(body, dict):
            raise StravaError("Strava returned an unexpected response")
        return body

    def _tokens(self, body: dict, athlete_id: int | None = None) -> dict:
        if (
            not isinstance(body.get("access_token"), str) or not body["access_token"]
            or not isinstance(body.get("refresh_token"), str) or not body["refresh_token"]
            or type(body.get("expires_at")) is not int
        ):
            raise StravaError("Strava token response is incomplete")
        athlete = body.get("athlete")
        if athlete is not None:
            if not isinstance(athlete, dict):
                raise StravaError("Strava token response has an invalid athlete")
            athlete_id = athlete.get("id", athlete_id)
        if athlete_id is not None and (type(athlete_id) is not int or athlete_id < 1):
            raise StravaError("Strava token response has an invalid athlete ID")
        return {key: body[key] for key in ("access_token", "refresh_token", "expires_at")} | {
            "athlete_id": athlete_id,
        }

    def complete(self, state: str, code: str, scope: str, error: str = "") -> None:
        with self.lock:
            pending = self.store.get("strava_oauth")
            if (
                not pending or pending["expires"] < time.time()
                or not hmac.compare_digest(state.encode(), pending["state"].encode())
            ):
                raise ValueError("Invalid or expired Strava authorization state; start again")
            self.store.put("strava_oauth", None)
            if error or not code:
                raise ValueError("Strava authorization was declined or did not return a code")
            granted = set(scope.split(","))
            if not {"read", "activity:read"}.issubset(granted):
                raise ValueError("Grant read and activity:read access before connecting")
            settings = self.store.get("strava_settings")
            body = self._request("POST", TOKEN_URL, data={
                "client_id": settings["client_id"], "client_secret": settings["client_secret"],
                "grant_type": "authorization_code", "code": code,
            })
            self.store.put("strava_tokens", self._tokens(body))
            self.store.put("strava_cache", None)
            self.store.put("strava_next_poll", 0)

    def disconnect(self) -> None:
        with self.lock:
            for key in ("strava_tokens", "strava_cache", "strava_oauth"):
                self.store.put(key, None)

    def fetch(self) -> dict:
        with self.lock:
            settings = self.store.get("strava_settings", {})
            tokens = self.store.get("strava_tokens")
            if not tokens:
                raise StravaError("Connect Strava before refreshing statistics")
            now = time.time()
            next_poll = self.store.get("strava_next_poll", 0)
            if next_poll and next_poll > now:
                raise StravaError("Strava polling is not due yet; wait for the next poll time")
            self.store.put("strava_next_poll", now + settings.get("poll_minutes", 30) * 60)
            if tokens["expires_at"] < now + 300:
                body = self._request("POST", TOKEN_URL, data={
                    "client_id": settings["client_id"],
                    "client_secret": settings["client_secret"],
                    "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                })
                tokens = self._tokens(body, tokens.get("athlete_id"))
                self.store.put("strava_tokens", tokens)
            headers = {"Authorization": f"Bearer {tokens['access_token']}"}
            athlete_id = tokens.get("athlete_id")
            if not athlete_id:
                athlete_id = self._request("GET", API + "/athlete", headers=headers).get("id")
                if type(athlete_id) is not int or athlete_id < 1:
                    raise StravaError("Strava did not identify the authorized athlete")
                tokens["athlete_id"] = athlete_id
                self.store.put("strava_tokens", tokens)
            raw = self._request("GET", f"{API}/athletes/{athlete_id}/stats", headers=headers)
            stats = {}
            for mode, field in FIELDS.items():
                source = raw.get(field)
                if not isinstance(source, dict):
                    raise StravaError(f"Strava response is missing {field}")
                stats[mode] = {}
                for key in ("count", "distance", "moving_time", "elevation_gain"):
                    value = source.get(key)
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise StravaError(f"Invalid Strava statistic: {key}")
                    stats[mode][key] = value
            cache = {"fetched_at": now, "stats": stats}
            self.store.put("strava_cache", cache)
            return cache
