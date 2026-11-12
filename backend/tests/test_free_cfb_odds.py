"""Safety checks for untimestamped free CFB market reference imports."""
import csv
import gzip
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.free_cfb_odds import (
    PRICING_BASIS, _candidate_decisions, load_archive, load_forecasts,
)


FIELDS = ['id', 'game_id', 'season', 'game_desc', 'date_time', 'market_type',
          'abbr', 'lines', 'odds', 'opening_lines', 'opening_odds', 'book',
          'season_type', 'week', 'home_team_id', 'away_team_id']


def game(game_id='cfb:401000001', home=1, away=2):
    return {'game_id': game_id, 'league': 'cfb', 'season': 2025,
            'home_team_id': f'cfb:{home}', 'away_team_id': f'cfb:{away}',
            'home_score': 27, 'away_score': 24, 'completed': True}


def market_row(market, side, value, book='DraftKings', game_id='401000001'):
    result = {'id': game_id, 'game_id': game_id, 'season': '2025.0',
              'game_desc': 'Away@Home', 'date_time': '2025-11-01T12:00:00Z',
              'market_type': market, 'abbr': side,
              'lines': str(value) if market == 'spread' else '',
              'odds': str(value) if market == 'money_line' else '',
              'opening_lines': '', 'opening_odds': '', 'book': book,
              'season_type': 'regular', 'week': 10,
              'home_team_id': 1, 'away_team_id': 2}
    return result


def make_archive(path, records):
    with gzip.open(path, 'wt', encoding='utf-8', newline='') as dest:
        writer = csv.DictWriter(dest, FIELDS)
        writer.writeheader()
        writer.writerows(records)


class ClosingArchiveTests(unittest.TestCase):
    def test_strict_id_pair_and_book_normalization(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'archive.csv.gz'
            records = [market_row('spread', 'Home', -7),
                       market_row('spread', 'Away', 7),
                       market_row('money_line', 'Home', -260, 'Draft Kings'),
                       market_row('money_line', 'Away', 210, 'DraftKings')]
            make_archive(path, records)
            result = load_archive(path, {'cfb:401000001': game()})
            self.assertEqual(result.coverage['paired_moneyline_games_after_quality_filter'], 1)
            self.assertEqual(result.books, {'draftkings': 1})
            self.assertEqual(result.spread['cfb:401000001'], 7)
            self.assertEqual({q['side'] for q in result.quotes['cfb:401000001']}, {'home', 'away'})
            self.assertTrue(all('quote_time' not in q for q in result.quotes['cfb:401000001']))

    def test_reversed_favorite_is_quarantined_before_settlement(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'archive.csv.gz'
            make_archive(path, [market_row('spread', 'Home', -10),
                                market_row('spread', 'Away', 10),
                                market_row('money_line', 'Home', 300),
                                market_row('money_line', 'Away', -395)])
            result = load_archive(path, {'cfb:401000001': game()})
            self.assertEqual(result.coverage['paired_moneyline_games_after_quality_filter'], 0)
            self.assertEqual(result.rejections['ml_favorite_contradicts_spread'], 1)
            self.assertEqual(result.spread['cfb:401000001'], 10)

    def test_mismatched_espn_team_ids_block_all_contracts(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'archive.csv.gz'
            record = market_row('money_line', 'Home', -110)
            record['home_team_id'] = 999
            make_archive(path, [record, market_row('money_line', 'Away', 100)])
            result = load_archive(path, {'cfb:401000001': game()})
            self.assertEqual(result.rejections['identity_mismatch'], 1)
            self.assertEqual(result.quotes, {})

    def test_duplicate_conflict_excludes_entire_contract(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'archive.csv.gz'
            make_archive(path, [market_row('money_line', 'Home', -110),
                                market_row('money_line', 'Home', -120),
                                market_row('money_line', 'Away', 100)])
            result = load_archive(path, {'cfb:401000001': game()})
            self.assertEqual(result.rejections['conflicting_duplicate_contract'], 1)
            self.assertEqual(result.quotes, {})

    def test_unpriced_spreads_are_not_imputed_moneyline(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'archive.csv.gz'
            make_archive(path, [market_row('spread', 'Home', -4),
                                market_row('spread', 'Away', 4)])
            result = load_archive(path, {'cfb:401000001': game()})
            self.assertIn('cfb:401000001', result.spread)
            self.assertEqual(result.quotes, {})

    def test_study_keeps_original_prior_history_gates(self):
        row = {'candidate': 'current', 'league': 'cfb', 'game_id': 'cfb:401000001',
               'season': 2025, 'game_time': '2025-11-01T12:00:00+00:00',
               'fold': '2025-10-28', 'training_cutoff': '2025-11-01T11:00:00+00:00',
               'source_provenance': {'available_at': '2025-11-02T00:00:00+00:00'},
               'actual_margin': 3, 'predicted_margin': 20, 'predicted_total': 50,
               'baseline_margin': 2.5, 'training_games': 120,
               'home_prior_games': 6, 'away_prior_games': 6,
               'prior_validation_games': 0, 'forecast_eligible': True,
               'raw_home_conditional': 0.5, 'home_win_probability': 0.5,
               'away_win_probability': 0.5, 'calibration_training_games': 0,
               'calibration_fitted': False, 'margin_sigma': 18, 'total_sigma': 18}
        class Prices:
            quotes = {'cfb:401000001': [dict(game_id='cfb:401000001', book_key='bovada',
                                            market='moneyline', side=side, odds=odds, line=None)
                                       for side, odds in [('home', 100), ('away', -110)]]}
        bets, reasons = _candidate_decisions([row], Prices(), {'cfb:401000001': game()}, 'current', 3)
        self.assertEqual(bets, [])
        self.assertEqual(reasons['insufficient_prior_forecast_validation'], 1)
        self.assertIn('not_executable', PRICING_BASIS)


if __name__ == '__main__':
    unittest.main()
