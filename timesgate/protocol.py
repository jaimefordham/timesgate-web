from __future__ import annotations

import base64
from urllib.parse import urlsplit
import uuid

import requests

from .models import Device, Screen, WIDGET_ACCENTS


class DeviceError(RuntimeError):
    pass


def validate_media_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname or len(url.encode()) >= 256:
        raise ValueError("Device media URL must be HTTP and shorter than 256 bytes")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Device media URLs cannot contain credentials")


def play_gif(device: Device, panel: int, url: str, session=requests) -> None:
    if not device.host or panel not in range(5):
        raise ValueError("Configure a device IP and screen 1-5")
    if device.hardware == 402 and device.local_token is None:
        raise ValueError("Hardware 402 requires the LocalToken from device settings")
    validate_media_url(url)
    payload = {"Command": "Device/PlayGif",
               "LcdArray": [int(index == panel) for index in range(5)], "FileName": [url]}
    device_command(device, payload, session)


def send_jpeg(device: Device, panel: int, jpeg: bytes, session=requests) -> None:
    if panel not in range(5):
        raise ValueError("Choose screen 1-5")
    device_command(device, {
        "Command": "Draw/SendHttpGif",
        "LcdArray": [int(index == panel) for index in range(5)],
        "PicNum": 1, "PicOffset": 0, "PicID": uuid.uuid4().int % 2147483647 + 1,
        "PicSpeed": 1000, "PicWidth": 128,
        "PicData": base64.b64encode(jpeg).decode("ascii"),
    }, session)


def native_clock_payload(device: Device, panel: int, screen: Screen, background: str) -> dict:
    if panel not in range(5) or screen.kind != "clock":
        raise ValueError("Choose a clock screen 1-5")
    validate_media_url(background)
    accent = "#65e0c8" if screen.accent == "original" else WIDGET_ACCENTS[screen.accent]["color"]
    items = []
    for text_id, kind, y, height, color in (
        (0, 22, 6, 18, accent), (1, 5, 42, 36, "#ffffff"), (2, 12, 86, 24, "#b9c9d6"),
    ):
        items.append({
            "TextId": text_id, "type": kind, "x": 6, "y": y, "dir": 0,
            "font": 2, "TextWidth": 116, "Textheight": height,
            "speed": 100, "align": 2 if device.hardware == 400 else 3,
            "color": color, "TextString": (screen.title or "LOCAL TIME")[:18] if kind == 22 else "",
        })
    return {
        "Command": "Draw/SendHttpItemList", "LcdIndex": panel, "NewFlag": 1,
        "BackgroudGif": background, "ItemList": items,
    }


def device_command(device: Device, payload: dict, session=requests) -> dict:
    if not device.host:
        raise ValueError("Configure a device IP")
    if device.hardware == 402 and device.local_token is None:
        raise ValueError("Hardware 402 requires the LocalToken from device settings")
    endpoint = (f"http://{device.host}:9000/divoom_api" if device.hardware == 402
                else f"http://{device.host}/post")
    payload = dict(payload)
    if device.local_token is not None:
        payload["LocalToken"] = device.local_token
    if device.hardware == 402:
        payload["ReturnCode"] = 0
    try:
        response = session.post(endpoint, json=payload, timeout=(5, 45), allow_redirects=False)
    except requests.RequestException:
        raise DeviceError("Device connection failed or timed out; effect unknown. No retry made.") from None
    if not 200 <= response.status_code < 300:
        raise DeviceError(f"Device HTTP {response.status_code}; effect unknown. No retry made.")
    try:
        body = response.json()
    except ValueError:
        raise DeviceError("Device returned invalid JSON; effect unknown. No retry made.") from None
    codes = [body[key] for key in ("ReturnCode", "error_code") if key in body] if isinstance(body, dict) else []
    if not codes or any(type(code) is not int or code != 0 for code in codes):
        timeout = isinstance(body, dict) and body.get("ReturnMessage") == "command timeout"
        detail = "command timeout" if timeout else "command rejected or missing success code"
        raise DeviceError(f"Device {detail}; effect unknown. No retry made.")
    return body
