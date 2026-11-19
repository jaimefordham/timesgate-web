from datetime import datetime
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from PIL import Image, ImageChops, ImageColor

from timesgate.models import Configuration, DesignLayer, Screen, WIDGET_ACCENTS
from timesgate.render import render
from timesgate.widgets import MAX_RESPONSE, Weather, WidgetSources, feed_titles, fetch_public


class WidgetModelTests(unittest.TestCase):
    def test_widget_inputs_are_validated_even_without_scheduling(self):
        for values in (
            {"kind": "weather"}, {"kind": "weather", "latitude": 91, "longitude": 0},
            {"kind": "countdown"}, {"kind": "countdown", "target": "2026-02-30T12:00"},
            {"kind": "countdown", "target": "2026-11-18"},
            {"kind": "rss"}, {"kind": "rss", "url": "file:///etc/hosts"},
            {"kind": "rss", "url": "https://user:password@example.com/feed"},
            {"kind": "rss", "url": "https://example.com/feed", "news_seconds": 0},
            {"kind": "custom", "elements": [{"type": "image"}]},
            {"kind": "custom", "elements": [{"text": "{cpu.real}"}]},
            {"kind": "custom", "elements": [{"text": "{time!r}"}]},
            {"kind": "custom", "elements": [{"text": "{"}]},
            {"kind": "custom", "elements": [{"maximum": 0}]},
            {"kind": "custom", "elements": [{"x": -1}]},
            {"kind": "custom", "elements": [{"media_id": "../not-media"}]},
            {"kind": "custom", "elements": [{}] * 21},
            {"accent": "#36D9FF"}, {"accent": "unknown"}, {"accent": ""},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                Screen(**values)

    def test_new_widgets_round_trip_without_changing_old_configs(self):
        config = Configuration.model_validate({"devices": [{"screens": [{}] * 5}]})
        config.devices[0].screens[:4] = [
            Screen(kind="weather", latitude=38.7, longitude=-9.1),
            Screen(kind="countdown", target="2026-12-25T00:00"),
            Screen(kind="rss", url="https://example.com/feed"),
            Screen(kind="custom", elements=[DesignLayer(text="{time} {cpu} {{literal}}")]),
        ]
        restored = Configuration.model_validate_json(config.model_dump_json())
        self.assertEqual(restored, config)
        self.assertEqual(restored.devices[0].screens[4].kind, "unmanaged")


class SourceTests(unittest.TestCase):
    def test_rss_atom_and_rdf_titles_are_bounded_and_sanitized(self):
        for source in (
            b"<rss><channel><item><title>A &amp; B</title></item></channel></rss>",
            b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A &amp; B</title></entry></feed>',
            b'<rdf:RDF xmlns:rdf="urn:rdf"><item><title>A &amp; B</title></item></rdf:RDF>',
        ):
            with self.subTest(source=source):
                self.assertEqual(feed_titles(source), ["A & B"])
        self.assertEqual(feed_titles(
            b"<rss><item><title>&lt;b&gt;Hello&lt;/b&gt;\n world</title></item></rss>"
        ), ["Hello world"])
        many = b"<rss>" + (b"<item><title>" + b"x" * 600 + b"</title></item>") * 60 + b"</rss>"
        titles = feed_titles(many)
        self.assertEqual((len(titles), len(titles[0])), (50, 500))

    def test_bad_or_unsafe_feeds_are_explicit_errors(self):
        for source in (
            b"<rss>", b"<rss/>", b"<html><item><title>Not a feed</title></item></html>",
            b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/hosts">]>'
            b"<rss><item><title>&x;</title></item></rss>",
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                feed_titles(source)

    def test_weather_and_news_cache_and_headline_rotation(self):
        sources = WidgetSources()
        weather = b'{"current":{"temperature_2m":12.5,"relative_humidity_2m":80,"weather_code":61}}'
        feed = b"<rss><item><title>First</title></item><item><title>Second</title></item></rss>"
        with patch("timesgate.widgets.fetch_public", side_effect=[weather, feed]) as fetch:
            self.assertEqual(sources.weather(38, -9), Weather(12.5, 80, 61))
            self.assertEqual(sources.weather(38, -9), Weather(12.5, 80, 61))
            self.assertEqual(sources.headline("https://example.com/rss", 15, 0), "1/2  First")
            self.assertEqual(sources.headline("https://example.com/rss", 15, 15), "2/2  Second")
            self.assertEqual(sources.headline("https://example.com/rss", 15, 30), "1/2  First")
            self.assertEqual(fetch.call_count, 2)

    def test_invalid_weather_data_does_not_render_successfully(self):
        for blob in (
            b"not-json", b'{"current":{}}',
            b'{"current":{"temperature_2m":null,"relative_humidity_2m":80,"weather_code":1}}',
            b'{"current":{"temperature_2m":10,"relative_humidity_2m":101,"weather_code":1}}',
            b'{"current":{"temperature_2m":10,"relative_humidity_2m":80,"weather_code":1e1000}}',
        ):
            with self.subTest(blob=blob), patch(
                "timesgate.widgets.fetch_public", return_value=blob,
            ), self.assertRaisesRegex(ValueError, "invalid data"):
                WidgetSources().weather(0, 0)

    def test_cache_expires_and_failures_are_reported_without_repeated_fetches(self):
        sources = WidgetSources()
        with patch("timesgate.widgets.time.monotonic", return_value=100), patch(
            "timesgate.widgets.fetch_public", return_value=b"<rss/>",
        ) as fetch:
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "no headlines"):
                    sources.headline("https://example.com/rss", 15, 100)
            self.assertEqual(fetch.call_count, 1)
        with patch("timesgate.widgets.time.monotonic", return_value=131), patch(
            "timesgate.widgets.fetch_public",
            return_value=b"<rss><item><title>Recovered</title></item></rss>",
        ) as fetch:
            self.assertIn("Recovered", sources.headline("https://example.com/rss", 15, 131))
            fetch.assert_called_once()

    def test_demo_sources_never_fetch_network_or_real_metrics(self):
        sources = WidgetSources(demo=True)
        with patch("timesgate.widgets.fetch_public") as fetch, patch(
            "timesgate.widgets.psutil.virtual_memory",
        ) as metrics:
            self.assertEqual(sources.weather(0, 0).temperature, 23)
            self.assertIn("Sample", sources.headline("https://example.com/rss", 15, 0))
            self.assertEqual(sources.metrics()["cpu"], 24)
            fetch.assert_not_called()
            metrics.assert_not_called()

    def test_metrics_compute_network_rates_after_the_first_sample(self):
        sources = WidgetSources()
        sources.nvidia = None
        with patch("timesgate.widgets.psutil.cpu_percent", return_value=24), patch(
            "timesgate.widgets.psutil.virtual_memory", return_value=Mock(percent=42),
        ), patch("timesgate.widgets.psutil.disk_usage", return_value=Mock(percent=50)), patch(
            "timesgate.widgets.psutil.sensors_temperatures", return_value={}, create=True,
        ), patch("timesgate.widgets.psutil.net_io_counters") as counters:
            counters.return_value = Mock(bytes_recv=1000, bytes_sent=500)
            with patch("timesgate.widgets.time.monotonic", return_value=100):
                first = sources.metrics()
            self.assertEqual(first["cpu"], 24)
            self.assertIsNone(first["download"])
            self.assertIsNone(first["gpu_temp"])
            counters.return_value = Mock(bytes_recv=1300, bytes_sent=600)
            with patch("timesgate.widgets.time.monotonic", return_value=110):
                second = sources.metrics()
            self.assertEqual((second["download"], second["upload"]), (30, 10))
            counters.return_value = Mock(bytes_recv=1, bytes_sent=1)
            with patch("timesgate.widgets.time.monotonic", return_value=120):
                reset = sources.metrics()
            self.assertIsNone(reset["download"])

    def test_private_targets_and_redirects_are_rejected(self):
        for address in ("127.0.0.1", "192.168.1.1", "169.254.169.254", "::1"):
            with self.subTest(address=address), patch(
                "timesgate.widgets.socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 80))],
            ), patch("timesgate.widgets.socket.create_connection") as connect:
                with self.assertRaisesRegex(ValueError, "public Internet"):
                    fetch_public("http://example.com/feed")
                connect.assert_not_called()
        response = Mock(status=302)
        response.getheader.return_value = "http://127.0.0.1/feed"
        with patch("timesgate.widgets.socket.getaddrinfo", side_effect=[
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))],
            [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))],
        ]), patch("timesgate.widgets.socket.create_connection"), patch(
            "timesgate.widgets.HTTPConnection",
        ) as connection:
            connection.return_value.getresponse.return_value = response
            with self.assertRaisesRegex(ValueError, "public Internet"):
                fetch_public("http://example.com/feed")
            response.close.assert_called_once()

    def test_fetch_pins_checked_address_and_limits_response_size(self):
        for oversized in (False, True):
            with self.subTest(oversized=oversized), patch(
                "timesgate.widgets.socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))],
            ), patch("timesgate.widgets.socket.create_connection") as connect, patch(
                "timesgate.widgets.HTTPConnection",
            ) as connection:
                response = connection.return_value.getresponse.return_value
                response.status = 200
                response.getheader.return_value = "identity"
                response.read1.side_effect = (
                    [b"x" * 65536] * (MAX_RESPONSE // 65536) + [b"x"]
                    if oversized else [b"feed", b""]
                )
                if oversized:
                    with self.assertRaisesRegex(ValueError, "2 MiB"):
                        fetch_public("http://example.com/feed")
                else:
                    self.assertEqual(fetch_public("http://example.com/feed"), b"feed")
                connect.assert_called_once_with(("93.184.216.34", 80), timeout=6)
                response.close.assert_called_once()


class WidgetRenderTests(unittest.TestCase):
    def test_clock_day_of_month_uses_accent_without_recolouring_time_or_date_labels(self):
        now = datetime(2026, 11, 18, 12, 34, tzinfo=ZoneInfo("UTC"))
        for accent in ("original", *WIDGET_ACCENTS):
            with self.subTest(accent=accent), patch(
                "timesgate.render.ImageDraw.ImageDraw.text", autospec=True,
            ) as text:
                render(Screen(kind="clock", accent=accent), "UTC", now=now)
                calls = {call.args[2]: call for call in text.call_args_list}
                expected = "#65e0c8" if accent == "original" else WIDGET_ACCENTS[accent]["color"]
                self.assertEqual(calls["18"].kwargs["fill"], expected)
                self.assertEqual(calls["12:34"].kwargs["fill"], "white")
                self.assertEqual(calls["Wed "].kwargs["fill"], "#b9c9d6")
                self.assertEqual(calls[" Nov"].kwargs["fill"], "#b9c9d6")

    def text_for(self, screen, now=None, **context):
        with patch("timesgate.render.ImageDraw.ImageDraw.text", autospec=True) as text:
            image = render(screen, "Europe/Lisbon", now=now, **context)
        self.assertEqual((image.mode, image.size), ("RGB", (128, 128)))
        return [call.args[2] for call in text.call_args_list]

    def test_accents_recolour_headers_and_labels_preserving_white_values_and_contrast(self):
        def luminance(rgb):
            channels = [channel / 255 for channel in rgb]
            linear = [
                channel / 12.92 if channel <= 0.04045 else ((channel + .055) / 1.055) ** 2.4
                for channel in channels
            ]
            return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))

        now = datetime(2026, 11, 18, 12, 34, tzinfo=ZoneInfo("UTC"))
        for accent in ("original", *WIDGET_ACCENTS):
            for kind in ("clock", "text", "weather", "countdown", "rss", "strava"):
                with self.subTest(accent=accent, kind=kind):
                    screen = Screen(
                        kind=kind, accent=accent, text="HELLO", latitude=0, longitude=0,
                        target="2099-12-25T00:00", url="https://example.com/feed",
                    )
                    expected = (
                        WIDGET_ACCENTS[accent]["color"] if accent != "original"
                        else "#fc4c02" if kind == "strava" else "#65e0c8"
                    )
                    with patch("timesgate.render.ImageDraw.ImageDraw.text", autospec=True) as text:
                        image = render(
                            screen, "UTC", now=now, weather=Weather(10, 70, 95),
                            headline="A readable headline", preview=True,
                        )
                    self.assertEqual(image.getpixel((6, 20 if kind == "strava" else 23)),
                                     ImageColor.getrgb(expected))
                    calls = text.call_args_list
                    self.assertEqual(calls[0].kwargs["fill"], expected)
                    self.assertTrue(any(call.kwargs["fill"] == "white" for call in calls))
                    label = {
                        "weather": "THUNDERSTORM", "countdown": "TIME REMAINING",
                        "strava": "EXAMPLE DATA",
                    }.get(kind)
                    if label:
                        self.assertEqual(next(
                            call.kwargs["fill"] for call in calls if call.args[2] == label
                        ), expected)
                    if accent != "original":
                        contrast = (luminance(ImageColor.getrgb(expected)) + .05) / (
                            luminance(image.getpixel((0, 0))) + .05
                        )
                        self.assertGreaterEqual(contrast, 7)

    def test_strava_layout_prioritizes_numbers_and_omits_elevation_in_both_modes(self):
        stats = {"distance": 1609344, "count": 42, "moving_time": 181800}
        for mode, period in (("ytd", "YTD"), ("all-time", "ALL")):
            for units, distance, unit in (("metric", "1,609", "km cycled"),
                                          ("imperial", "1,000", "mi cycled")):
                with self.subTest(mode=mode, units=units):
                    cache = {"fetched_at": 1000, "stats": {mode: stats}}
                    with patch("timesgate.render.ImageDraw.ImageDraw.text", autospec=True) as text:
                        render(Screen(kind="strava", strava_mode=mode, units=units), "UTC", cache)
                    labels = {call.args[2]: call for call in text.call_args_list}
                    self.assertIn(distance, labels)
                    self.assertIn(unit, labels)
                    self.assertIn(period, labels)
                    self.assertIn("42", labels)
                    self.assertIn("50h", labels)
                    self.assertIn("RIDES", labels)
                    self.assertIn("MOVING", labels)
                    self.assertGreater(labels[distance].kwargs["font"].size,
                                       labels["42"].kwargs["font"].size)

    def test_strava_large_numbers_fit_their_assigned_columns(self):
        stats = {"distance": 987654321000, "count": 1234567, "moving_time": 987654321000}
        cache = {"fetched_at": 1000, "stats": {"all-time": stats}}
        with patch("timesgate.render.ImageDraw.ImageDraw.text", autospec=True) as text:
            render(Screen(kind="strava", strava_mode="all-time"), "UTC", cache)
        calls = {call.args[2]: call for call in text.call_args_list}
        for value, width in (("987,654,321", 116), ("1,234,567", 50), ("274,348,422h", 58)):
            self.assertIn(value, calls)
            face = calls[value].kwargs["font"]
            self.assertLessEqual(face.getlength(value), width)

    def test_weather_converts_units_and_shows_conditions(self):
        for units, temperature in (("metric", "10°C"), ("imperial", "50°F")):
            text = self.text_for(Screen(kind="weather", latitude=0, longitude=0, units=units),
                                 weather=Weather(10, 70, 95))
            self.assertIn(temperature, text)
            self.assertIn("THUNDERSTORM", text)
            self.assertIn("Humidity 70%", text)

    def test_countdown_uses_configured_timezone_and_finishes_at_zero(self):
        now = datetime(2026, 7, 1, 12, 0, tzinfo=ZoneInfo("UTC"))
        for target, expected in (
            ("2026-07-01T14:00", "01:00"),
            ("2026-07-01T13:00+00:00", "01:00"),
            ("2026-07-03T15:00", "2d 2h"),
            ("2026-07-01T13:00:30", "00:30"),
            ("2026-07-01T12:00", "00:00"),
        ):
            with self.subTest(target=target):
                text = self.text_for(Screen(kind="countdown", target=target), now)
                self.assertIn(expected, text)
                self.assertIn("FINISHED" if expected == "00:00" else "TIME REMAINING", text)

    def test_rss_wraps_long_headlines_inside_canvas(self):
        text = self.text_for(Screen(kind="rss", url="https://example.com/rss"),
                             headline="A long headline " * 30)
        self.assertLessEqual(len(text), 7)
        self.assertTrue(text[-1].endswith("..."))

    def test_custom_layers_clip_stack_and_use_static_first_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            key = "a" * 32
            Image.new("RGB", (128, 128), "red").save(
                root / (key + ".gif"), save_all=True,
                append_images=[Image.new("RGB", (128, 128), "green")], duration=100,
            )
            screen = Screen(kind="custom", elements=[
                DesignLayer(type="image", media_id=key, x=0, y=0, width=128, height=128),
                DesignLayer(type="bar", x=120, y=120, width=20, height=20, value=200, color="#0000ff"),
            ])
            image = render(screen, "UTC", media_dir=root)
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(image.getpixel((127, 127)), (0, 0, 255))
            screen.elements.reverse()
            behind = render(screen, "UTC", media_dir=root)
            self.assertIsNotNone(ImageChops.difference(image, behind).getbbox())
            self.assertEqual(behind.getpixel((127, 127)), (255, 0, 0))
            screen.accent = "hot-pink"
            self.assertIsNone(ImageChops.difference(
                behind, render(screen, "UTC", media_dir=root),
            ).getbbox())

    def test_custom_dynamic_fields_and_metric_bars(self):
        now = datetime(2026, 11, 18, 12, 34, tzinfo=ZoneInfo("UTC"))
        screen = Screen(kind="custom", elements=[
            DesignLayer(text="{time}", height=32),
            DesignLayer(type="bar", metric="cpu", x=0, y=60, width=100, height=10,
                        color="#00ff00"),
        ])
        self.assertIn("12:34", self.text_for(screen, now))
        image = render(screen, "UTC", now=now, metrics={"cpu": 25})
        self.assertEqual(image.getpixel((24, 65)), (0, 255, 0))
        self.assertEqual(image.getpixel((25, 65)), (8, 18, 30))
        screen.elements = [DesignLayer(text="{gpu}", height=30)]
        self.assertIn("N/A", self.text_for(screen, now))
