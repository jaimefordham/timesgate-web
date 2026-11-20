from __future__ import annotations

from datetime import datetime
from pathlib import Path
from string import Formatter
import textwrap
from typing import Mapping
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .models import METRICS, WIDGET_ACCENTS, Screen
from .widgets import Weather


def font(size: int):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def wrapped(draw: ImageDraw.ImageDraw, text: str, size: int, width: int) -> list[str]:
    lines = []
    face = font(size)
    for paragraph in text.splitlines() or [""]:
        line = ""
        for character in paragraph:
            if line and draw.textlength(line + character, font=face) > width:
                lines.append(line)
                line = ""
            line += character
        lines.append(line)
    return lines


def custom_image(screen: Screen, now: datetime, metrics: Mapping[str, float | None],
                 media_dir: Path | None) -> Image.Image:
    image = Image.new("RGB", (128, 128), screen.background)
    values = {
        key: "N/A" if metrics.get(key) is None else f"{metrics[key]:.1f}" for key in METRICS
    }
    values.update(time=now.strftime("%H:%M"), date=now.strftime("%d/%m/%Y"))
    for layer in screen.elements:
        tile = Image.new("RGBA", (layer.width, layer.height))
        draw = ImageDraw.Draw(tile)
        if layer.type == "text":
            text = Formatter().vformat(layer.text, (), values)
            for index, line in enumerate(wrapped(draw, text, layer.size, layer.width)):
                y = index * (layer.size + 2)
                if y >= layer.height:
                    break
                draw.text((0, y), line, fill=layer.color, font=font(layer.size))
        elif layer.type == "bar":
            value = layer.value if layer.metric == "value" else metrics.get(layer.metric)
            if value is None:
                draw.text((0, 0), "N/A", fill=layer.color, font=font(10))
            else:
                draw.rectangle((0, 0, layer.width - 1, layer.height - 1), outline=layer.color)
                fill = round(layer.width * max(0, min(1, value / layer.maximum)))
                if fill:
                    draw.rectangle((0, 0, fill - 1, layer.height - 1), fill=layer.color)
        else:
            if media_dir is None or not layer.media_id:
                raise ValueError("Choose existing uploaded media for image layers")
            try:
                with Image.open(media_dir / (layer.media_id + ".gif")) as source:
                    tile = ImageOps.fit(source.convert("RGBA"), tile.size, Image.Resampling.LANCZOS)
            except OSError:
                raise ValueError("Custom image layer media is missing or unreadable") from None
        image.paste(tile, (layer.x, layer.y), tile)
    return image


def weather_label(code: int) -> str:
    if code == 0:
        return "CLEAR"
    if code <= 3:
        return "CLOUDY"
    if code in (45, 48):
        return "FOG"
    if code in (71, 73, 75, 77, 85, 86):
        return "SNOW"
    if code >= 95:
        return "THUNDERSTORM"
    return "RAIN / DRIZZLE"


