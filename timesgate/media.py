from __future__ import annotations

import io
from pathlib import Path
import time
import uuid

from PIL import GifImagePlugin, Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD = 8 * 1024 * 1024
MAX_FRAMES = 300


def opaque(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    return Image.alpha_composite(Image.new("RGBA", rgba.size, "black"), rgba).convert("RGB")


def encode_full_frames(frames: list[Image.Image], durations: list[int]) -> bytes:
    if not frames or len(frames) != len(durations):
        raise ValueError("Animation requires matching frames and durations")
    output = io.BytesIO()
    indexed = [opaque(frame).quantize(colors=256) for frame in frames]
    header, _ = GifImagePlugin.getheader(indexed[0], info={"loop": 0, "background": 0})
    for chunk in header:
        output.write(chunk)
    for index, (frame, duration) in enumerate(zip(indexed, durations)):
        # getdata writes the complete descriptor, avoiding Pillow's delta-frame optimization.
        for chunk in GifImagePlugin.getdata(
            frame, offset=(0, 0), duration=max(20, duration), disposal=1,
            include_color_table=index > 0,
        ):
            output.write(chunk)
    output.write(b";")
    return output.getvalue()


def normalize(blob: bytes) -> tuple[bytes, int, int]:
    if not blob or len(blob) > MAX_UPLOAD:
        raise ValueError("Upload an image/GIF of at most 8 MiB")
    frames, durations = [], []
    started = time.monotonic()
    try:
        with Image.open(io.BytesIO(blob)) as image:
            if image.format not in ("GIF", "PNG", "JPEG", "WEBP"):
                raise ValueError("Supported formats: GIF, PNG, JPEG, WebP")
            if image.width * image.height > 4_000_000:
                raise ValueError("Maximum source canvas is 4 megapixels")
            count = getattr(image, "n_frames", 1)
            if count > MAX_FRAMES:
                raise ValueError("Maximum animation length is 300 frames")
            for index in range(count):
                if time.monotonic() - started > 30:
                    raise ValueError("Image conversion exceeded 30 seconds")
                image.seek(index)
                frame = ImageOps.exif_transpose(image.copy())
                frames.append(ImageOps.pad(
                    opaque(frame), (128, 128), method=Image.Resampling.LANCZOS, color="black"
                ))
                durations.append(max(20, int(image.info.get("duration", 100) or 100)))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("Image is corrupt, unsupported or too large") from None
    return encode_full_frames(frames, durations), len(frames), sum(durations)


def publish(directory: Path, blob: bytes) -> str:
    name = f"{uuid.uuid4().hex}.gif"
    temporary = directory / f".{name}.tmp"
    try:
        temporary.write_bytes(blob)
        temporary.chmod(0o644)
        temporary.replace(directory / name)
    finally:
        temporary.unlink(missing_ok=True)
    return name
