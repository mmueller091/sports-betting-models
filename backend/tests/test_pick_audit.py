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

    def test_legacy_nhl_team_side_becomes_revision_and_opponent_stays_separate(self):
        for market, bet_type, line in (('spread', 'Spread', -1.5), ('moneyline', 'Moneyline', None)):
            for team_fields in (False, True):
                with self.subTest(market=market, team_fields=team_fields):
                    ledger = self.ledger()
                    game = {'game_id': '2026020299', 'matchup': 'Montreal Canadiens @ Boston Bruins',
                            'game_time': '2026-11-13T00:00:00Z'}
                    if team_fields:
                        game.update(home_team='Boston Bruins', away_team='Montreal Canadiens')
                    pick = {**game, 'team': 'Boston Bruins', 'pick': 'Boston Bruins -1.5 (betus)' if line else 'Boston Bruins ML',
                            'bet_type': bet_type, 'line': line, 'book': 'betus', 'odds': 208, 'our_prob': .765}
                    first = {'nhl': {'status': 'ok', 'generated_at': '2026-11-12T10:00:00Z',
                                     'data': {'games': {'games': [game]}, 'bets': {'bets': [pick]}}}}
                    audit.record_bundle(first, ledger)
                    original_id = next(iter(ledger['records']))
                    original = copy.deepcopy(ledger['records'][original_id]['original'])
                    self.assertEqual(original['side'], 'Boston Bruins')

                    revised = copy.deepcopy(first)
                    revised['nhl']['generated_at'] = '2026-11-12T11:00:00Z'
                    revised['nhl']['data']['bets']['bets'][0].update(
                        market=market, side='home', odds=180, our_prob=.44,
                        model_version='nhl-2026-11-12.correctness.1')
                    audit.record_bundle(revised, ledger)
                    audit.record_bundle(revised, ledger)
                    self.assertEqual(list(ledger['records']), [original_id])
                    record = ledger['records'][original_id]
                    self.assertEqual(record['original'], original)
                    self.assertTrue(record['primary'])
                    self.assertEqual(record['observations'], 2)
                    self.assertEqual(len(record['revisions']), 1)
                    self.assertEqual(record['revisions'][0]['id'], original_id)
                    self.assertEqual(record['revisions'][0]['side'], 'home')
                    self.assertEqual(record['revisions'][0]['odds'], 180)

                    opposing = copy.deepcopy(revised)
                    opposing['nhl']['generated_at'] = '2026-11-12T12:00:00Z'
                    opposing['nhl']['data']['bets']['bets'][0].update(
                        side='away', team='Montreal Canadiens', line=1.5 if line else None,
                        pick='Montreal Canadiens +1.5 (betus)' if line else 'Montreal Canadiens ML')
                    audit.record_bundle(opposing, ledger)
                    self.assertEqual(len(ledger['records']), 2)
                    self.assertEqual(ledger['records'][original_id]['original'], original)
                    other = next(value for key, value in ledger['records'].items() if key != original_id)
                    self.assertEqual(other['original']['side'], 'away')
                    self.assertFalse(other['primary'])

    async def test_existing_nhl_duplicate_becomes_alias_without_changing_originals_or_returns(self):
        ledger = self.ledger()
        game = {'game_id': '2026020300', 'matchup': 'Minnesota Wild @ Toronto Maple Leafs',
                'game_time': '2026-11-13T00:00:00Z'}
        pick = {**game, 'team': 'Minnesota Wild', 'pick': 'Minnesota Wild -1.5 (fanduel)',
                'bet_type': 'Spread', 'line': -1.5, 'book': 'fanduel', 'odds': 152, 'our_prob': .842}
        first = {'nhl': {'status': 'ok', 'generated_at': '2026-11-12T10:00:00Z',
                         'data': {'games': {'games': [game]}, 'bets': {'bets': [pick]}}}}
        audit.record_bundle(first, ledger)
        revised = copy.deepcopy(first)
        revised['nhl']['generated_at'] = '2026-11-12T11:00:00Z'
        revised['nhl']['data']['bets']['bets'][0]['odds'] = 150
        audit.record_bundle(revised, ledger)
        legacy_id = next(iter(ledger['records']))
        legacy = ledger['records'][legacy_id]
        legacy['settlement'] = {'result': 'WIN', 'profit': 152}
        before = audit.audit_summary(ledger)['summary']

        # Build the separately keyed record produced before the compatibility fix.
        canonical_ledger = self.ledger()
        canonical = copy.deepcopy(revised)
        canonical['nhl']['generated_at'] = '2026-11-12T12:00:00Z'
        canonical['nhl']['data']['bets']['bets'][0].update(
            market='spread', side='away', odds=140, our_prob=.53,
            model_version='nhl-2026-11-12.correctness.1')
        audit.record_bundle(canonical, canonical_ledger)
        canonical = copy.deepcopy(canonical)
        canonical['nhl']['generated_at'] = '2026-11-12T13:00:00Z'
        canonical['nhl']['data']['bets']['bets'][0]['odds'] = 135
        audit.record_bundle(canonical, canonical_ledger)
        canonical_id = next(iter(canonical_ledger['records']))
        duplicate = canonical_ledger['records'][canonical_id]
        duplicate['primary'] = False
        ledger['records'][canonical_id] = duplicate
        originals = {key: copy.deepcopy(record['original']) for key, record in ledger['records'].items()}
        retained_revisions = copy.deepcopy(duplicate['revisions'])

        summary = audit.audit_summary(ledger)
        self.assertEqual(len(ledger['records']), 2)
        self.assertEqual(duplicate['alias_of'], legacy_id)
        self.assertEqual(summary['records'], [legacy])
        self.assertEqual(summary['aliases'], [duplicate])
        self.assertEqual(summary['summary'], before)
        self.assertEqual({key: record['original'] for key, record in ledger['records'].items()}, originals)
        self.assertEqual(duplicate['revisions'], retained_revisions)
        self.assertEqual(legacy['observations'], 4)
        self.assertEqual(len(legacy['revisions']), 3)
        self.assertEqual([row.get('source_record_id') for row in legacy['revisions'][1:]],
                         [canonical_id, canonical_id])
        self.assertTrue(all(row['id'] == legacy_id for row in legacy['revisions']))

        reconciled = copy.deepcopy(ledger)
        audit.reconcile_nhl_side_aliases(ledger)
        audit.record_bundle(canonical, ledger)
        self.assertEqual(ledger, reconciled)
        canonical = copy.deepcopy(canonical)
        canonical['nhl']['generated_at'] = '2026-11-12T14:00:00Z'
        canonical['nhl']['data']['bets']['bets'][0]['odds'] = 130
        audit.record_bundle(canonical, ledger)
        self.assertEqual(len(ledger['records']), 2)
        self.assertEqual((legacy['observations'], len(legacy['revisions'])), (5, 4))
        self.assertEqual(legacy['revisions'][-1]['odds'], 130)
        self.assertEqual(legacy['revisions'][-1]['id'], legacy_id)
        self.assertEqual(duplicate, reconciled['records'][canonical_id])
        self.assertEqual({key: record['original'] for key, record in ledger['records'].items()}, originals)

        fetch = AsyncMock()
        await audit.grade_ledger(ledger, datetime(2026, 11, 14, tzinfo=timezone.utc), fetch)
        fetch.assert_not_awaited()  # The only pending record is the retained alias.
        self.assertIsNone(duplicate['settlement'])
        self.assertNotIn('last_checked', duplicate)
        self.assertEqual(audit.audit_summary(ledger)['summary'], before)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'audit.json'
            audit.save_ledger(ledger, path)
            self.assertEqual(audit.load_ledger(path), ledger)

    def test_nhl_model_change_appends_revision_and_keeps_original(self):
        ledger = self.ledger()
        pick = {'game_id': '99', 'game_time': '2026-11-14T00:00:00Z',
                'matchup': 'Away @ Home', 'market': 'moneyline', 'side': 'home',
                'pick': 'Home moneyline', 'odds': 110, 'our_prob': .55}
        data = {'bets': {'bets': [pick]}, 'games': {'games': [
            {'game_id': '99', 'home_team': 'Home', 'away_team': 'Away'}]}}
        audit.record_bundle({'nhl': {'status': 'ok', 'generated_at': '2026-11-12T10:00:00Z', 'data': data}}, ledger)
        pick['model_version'] = 'nhl-corrected-test'
        audit.record_bundle({'nhl': {'status': 'ok', 'generated_at': '2026-11-12T11:00:00Z', 'data': data}}, ledger)
        record = next(iter(ledger['records'].values()))
        self.assertEqual(record['original']['model_version'], 'nhl-existing-model')
        self.assertEqual(record['revisions'][0]['model_version'], 'nhl-corrected-test')

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
