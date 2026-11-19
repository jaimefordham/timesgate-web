from __future__ import annotations

import hashlib

from .models import Device, LightingSettings, canonical

# Zone/effect behaviour follows the community mapping referenced by the source
# controller; firmware can change the visible result.
EDGE_EFFECTS = [
    {"name": "Pastel sparkle", "description": "A slowly rising multicolour pattern."},
    {"name": "Pendulum", "description": "Alternating warm red and orange light."},
    {"name": "Rainbow", "description": "Multiple colours appear and fade."},
    {"name": "Colour fade", "description": "The lights change hue together."},
    {"name": "Breathing", "description": "The selected colour brightens and dims."},
    {"name": "Steady light", "description": "No movement; edge colour may be fixed by firmware."},
    {"name": "Waves", "description": "Rainbow movement alternates between the sides."},
    {"name": "Rainbow rain", "description": "Multicolour flashes fade away."},
    {"name": "Side sweep", "description": "The selected colour moves across the edges."},
    {"name": "Cascade", "description": "Light travels down both sides."},
    {"name": "Rocket", "description": "A rotating rainbow travels across the device."},
    {"name": "Colour wheel", "description": "The selected colour moves towards the front."},
]
BACK_EFFECTS = [
    {"name": "Static rainbow", "description": "A stationary multicolour gradient."},
    {"name": "Soft fire", "description": "Warm reds and oranges alternate."},
    {"name": "Pendulum", "description": "Red, yellow and green lights appear in sequence."},
    {"name": "Rainbow fade", "description": "A gradual blend through rainbow colours."},
    {"name": "Pulse", "description": "The rear light breathes in the selected colour."},
    {"name": "Solid colour", "description": "The selected colour stays on without movement."},
    {"name": "Chase", "description": "Bright points travel across the rear light."},
    {"name": "Back and forth", "description": "Light moves from the ends towards the centre."},
    {"name": "Radar", "description": "A sweep travels across and returns."},
    {"name": "Waves", "description": "The rear lights brighten and fade together."},
    {"name": "Rain", "description": "Scattered flashes use the selected colour."},
    {"name": "Sliding glow", "description": "A moving glow fades in gently."},
]


def target_signature(device: Device) -> str:
    return hashlib.sha256(canonical({
        "host": device.host, "hardware": device.hardware, "token": device.local_token,
    }).encode()).hexdigest()


def lighting_approval(saved: dict) -> str:
    return saved.get("approval", str(saved["applied_at"]))


def lighting_payload(settings: LightingSettings) -> dict:
    effects = [{"SelectEffect": 0} for _ in range(3)]
    effects[settings.zone]["SelectEffect"] = settings.effect
    return {
        "Command": "Channel/SetRGBInfo",
        "Color": settings.color.lower(),
        "Brightness": settings.brightness,
        "SelectLightIndex": settings.zone,
        "LightList": effects,
        "OnOff": int(settings.on),
        "ColorCycle": int(settings.cycle),
        "KeyOnOff": int(settings.keys),
    }
