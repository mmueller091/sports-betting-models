import copy
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import httpx
from data import pick_audit as audit


def bundle(at='2026-11-12T10:00:00Z', odds=-110, line=45.5, side='over'):
    return {'cfb': {'status': 'ok', 'generated_at': at, 'data': {'weekly': {
        'model_version': 'test-1', 'games': [{'game_id': '42', 'home_team': 'Home', 'away_team': 'Away'}],
        'picks': [{'game_id': '42', 'game_time': '2026-11-14T17:00:00Z', 'matchup': 'Away @ Home',
                   'pick': f'{side.title()} {line}', 'market': 'total', 'side': side, 'line': line,
                   'odds': odds, 'book': 'Test book', 'model_probability': .6}]}}}}


class AuditTests(unittest.IsolatedAsyncioTestCase):
    def ledger(self):
        return {'version': 1, 'started_at': None, 'records': {}}

    def test_first_quote_frozen_revisions_persist_and_same_snapshot_is_idempotent(self):
        ledger = self.ledger()
        first = bundle()
        audit.record_bundle(first, ledger)
        audit.record_bundle(first, ledger)
        audit.record_bundle(bundle('2026-11-12T11:00:00Z', -125, 44), ledger)
        self.assertEqual(len(ledger['records']), 1)
        r = next(iter(ledger['records'].values()))
        self.assertEqual((r['original']['odds'], r['original']['line']), (-110, 45.5))
        self.assertEqual((r['revisions'][0]['odds'], r['revisions'][0]['line']), (-125, 44))
        self.assertEqual(r['observations'], 2)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'audit.json'
            audit.save_ledger(ledger, path)
            self.assertEqual(audit.load_ledger(path), ledger)
            path.write_text('{bad')
            with self.assertRaises(json.JSONDecodeError):
                audit.load_ledger(path)

    def test_opposing_selection_is_visible_but_not_double_counted(self):
        ledger = self.ledger()
        audit.record_bundle(bundle(), ledger)
        audit.record_bundle(bundle('2026-11-12T11:00:00Z', side='under'), ledger)
        rows = list(ledger['records'].values())
        self.assertEqual(len(rows), 2)
        self.assertEqual([r['primary'] for r in rows], [True, False])
        for r in rows:
            r['settlement'] = {'result': 'WIN', 'profit': 100}
        self.assertEqual(audit.audit_summary(ledger)['summary']['profit'], 100)

    def test_stale_and_after_kickoff_picks_never_become_pregame_records(self):
        ledger = self.ledger()
        late = bundle('2026-11-14T18:00:00Z')
        audit.record_bundle(late, ledger)
        stale = bundle(); stale['cfb']['status'] = 'stale'
        audit.record_bundle(stale, ledger)
        self.assertFalse(ledger['records'])

    def test_total_spread_moneyline_push_and_price_settlement(self):
        base = {'market': 'total', 'side': 'under', 'line': 45, 'odds': -125}
        self.assertEqual(audit.settle(base, {'completed': True, 'home_score': 24, 'away_score': 21})['result'], 'PUSH')
        self.assertEqual(audit.settle(base, {'completed': True, 'home_score': 21, 'away_score': 20})['profit'], 80)
        spread = {'market': 'spread', 'side': 'away', 'home_team': 'H', 'away_team': 'A', 'line': 3.5, 'odds': 120}
        self.assertEqual(audit.settle(spread, {'completed': True, 'home_score': 24, 'away_score': 21})['profit'], 120)
        spread['line'] = -3.5
        self.assertEqual(audit.settle(spread, {'completed': True, 'home_score': 24, 'away_score': 21})['profit'], -100)
        ml = {**spread, 'market': 'moneyline', 'side': 'home'}
        self.assertEqual(audit.settle(ml, {'completed': True, 'home_score': 20, 'away_score': 20})['result'], 'PUSH')
        self.assertIsNone(audit.settle(base, {'completed': False}))
        self.assertEqual(audit.settle(base, {'canceled': True})['result'], 'VOID')

    def test_goal_prediction_unpriced_and_nonparticipant_void(self):
        row = {'market': 'goal', 'player_id': 1, 'odds': None}
        self.assertIsNone(audit.settle(row, {'completed': True}))
        self.assertIsNone(audit.settle(row, {'completed': True, 'players': {'1': {'goals': 0, 'did_play': None}}}))
        self.assertEqual(audit.settle(row, {'completed': True, 'players': {}})['result'], 'VOID')
        result = audit.settle(row, {'completed': True, 'players': {'1': {'goals': 1, 'did_play': True}}})
        self.assertEqual(result['result'], 'WIN'); self.assertIsNone(result['profit'])

    async def test_failed_source_stays_pending_and_duplicate_game_fetches_once(self):
        ledger = self.ledger(); audit.record_bundle(bundle(), ledger)
        audit.record_bundle(bundle('2026-11-12T11:00:00Z', side='under'), ledger)
        now = datetime(2026, 11, 15, tzinfo=timezone.utc)
        fetch = AsyncMock(side_effect=RuntimeError('private source query text'))
        await audit.grade_ledger(ledger, now, fetch)
        fetch.assert_awaited_once()
        self.assertTrue(all(r['settlement'] is None for r in ledger['records'].values()))
        self.assertNotIn('private source', json.dumps(ledger))
        fetch = AsyncMock(return_value={'completed': True, 'home_score': 27, 'away_score': 24, 'source_url': 'https://official.test'})
        await audit.grade_ledger(ledger, now.replace(hour=3), fetch)
        self.assertEqual(audit.audit_summary(ledger)['summary']['wins'], 1)

    async def test_official_score_parsers(self):
        client = AsyncMock()
        client.get.return_value = httpx.Response(200, request=httpx.Request('GET', 'https://test'), json={'header': {'competitions': [{'status': {'type': {'completed': True, 'name': 'STATUS_FINAL'}}, 'competitors': [{'homeAway': 'home', 'score': '27'}, {'homeAway': 'away', 'score': '24'}]}]}})
        result = await audit.fetch_result(client, 'nfl', '42')
        self.assertEqual(result['home_score'], 27)
        client.get.return_value = httpx.Response(200, request=httpx.Request('GET', 'https://test'), json={'gameState': 'OFF', 'homeTeam': {'score': 3}, 'awayTeam': {'score': 2}, 'playerByGameStats': {'homeTeam': {'defense': [], 'forwards': [{'playerId': 1, 'goals': 2, 'toi': '20:00'}]}, 'awayTeam': {'defense': [], 'forwards': [{'playerId': 2, 'goals': 0, 'toi': '15:00'}]}}})
        result = await audit.fetch_result(client, 'nhl', '43')
        self.assertEqual(result['players']['1']['goals'], 2)