def render(screen: Screen, timezone: str, cache: dict | None = None,
           now: datetime | None = None, *, weather: Weather | None = None,
           headline: str | None = None, metrics: Mapping[str, float | None] | None = None,
           media_dir: Path | None = None, preview: bool = False) -> Image.Image:
    now = now or datetime.now(ZoneInfo(timezone))
    if screen.kind == "custom":
        return custom_image(screen, now, metrics or {}, media_dir)
    image = Image.new("RGB", (128, 128), screen.background)
    draw = ImageDraw.Draw(image)
    accent = "#fc4c02" if screen.kind == "strava" else "#65e0c8"
    if screen.accent != "original":
        accent = WIDGET_ACCENTS[screen.accent]["color"]
    title = screen.title or {
        "clock": "LOCAL TIME", "text": "NOTE", "strava": "STRAVA", "weather": "WEATHER",
        "countdown": "COUNTDOWN", "rss": "NEWS",
    }.get(screen.kind, "")
    draw.text((6, 5 if screen.kind == "strava" else 6), title[:18],
              fill=accent, font=font(9 if screen.kind == "strava" else 10))
    line_y = 20 if screen.kind == "strava" else 23
    draw.line((6, line_y, 121, line_y), fill=accent)
    if screen.kind == "clock":
        draw.text((64, 59), now.strftime("%H:%M"), anchor="mm", fill="white", font=font(32))
        date_font = font(12)
        prefix, day, suffix = now.strftime("%a "), now.strftime("%d"), now.strftime(" %b")
        date = prefix + day + suffix
        x = 64 - draw.textlength(date, font=date_font) / 2
        draw.text((x, 91), prefix, anchor="lm", fill="#b9c9d6", font=date_font)
        draw.text((x + draw.textlength(prefix, font=date_font), 91), day,
                  anchor="lm", fill=accent, font=date_font)
        draw.text((x + draw.textlength(prefix + day, font=date_font), 91), suffix,
                  anchor="lm", fill="#b9c9d6", font=date_font)
        draw.text((64, 115), "Updated by controller", anchor="mm", fill="#9aabbc", font=font(8))
    elif screen.kind == "text":
        lines = []
        for paragraph in screen.text.splitlines():
            lines.extend(textwrap.wrap(paragraph, width=15) or [""])
        if len(lines) > 6:
            lines = lines[:6]
            lines[-1] = lines[-1][:12] + "..."
        for index, line in enumerate(lines):
            draw.text((6, 30 + index * 15), line, fill="white", font=font(12))
    elif screen.kind == "weather":
        if weather is None:
            raise ValueError("Weather data has not been loaded")
        temperature = weather.temperature
        if screen.units == "imperial":
            temperature = temperature * 9 / 5 + 32
        draw.text((64, 53), f"{temperature:.0f}°{'F' if screen.units == 'imperial' else 'C'}",
                  anchor="mm", fill="white", font=font(30))
        draw.text((64, 86), weather_label(weather.code), anchor="mm", fill=accent, font=font(10))
        draw.text((64, 110), f"Humidity {weather.humidity:.0f}%",
                  anchor="mm", fill="white", font=font(11))
    elif screen.kind == "countdown":
        target = datetime.fromisoformat(screen.target)
        if target.tzinfo is None:
            target = target.replace(tzinfo=ZoneInfo(timezone))
        seconds = max(0, int(target.timestamp() - now.timestamp()))
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds_left = divmod(remainder, 60)
        value = f"{days}d {hours}h" if days else (
            f"{hours:02}:{minutes:02}" if hours else f"{minutes:02}:{seconds_left:02}"
        )
        size = 30
        while size > 10 and draw.textlength(value, font=font(size)) > 116:
            size -= 1
        draw.text((64, 60), value, anchor="mm", fill="white", font=font(size))
        draw.text((64, 100), "FINISHED" if seconds == 0 else "TIME REMAINING",
                  anchor="mm", fill=accent, font=font(10))
    elif screen.kind == "rss":
        if headline is None:
            raise ValueError("Feed headlines have not been loaded")
        lines = wrapped(draw, headline, screen.news_size, 116)
        count = max(1, 94 // (screen.news_size + 2))
        for index, line in enumerate(lines[:count]):
            if index == count - 1 and len(lines) > count:
                line = line[:-3] + "..."
            draw.text((6, 28 + index * (screen.news_size + 2)), line,
                      fill="white", font=font(screen.news_size))
    elif screen.kind == "strava":
        example = not cache and preview
        if not cache and not example:
            raise ValueError("No cached Strava statistics; connect and refresh Strava first")
        if cache:
            stats = cache["stats"][screen.strava_mode]
        else:
            stats = {
                "ytd": {
                    "distance": 1250000, "count": 42,
                    "moving_time": 180000, "elevation_gain": 12500,
                },
                "all-time": {
                    "distance": 8400000, "count": 280,
                    "moving_time": 1260000, "elevation_gain": 84000,
                },
            }[screen.strava_mode]
        imperial = screen.units == "imperial"
        distance = stats["distance"] / (1609.344 if imperial else 1000)
        hours = int(stats["moving_time"]) // 3600
        draw.text((122, 6), "YTD" if screen.strava_mode == "ytd" else "ALL",
                  anchor="ra", fill="#b9c9d6", font=font(8))
        for x, y, value, size, width in (
            (64, 42, f"{distance:,.0f}", 31, 116),
            (32, 93, f"{stats['count']:,.0f}", 20, 50),
            (95, 93, f"{hours:,}h", 20, 58),
        ):
            while size > 6 and draw.textlength(value, font=font(size)) > width:
                size -= 1
            draw.text((x, y), value, anchor="mm", fill="white", font=font(size))
        draw.text((64, 64), "mi cycled" if imperial else "km cycled",
                  anchor="mm", fill="#b9c9d6", font=font(9))
        draw.line((6, 76, 121, 76), fill="#283951")
        draw.line((62, 83, 62, 110), fill="#283951")
        draw.text((32, 109), "RIDES", anchor="mm", fill="#b9c9d6", font=font(8))
        draw.text((95, 109), "MOVING", anchor="mm", fill="#b9c9d6", font=font(8))
        if cache:
            fetched = datetime.fromtimestamp(cache["fetched_at"], ZoneInfo(timezone))
            footer = fetched.strftime("Data %d/%m %H:%M")
        else:
            footer = "EXAMPLE DATA"
        draw.text((64, 122), footer, anchor="mm",
                  fill=accent if example else "#b9c9d6", font=font(7))
    else:
        raise ValueError("This content type does not have a generated preview")
    return image
