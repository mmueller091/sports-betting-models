import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from data import odds
from data.source_health import begin_collection, end_collection, safe_fetch, warn
from export_site import export_sport


class PublishingTests(unittest.IsolatedAsyncioTestCase):
    async def test_football_export_preserves_prior_results_on_http_failure(self):
        previous = {"date": "2026-11-10", "generated_at": "2026-11-10T18:00:00Z", "data": {"weekly": {"picks": [{"game_id": "old"}]}}}
        client = AsyncMock()
        client.get.return_value = httpx.Response(502, json={"detail": "Data unavailable"}, request=httpx.Request("GET", "http://test/api/nfl/weekly"))
        result = await export_sport(client, "nfl", "2026-11-11", previous)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["data"], previous["data"])
        self.assertEqual(result["generated_at"], previous["generated_at"])

    async def test_football_export_publishes_weekly_warnings_and_actual_odds_timestamp(self):
        client = AsyncMock()
        client.get.return_value = httpx.Response(200, json={"league": "cfb", "picks": [], "warnings": ["College injury status unavailable"]}, request=httpx.Request("GET", "http://test/api/cfb/weekly"))
        with patch.dict(odds.ODDS_STATUS, {"americanfootball_ncaaf": {"updated_at": "2026-11-11T17:00:00Z"}}):
            result = await export_sport(client, "cfb", "2026-11-11", {})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["data"]["weekly"]["league"], "cfb")
        self.assertEqual(result["odds"]["updated_at"], "2026-11-11T17:00:00Z")

    async def test_market_quote_preserves_bookmaker_timestamp_and_price(self):
        game = odds._parse_odds_game({"id": "game1", "sport_key": "americanfootball_nfl", "commence_time": "2026-11-15T18:00:00Z", "away_team": "Away", "home_team": "Home", "bookmakers": [{"key": "book", "title": "Book Name", "markets": [{"key": "spreads", "last_update": "2026-11-11T19:00:00Z", "outcomes": [{"name": "Home", "price": -115, "point": -3.5}, {"name": "Away", "price": -105, "point": 3.5}]}]}]})
        self.assertEqual(game["market_updated_at"]["spreads"]["book"], "2026-11-11T19:00:00Z")
        self.assertEqual(game["book_titles"]["book"], "Book Name")
        self.assertEqual(game["run_lines"]["book"]["home"], {"line": -3.5, "odds": -115})

    async def test_schedule_failure_keeps_last_good_sport(self):
        previous = {"date": "2026-11-10", "generated_at": "2026-11-10T18:00:00Z", "data": {"bets": {"bets": ["previous"]}}}
        async def get(*args, **kwargs):
            warn("get_todays_nhl_games: unavailable; fallback data used")
            return httpx.Response(200, json={"games": []}, request=httpx.Request("GET", "http://test"))
        client = AsyncMock()
        client.get.side_effect = get
        result = await export_sport(client, "nhl", "2026-11-11", previous)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["data"], previous["data"])
        self.assertEqual(result["generated_at"], previous["generated_at"])

    async def test_failed_source_never_publishes_exception_secrets(self):
        async def source():
            raise RuntimeError("https://api.example/?apiKey=secret-value")
        token = begin_collection()
        self.assertEqual(await safe_fetch(source(), []), [])
        warnings = end_collection(token)
        self.assertIn("source:", warnings[0])
        self.assertNotIn("secret-value", json.dumps(warnings))

    async def test_cached_odds_do_not_make_paid_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            import hashlib
            key = hashlib.sha256(b"icehockey_nhl:us:h2h,spreads,totals").hexdigest()[:16]
            (cache / f"{key}.json").write_text(json.dumps({"timestamp": time.time(), "updated_at": "2026-11-11T12:00:00Z", "games": [{"cached": True}]}))
            with patch.object(odds, "CACHE_DIR", cache), patch.dict("os.environ", {"ODDS_CACHE_HOURS": "12"}), patch.object(odds.httpx, "AsyncClient") as http:
                result = await odds.get_game_odds(sport="icehockey_nhl")
            self.assertEqual(result, [{"cached": True}])
            http.assert_not_called()
