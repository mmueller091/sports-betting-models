import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
from api.main import app, get_snapshot
from data import odds
import export_site


class BaseballPauseTests(unittest.IsolatedAsyncioTestCase):
    async def test_baseball_odds_and_props_never_create_http_client(self):
        with patch.dict('os.environ', {'ENABLE_MLB': 'false'}), patch.object(odds.httpx, 'AsyncClient') as client:
            self.assertEqual(await odds.get_game_odds(sport='baseball_mlb'), [])
            self.assertEqual(await odds.get_props('42'), [])
            client.assert_not_called()

    async def test_baseball_routes_blocked_before_source_work(self):
        with patch.dict('os.environ', {'ENABLE_MLB': 'false'}), patch('api.main.build_daily_snapshot', new_callable=AsyncMock) as build:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                for path in ('/api/daily', '/api/best-bets', '/api/hit-props', '/api/performance', '/api/refresh'):
                    r = await client.request('POST' if path.endswith('refresh') else 'GET', path)
                    self.assertEqual(r.status_code, 503)
            build.assert_not_awaited()

    async def test_publisher_skips_every_baseball_endpoint_and_training(self):
        async def sport(client, name, target, previous):
            self.assertNotEqual(name, 'mlb')
            return {'status': 'ok', 'generated_at': '2026-11-12T10:00:00Z', 'warnings': [], 'data': {}}
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'ENABLE_MLB': 'false'}), \
             patch.object(export_site, 'export_sport', side_effect=sport), \
             patch.object(export_site, 'train_if_ready') as train, \
             patch.object(export_site, 'grade_ledger', new_callable=AsyncMock), \
             patch('data.pick_audit.AUDIT_PATH', Path(directory) / 'audit.json'), \
             patch('api.main.grade_pending', new_callable=AsyncMock) as mlb_grade:
            result = await export_site.export(Path(directory) / 'dashboard.json')
            self.assertEqual(result['mlb']['status'], 'paused')
            self.assertIsNone(result['performance'])
            train.assert_not_called(); mlb_grade.assert_not_awaited()
