from __future__ import annotations

from datetime import datetime, timedelta
import ipaddress
import json
from string import Formatter
import uuid
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


METRICS = ("cpu", "ram", "disk", "gpu", "cpu_temp", "gpu_temp", "download", "upload")

WIDGET_ACCENTS = {
    "electric-cyan": {"name": "Electric Cyan", "color": "#36D9FF"},
    "neon-mint": {"name": "Neon Mint", "color": "#55E6B0"},
    "acid-lime": {"name": "Acid Lime", "color": "#B7F34A"},
    "solar-gold": {"name": "Solar Gold", "color": "#FFD166"},
    "tangerine": {"name": "Tangerine", "color": "#FF8A4C"},
    "hot-pink": {"name": "Hot Pink", "color": "#FF6FAE"},
    "ultraviolet": {"name": "Ultraviolet", "color": "#B99AFF"},
    "glacier-blue": {"name": "Glacier Blue", "color": "#72B7FF"},
}


def widget_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https") or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.fragment or any(ord(char) <= 32 or ord(char) >= 127 for char in value)
    ):
        raise ValueError("Use a public HTTP(S) URL without credentials or a fragment")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid URL port")
    return value


class DesignLayer(Model):
    type: Literal["text", "image", "bar"] = "text"
    x: StrictInt = Field(default=8, ge=0, le=127)
    y: StrictInt = Field(default=8, ge=0, le=127)
    width: StrictInt = Field(default=112, ge=1, le=128)
    height: StrictInt = Field(default=24, ge=1, le=128)
    size: StrictInt = Field(default=16, ge=6, le=64)
    color: str = Field(default="#ffffff", pattern=r"^#[a-fA-F0-9]{6}$")
    text: str = Field(default="", max_length=300)
    media_id: str = Field(default="", pattern=r"^(?:[a-f0-9]{32})?$")
    metric: Literal[
        "value", "cpu", "ram", "disk", "gpu", "cpu_temp", "gpu_temp", "download", "upload"
    ] = "value"
    value: float = Field(default=50, allow_inf_nan=False)
    maximum: float = Field(default=100, gt=0, allow_inf_nan=False)

    @field_validator("text")
    @classmethod
    def valid_fields(cls, value: str) -> str:
        for _, name, spec, conversion in Formatter().parse(value):
            if name is not None and (name not in ("time", "date", *METRICS) or spec or conversion):
                raise ValueError("Use only {time}, {date} or a supported metric field")
        return value


