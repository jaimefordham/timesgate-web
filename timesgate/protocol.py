from __future__ import annotations

from urllib.parse import urlsplit

import requests

from .models import Device


class DeviceError(RuntimeError):
    pass


def play_gif(device: Device, panel: int, url: str, session=requests) -> None:
    if not device.host or panel not in range(5):
        raise ValueError("Configure a device IP and screen 1-5")
    if device.hardware == 402 and device.local_token is None:
        raise ValueError("Hardware 402 requires the LocalToken from device settings")
    parsed = urlsplit(url)
    if parsed.scheme != "http" or not parsed.hostname or len(url.encode()) >= 256:
        raise ValueError("Device media URL must be HTTP and shorter than 256 bytes")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Device media URLs cannot contain credentials")
    payload = {"Command": "Device/PlayGif",
               "LcdArray": [int(index == panel) for index in range(5)], "FileName": [url]}
    device_command(device, payload, session)


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
