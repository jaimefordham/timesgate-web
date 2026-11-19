from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from timesgate.models import StravaSettings
from timesgate.store import Store
from timesgate.strava import Strava, StravaError


def response(body, code=200, headers=None):
    result = Mock(status_code=code, headers=headers or {})
    result.json.return_value = body
    return result


class StravaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite")
        self.session = Mock()
        self.client = Strava(self.store, "http://localhost:8080", self.session)
        self.client.configure(StravaSettings(client_id="123", client_secret="private-client-secret"))

    def test_oauth_success_connects_and_fetches_authorized_athlete_totals(self):
        authorization = parse_qs(urlsplit(self.client.authorize()).query)
        self.assertEqual(authorization["redirect_uri"],
                         ["http://localhost:8080/oauth/strava/callback"])
        totals = {"count": 3, "distance": 100000, "moving_time": 3600, "elevation_gain": 250}
        self.session.request.side_effect = [
            response({"access_token": "access", "refresh_token": "refresh",
                      "expires_at": int(time.time()) + 3600, "athlete": {"id": 42}}),
            response({"ytd_ride_totals": totals, "all_ride_totals": totals}),
        ]
        self.client.complete(authorization["state"][0], "one-time-code", "read,activity:read")
        self.assertTrue(self.client.status()["connected"])
        self.assertIsNone(self.store.get("strava_oauth"))
        self.assertEqual(self.client.fetch()["stats"]["all-time"]["count"], 3)
        self.assertEqual(self.session.request.call_args.args[1],
                         "https://www.strava.com/api/v3/athletes/42/stats")
        with self.assertRaises(ValueError):
            self.client.complete(authorization["state"][0], "one-time-code", "read,activity:read")

    def test_refresh_rotation_is_persisted_before_stats_and_cache_is_validated(self):
        self.store.put("strava_tokens", {"access_token": "old-access", "refresh_token": "old-refresh",
                                        "expires_at": 1, "athlete_id": 42})
        totals = {"count": 10, "distance": 345000, "moving_time": 7200, "elevation_gain": 1234}
        self.session.request.side_effect = [
            response({"access_token": "new-access", "refresh_token": "new-refresh",
                      "expires_at": int(time.time()) + 3600}),
            response({"ytd_ride_totals": totals, "all_ride_totals": totals}),
        ]
        cache = self.client.fetch()
        self.assertEqual(self.store.get("strava_tokens")["refresh_token"], "new-refresh")
        self.assertEqual(cache["stats"]["ytd"]["distance"], 345000)
        self.assertEqual(self.session.request.call_count, 2)
        self.assertNotIn("new-refresh", str(self.client.status()))
        with self.assertRaisesRegex(StravaError, "not due"):
            self.client.fetch()
        self.assertEqual(self.session.request.call_count, 2)

    def test_invalid_or_reused_oauth_state_never_exchanges_code(self):
        self.client.authorize()
        with self.assertRaises(ValueError):
            self.client.complete("wrong-state", "code", "read,activity:read")
        self.session.request.assert_not_called()
        state = self.store.get("strava_oauth")["state"]
        with self.assertRaisesRegex(ValueError, "Grant"):
            self.client.complete(state, "code", "read")
        with self.assertRaises(ValueError):
            self.client.complete(state, "code", "read,activity:read")

    def test_oauth_rejects_malformed_athlete_without_storing_tokens(self):
        for athlete in ([], "invalid", {"id": "../stats"}, {"id": True}):
            with self.subTest(athlete=athlete):
                self.client.authorize()
                state = self.store.get("strava_oauth")["state"]
                self.session.request.return_value = response({
                    "access_token": "access", "refresh_token": "refresh",
                    "expires_at": int(time.time()) + 3600, "athlete": athlete,
                })
                with self.assertRaisesRegex(StravaError, "invalid athlete"):
                    self.client.complete(state, "code", "read,activity:read")
                self.assertIsNone(self.store.get("strava_tokens"))

    def test_rate_limit_retains_last_good_cache_and_pauses_polling(self):
        cache = {"fetched_at": 1, "stats": {"ytd": {"count": 2}}}
        self.store.put("strava_cache", cache)
        self.store.put("strava_tokens", {"access_token": "access", "refresh_token": "refresh",
                                        "expires_at": int(time.time()) + 3600, "athlete_id": 42})
        self.session.request.return_value = response({}, 429, {"Retry-After": "1800"})
        with self.assertRaisesRegex(StravaError, "rate limit"):
            self.client.fetch()
        self.assertGreater(self.store.get("strava_next_poll"), time.time() + 1700)
        self.assertEqual(self.store.get("strava_cache"), cache)
        self.session.request.assert_called_once()
