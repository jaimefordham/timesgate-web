from __future__ import annotations

from dataclasses import dataclass
import html
from http.client import HTTPConnection, HTTPSConnection, HTTPException, HTTPResponse
import ipaddress
import json
import math
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
from typing import Callable, TypeVar, cast
from urllib.parse import urlencode, urljoin, urlsplit

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
import psutil

from .models import METRICS, widget_url

T = TypeVar("T")
MAX_RESPONSE = 2 * 1024 * 1024


def fetch_public(url: str) -> bytes:
    deadline = time.monotonic() + 12
    for _ in range(4):
        parsed = urlsplit(widget_url(url))
        host = parsed.hostname
        if host is None:
            raise ValueError("Widget URL has no hostname")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        connection: HTTPConnection | None = None
        response: HTTPResponse | None = None
        try:
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            if not addresses or any(
                not ipaddress.ip_address(item[4][0]).is_global for item in addresses
            ):
                raise ValueError("Widget URLs must resolve only to public Internet addresses")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Widget request exceeded its time limit")
            address = addresses[0][4][0]
            connection = (
                HTTPSConnection(host, port, timeout=min(6, remaining))
                if parsed.scheme == "https" else HTTPConnection(host, port, timeout=min(6, remaining))
            )
            # Connect to the checked address, retaining the hostname for TLS and Host.
            connection.sock = socket.create_connection((address, port), timeout=min(6, remaining))
            if parsed.scheme == "https":
                connection.sock = ssl.create_default_context().wrap_socket(
                    connection.sock, server_hostname=host,
                )
            transport = connection.sock
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request("GET", path, headers={
                "User-Agent": "Timesgate-Web/0.1", "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Widget source returned a redirect without a location")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f"Widget source returned HTTP {response.status}")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("Widget source returned unsupported compressed content")
            chunks, length = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("Widget request exceeded its time limit")
                transport.settimeout(min(6, remaining))
                chunk = response.read1(min(65536, MAX_RESPONSE + 1 - length))
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                length += len(chunk)
                if length > MAX_RESPONSE:
                    raise ValueError("Widget source exceeds 2 MiB")
        except (OSError, HTTPException):
            raise ValueError("Could not fetch widget source; check the URL and network") from None
        finally:
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
    raise ValueError("Widget source redirected too many times")


@dataclass(frozen=True)
class Weather:
    temperature: float
    humidity: float
    code: int


def feed_titles(blob: bytes) -> list[str]:
    try:
        root = ElementTree.fromstring(blob)
    except (ElementTree.ParseError, DefusedXmlException):
        raise ValueError("Feed is not valid, safe RSS or Atom XML") from None
    if root.tag.rsplit("}", 1)[-1] not in ("rss", "feed", "RDF"):
        raise ValueError("Source is not an RSS or Atom feed")
    titles = []
    for entry in root.iter():
        if entry.tag.rsplit("}", 1)[-1] not in ("item", "entry"):
            continue
        title = next((child for child in entry if child.tag.rsplit("}", 1)[-1] == "title"), None)
        if title is not None:
            text = html.unescape("".join(title.itertext()))
            text = re.sub(r"<[^>]*>", "", text)
            text = " ".join(text.split())[:500]
            if text:
                titles.append(text)
        if len(titles) == 50:
            break
    if not titles:
        raise ValueError("Feed contains no headlines")
    return titles


class WidgetSources:
    def __init__(self, *, demo: bool = False):
        self.demo = demo
        self.lock = threading.Lock()
        self.cache: dict[tuple, tuple[float, object]] = {}
        self.network_previous: tuple[float, int, int] | None = None
        self.nvidia = shutil.which("nvidia-smi")
        psutil.cpu_percent()

    def cached(self, key: tuple, seconds: int, loader: Callable[[], T]) -> T:
        # Previews and the scheduler share one cache, including short-lived failures.
        with self.lock:
            cached = self.cache.get(key)
            if cached and time.monotonic() < cached[0]:
                value = cached[1]
                if isinstance(value, ValueError):
                    raise ValueError(str(value))
                return cast(T, value)
            if len(self.cache) >= 64 and key not in self.cache:
                self.cache.pop(next(iter(self.cache)))
            try:
                value = loader()
            except ValueError as error:
                self.cache[key] = (time.monotonic() + 30, error)
                raise
            self.cache[key] = (time.monotonic() + seconds, value)
            return value

    def weather(self, latitude: float, longitude: float) -> Weather:
        if self.demo:
            return Weather(23, 48, 1)

        def load() -> Weather:
            url = "https://api.open-meteo.com/v1/forecast?" + urlencode({
                "latitude": latitude, "longitude": longitude,
                "current": "temperature_2m,relative_humidity_2m,weather_code",
            })
            try:
                current = json.loads(fetch_public(url))["current"]
                temperature = float(current["temperature_2m"])
                humidity = float(current["relative_humidity_2m"])
                code = int(current["weather_code"])
                if not math.isfinite(temperature) or not 0 <= humidity <= 100:
                    raise ValueError("Invalid weather values")
                return Weather(temperature, humidity, code)
            except (KeyError, TypeError, ValueError, OverflowError):
                raise ValueError("Weather source unavailable or returned invalid data") from None
        return self.cached(("weather", latitude, longitude), 600, load)

    def headline(self, url: str, seconds: int, now: float) -> str:
        titles = ["Sample headline: your news on Times Gate", "Choose an RSS or Atom feed"] if self.demo else (
            self.cached(("rss", url), 300, lambda: feed_titles(fetch_public(url)))
        )
        index = int(now // seconds) % len(titles)
        return f"{index + 1}/{len(titles)}  {titles[index]}"

    def metrics(self) -> dict[str, float | None]:
        if self.demo:
            return dict(zip(METRICS, (24, 62, 48, 37, 52, 46, 2500000, 350000)))

        def load() -> dict[str, float | None]:
            values: dict[str, float | None] = dict.fromkeys(METRICS)
            values.update(cpu=psutil.cpu_percent(), ram=psutil.virtual_memory().percent,
                          disk=psutil.disk_usage("/").percent)
            now = time.monotonic()
            counters = psutil.net_io_counters()
            if counters is not None:
                if self.network_previous is not None:
                    stamp, received, sent = self.network_previous
                    elapsed = now - stamp
                    if elapsed > 0 and counters.bytes_recv >= received and counters.bytes_sent >= sent:
                        values["download"] = (counters.bytes_recv - received) / elapsed
                        values["upload"] = (counters.bytes_sent - sent) / elapsed
                self.network_previous = (now, counters.bytes_recv, counters.bytes_sent)
            if hasattr(psutil, "sensors_temperatures"):
                groups = psutil.sensors_temperatures()
                for name in ("coretemp", "k10temp", "cpu_thermal"):
                    if groups.get(name):
                        values["cpu_temp"] = groups[name][0].current
                        break
            if self.nvidia:
                try:
                    result = subprocess.run(
                        [self.nvidia, "--query-gpu=utilization.gpu,temperature.gpu",
                         "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=3, check=True,
                    )
                    gpu, temperature = result.stdout.splitlines()[0].split(",")
                    values.update(gpu=float(gpu), gpu_temp=float(temperature))
                except (OSError, subprocess.SubprocessError, ValueError, IndexError):
                    raise ValueError("NVIDIA metrics could not be read") from None
            return values
        return self.cached(("metrics",), 5, load)
