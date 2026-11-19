from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .lighting import target_signature
from .models import ControlRequest, Device, NightMode, Screen, canonical, next_due


def control_commands(request: ControlRequest, device: Device, timezone: str,
                     now: float) -> list[dict]:
    control = request.control
    match control.action:
        case "brightness":
            return [{"Command": "Channel/SetBrightness", "Brightness": control.value}]
        case "power":
            return [{"Command": "Channel/OnOffScreen", "OnOff": int(control.on)}]
        case "preferences":
            return [
                {"Command": "Device/SetTime24Flag", "Mode": int(control.hour24)},
                {"Command": "Device/SetDisTempMode", "Mode": 0 if control.celsius else 1},
                {"Command": "Device/SetMirrorMode", "Mode": int(control.mirror)},
            ]
        case "sync_time":
            offset = datetime.fromtimestamp(now, ZoneInfo(timezone)).utcoffset()
            if offset is None:
                raise ValueError("Cannot determine the configured timezone offset")
            hours = offset.total_seconds() / 3600
            return [
                {"Command": "Device/SetUTC", "Utc": int(now)},
                {"Command": "Sys/TimeZone", "TimeZoneValue": f"GMT{hours:+g}"},
            ]
        case "read_settings":
            return [
                {"Command": "Channel/GetClockInfo", "LcdIndex": 0, "LcdIndependence": 0}
                if device.hardware == 402 else {"Command": "Channel/GetAllConf"}
            ]
        case "timer":
            return [{
                "Command": "Tools/SetTimer", "Minute": control.minutes if control.running else 0,
                "Second": control.seconds if control.running else 0, "Status": int(control.running),
            }]
        case "stopwatch":
            return [{"Command": "Tools/SetStopWatch", "Status": {"start": 1, "stop": 0, "reset": 2}[control.state]}]
        case "scoreboard":
            return [{"Command": "Tools/SetScoreBoard", "RedScore": control.red, "BlueScore": control.blue}]
        case "noise":
            return [{"Command": "Tools/SetNoiseStatus", "NoiseStatus": int(control.on)}]
        case "buzzer":
            return [{
                "Command": "Device/PlayBuzzer", "ActiveTimeInCycle": 150,
                "OffTimeInCycle": 150, "PlayTotalTime": 600,
            }]
        case "reboot":
            return [{"Command": "Device/SysReboot"}]
        case "resume":
            return []
    raise ValueError("Unsupported device control")


def diagnostic_values(body: dict) -> dict:
    return {
        key: body[key] for key, maximum in (("Brightness", 100), ("LightSwitch", 1))
        if type(body.get(key)) is int and 0 <= body[key] <= maximum
    }


def night_signature(device: Device, timezone: str) -> str:
    return canonical({
        "target": target_signature(device), "night": device.night.model_dump(), "timezone": timezone,
    })


def night_phase(settings: NightMode, timezone: str, now: float) -> str:
    local = datetime.fromtimestamp(now, ZoneInfo(timezone))
    start_hour, start_minute = map(int, settings.start.split(":"))
    end_hour, end_minute = map(int, settings.end.split(":"))
    for days_ago in (0, 1):
        day = local - timedelta(days=days_ago)
        start = day.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0, fold=0)
        end = day.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0, fold=0)
        if settings.end < settings.start:
            end += timedelta(days=1)
        # Compare instants so the repeated DST hour cannot end a window early.
        if start.timestamp() <= now < end.timestamp():
            return "start"
    return "end"


def night_next(settings: NightMode, phase: str, timezone: str, now: float) -> float:
    clock = settings.start if phase == "start" else settings.end
    return next_due(Screen(kind="clock", schedule="daily", daily_time=clock), timezone, now)


def night_commands(settings: NightMode, phase: str) -> list[dict]:
    if settings.mode == "sleep" and phase == "start":
        return [{"Command": "Channel/OnOffScreen", "OnOff": 0}]
    brightness = settings.brightness if phase == "start" else settings.day_brightness
    commands = [{"Command": "Channel/SetBrightness", "Brightness": brightness}]
    if settings.mode == "sleep":
        commands.append({"Command": "Channel/OnOffScreen", "OnOff": 1})
    return commands