class Screen(Model):
    kind: Literal[
        "unmanaged", "media", "clock", "text", "strava",
        "weather", "countdown", "rss", "custom",
    ] = "unmanaged"
    enabled: bool = False
    title: str = Field(default="", max_length=40)
    accent: str = "original"
    text: str = Field(default="", max_length=300)
    media_id: str = ""
    refresh_seconds: StrictInt = Field(default=60, ge=10, le=604800)
    schedule: Literal["interval", "daily", "on_time_change"] = "interval"
    daily_time: str = Field(default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    strava_mode: Literal["ytd", "all-time"] = "ytd"
    units: Literal["metric", "imperial"] = "metric"
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    target: str = Field(default="", max_length=50)
    url: str = Field(default="", max_length=2048)
    news_seconds: StrictInt = Field(default=15, ge=10, le=3600)
    news_size: StrictInt = Field(default=13, ge=8, le=32)
    background: str = Field(default="#08121e", pattern=r"^#[a-fA-F0-9]{6}$")
    elements: list[DesignLayer] = Field(default_factory=list, max_length=20)

    @field_validator("accent")
    @classmethod
    def valid_accent(cls, value: str) -> str:
        if value != "original" and value not in WIDGET_ACCENTS:
            raise ValueError("Choose Original or a supported widget accent")
        return value

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        return widget_url(value) if value else value

    @field_validator("target")
    @classmethod
    def valid_target(cls, value: str) -> str:
        if value:
            if "T" not in value and " " not in value:
                raise ValueError("Choose a date and time, not just a date")
            datetime.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def configured(self) -> Screen:
        if self.schedule == "on_time_change" and self.kind != "clock":
            raise ValueError("On time change is only available for clock screens")
        if self.enabled and self.kind == "unmanaged":
            raise ValueError("Choose content before enabling its schedule")
        if self.kind == "media" and self.enabled and not self.media_id:
            raise ValueError("Choose media before enabling its schedule")
        if self.kind == "weather" and (self.latitude is None or self.longitude is None):
            raise ValueError("Weather requires latitude and longitude")
        if self.kind == "countdown" and not self.target:
            raise ValueError("Choose a countdown target date and time")
        if self.kind == "rss" and not self.url:
            raise ValueError("Enter an RSS or Atom feed URL")
        if self.kind == "custom" and any(
            layer.type == "image" and not layer.media_id for layer in self.elements
        ):
            raise ValueError("Choose uploaded media for each image layer")
        return self


class NightMode(Model):
    enabled: StrictBool = False
    start: str = Field(default="22:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(default="07:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    mode: Literal["dim", "sleep"] = "dim"
    brightness: StrictInt = Field(default=10, ge=0, le=100)
    day_brightness: StrictInt = Field(default=70, ge=0, le=100)

    @model_validator(mode="after")
    def distinct_times(self) -> NightMode:
        if self.enabled and self.start == self.end:
            raise ValueError("Night mode start and end must differ")
        return self


class Device(Model):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    name: str = Field(default="Times Gate", min_length=1, max_length=60)
    host: str = ""
    hardware: Literal[400, 402] = 402
    local_token: StrictInt | None = Field(default=None, ge=1, le=2147483647)
    local_token_set: bool = Field(default=False, exclude=True)
    night: NightMode = Field(default_factory=NightMode)
    screens: list[Screen] = Field(
        default_factory=lambda: [Screen() for _ in range(5)], min_length=5, max_length=5
    )

    @field_validator("host")
    @classmethod
    def private_host(cls, value: str) -> str:
        if not value:
            return ""
        ip = ipaddress.IPv4Address(value.strip())
        networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        if not any(ip in ipaddress.IPv4Network(network) for network in networks):
            raise ValueError("Use the device's private IPv4 address")
        return str(ip)

    @field_validator("hardware", mode="before")
    @classmethod
    def integer_hardware(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Hardware must be the integer 400 or 402")
        return value


class Configuration(Model):
    timezone: str = "Europe/Lisbon"
    devices: list[Device] = Field(default_factory=lambda: [Device()], min_length=1, max_length=10)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("Unknown IANA timezone") from None
        return value

    @model_validator(mode="after")
    def unique_devices(self) -> Configuration:
        if len({device.id for device in self.devices}) != len(self.devices):
            raise ValueError("Device IDs must be unique")
        hosts = [device.host for device in self.devices if device.host]
        if len(set(hosts)) != len(hosts):
            raise ValueError("Configure each device IP only once")
        return self


class SendRequest(Model):
    device_id: str
    panel: StrictInt = Field(ge=0, le=4)


class LightingSettings(Model):
    color: str = Field(default="#55e6b0", pattern=r"^#[a-fA-F0-9]{6}$")
    brightness: StrictInt = Field(default=50, ge=0, le=100)
    zone: StrictInt = Field(default=2, ge=0, le=2)
    effect: StrictInt = Field(default=5, ge=0, le=11)
    on: StrictBool = True
    cycle: StrictBool = False
    keys: StrictBool = True


class LightingRequest(Model):
    device_id: str
    settings: LightingSettings


class LightingRestoreRequest(Model):
    device_id: str
    enabled: StrictBool


class ProfileName(Model):
    name: str = Field(min_length=1, max_length=40)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a profile name")
        return value


class DeviceProfile(ProfileName):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    device_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    screens: list[Screen] = Field(min_length=5, max_length=5)
    night: NightMode
    lighting: LightingSettings


class ConfigUpdate(Model):
    config: Configuration
    revision: str
    profiles: list[DeviceProfile] = Field(default_factory=list, max_length=10)


class LightingPreset(Model):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=40)
    settings: LightingSettings

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a preset name")
        return value


class BrightnessControl(Model):
    action: Literal["brightness"]
    value: StrictInt = Field(ge=0, le=100)


class PowerControl(Model):
    action: Literal["power"]
    on: StrictBool


class PreferencesControl(Model):
    action: Literal["preferences"]
    hour24: StrictBool
    celsius: StrictBool
    mirror: StrictBool


class TimerControl(Model):
    action: Literal["timer"]
    minutes: StrictInt = Field(default=25, ge=0, le=999)
    seconds: StrictInt = Field(default=0, ge=0, le=59)
    running: StrictBool = True


class StopwatchControl(Model):
    action: Literal["stopwatch"]
    state: Literal["start", "stop", "reset"]


class ScoreboardControl(Model):
    action: Literal["scoreboard"]
    red: StrictInt = Field(ge=0, le=999)
    blue: StrictInt = Field(ge=0, le=999)


class NoiseControl(Model):
    action: Literal["noise"]
    on: StrictBool


class SimpleControl(Model):
    action: Literal["sync_time", "read_settings", "reboot", "resume", "buzzer"]


TAKEOVER_CONTROLS = ("timer", "stopwatch", "scoreboard", "noise")


class ControlRequest(Model):
    device_id: str
    control: Annotated[
        BrightnessControl | PowerControl | PreferencesControl | TimerControl
        | StopwatchControl | ScoreboardControl | NoiseControl | SimpleControl,
        Field(discriminator="action"),
    ]
    confirmed: StrictBool = False

    @model_validator(mode="after")
    def confirm_disruption(self) -> ControlRequest:
        if self.control.action in (*TAKEOVER_CONTROLS, "reboot") and not self.confirmed:
            raise ValueError("Confirm this disruptive device control first")
        return self


class StravaSettings(Model):
    client_id: str = Field(pattern=r"^\d{1,20}$")
    client_secret: str | None = Field(default=None, min_length=1, max_length=200)
    poll_minutes: StrictInt = Field(default=30, ge=15, le=1440)


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def http_base(value: str, *, http_only: bool) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in (("http",) if http_only else ("http", "https"))
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or any(ord(char) <= 32 or ord(char) >= 127 for char in value)
    ):
        raise ValueError("Base URL must be an origin such as http://NAS_IP:8081")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid URL port")
    return value.rstrip("/")


def next_due(screen: Screen, timezone: str, now: float) -> float:
    if screen.schedule == "interval":
        return now + screen.refresh_seconds
    local = datetime.fromtimestamp(now, ZoneInfo(timezone))
    if screen.schedule == "on_time_change":
        return local.replace(second=0, microsecond=0).timestamp() + 60
    hour, minute = map(int, screen.daily_time.split(":"))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()
