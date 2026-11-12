import unittest
from unittest.mock import AsyncMock, patch

import httpx

from api.main import app
from api import football


class FootballEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        football._cache.clear()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        football._cache.clear()

    async def test_leagues_are_isolated_and_repeated_reads_are_cached(self):
        async def snapshot(league, target):
            return {"league": league, "date": target.isoformat()}
        with patch.object(football, "build_football_snapshot", side_effect=snapshot) as build, patch.object(football, "find_football_bets", side_effect=lambda data, count: data):
            nfl = await self.client.get("/api/nfl/weekly?date_str=2026-11-11")
            cfb = await self.client.get("/api/cfb/weekly?date_str=2026-11-11")
            again = await self.client.get("/api/nfl/weekly?date_str=2026-11-11")
        self.assertEqual(nfl.json()["league"], "nfl")
        self.assertEqual(cfb.json()["league"], "cfb")
        self.assertEqual(nfl.json(), again.json())
        self.assertEqual(build.call_count, 2)

    async def test_source_errors_do_not_expose_upstream_credentials(self):
        with patch.object(football, "build_football_snapshot", new=AsyncMock(side_effect=RuntimeError("apiKey=secret"))):
            result = await self.client.get("/api/nfl/weekly")
        self.assertEqual(result.status_code, 502)
        self.assertNotIn("secret", result.text)

    async def test_invalid_date_is_rejected_before_fetching(self):
        with patch.object(football, "build_football_snapshot", new=AsyncMock()) as build:
            result = await self.client.get("/api/cfb/weekly?date_str=not-a-date")
        self.assertEqual(result.status_code, 400)
        build.assert_not_called()
