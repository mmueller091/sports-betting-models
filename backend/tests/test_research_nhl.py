import copy
from datetime import datetime, timedelta, timezone
import unittest

from research.nhl_replay import NHLReplayConfig, run_nhl_replay, train_nhl_artifact
from research.evaluation import evaluate_forecasts


def games():
    start = datetime(2022, 10, 1, 22, tzinfo=timezone.utc)
    output = []
    for day in range(0, 60, 2):
        for pair in range(3):
            at = start+timedelta(days=day, minutes=pair*20)
            home, away = str(pair*2), str(pair*2+1)
            if day % 4:
                home, away = away, home
            home_score, away_score = 2+day%3, 1+pair%2
            if home_score == away_score:
                home_score += 1
            output.append({'league': 'nhl', 'game_id': f'{day}-{pair}', 'season': 2022,
                           'home_team_id': home, 'away_team_id': away,
                           'home_score': home_score, 'away_score': away_score,
                           'game_time': at.isoformat(), 'available_at': (at+timedelta(hours=12)).isoformat(),
                           'completed': True})
    return output


class NHLResearchTests(unittest.TestCase):
    def setUp(self):
        self.config = NHLReplayConfig(minimum_training_games=6, minimum_forecast_team_games=2)

    def test_future_outcomes_cannot_change_prior_forecasts_or_fit(self):
        data = games()
        original = run_nhl_replay(data, config=self.config)
        changed = copy.deepcopy(data)
        for game in changed[-9:]:
            game['home_score'] = 12
        replay = run_nhl_replay(changed, config=self.config)
        cutoff = changed[-9]['game_time']
        earlier = [(a, b) for a, b in zip(original, replay) if a['game_time'] < cutoff]
        self.assertTrue(any(a['forecast_eligible'] for a, _ in earlier))
        for a, b in earlier:
            self.assertEqual(a['predicted_total'], b['predicted_total'])
            self.assertEqual(a['home_win_probability'], b['home_win_probability'])
            self.assertEqual(a['training_source_hash'], b['training_source_hash'])
            if a['latest_feature_time']:
                self.assertLess(a['latest_feature_time'], a['training_cutoff'])

    def test_delayed_result_unavailable_to_training_and_artifact(self):
        data = games()
        data[0]['available_at'] = '2023-01-01T00:00:00Z'
        artifact = train_nhl_artifact(data, '2022-12-10T00:00:00Z', self.config)
        self.assertEqual(artifact['training_games'], len(data)-1)
        self.assertIsNotNone(artifact['coefficients'])
        self.assertLess(artifact['latest_feature_time'], artifact['training_cutoff'])

    def test_price_free_replay_does_not_invent_profit(self):
        rows = run_nhl_replay(games(), config=self.config)
        metrics = evaluate_forecasts(rows, bootstrap_samples=20)
        self.assertTrue(all(row['bet'] is None for row in rows))
        self.assertIsNone(metrics['overall']['betting']['profit_units'])
        self.assertIsNone(metrics['overall']['betting']['roi'])
        for row in rows:
            self.assertAlmostEqual(row['home_win_probability']+row['away_win_probability'], 1)
            self.assertEqual(row['push_probability'], 0)

    def test_regulation_only_tied_score_is_not_a_full_game_label(self):
        data = games()
        data[0]['home_score'] = data[0]['away_score']
        with self.assertRaisesRegex(ValueError, 'cannot be tied'):
            run_nhl_replay(data, config=self.config)

    def test_closing_only_quote_cannot_become_an_entry(self):
        data = games()
        target = data[-1]
        at = datetime.fromisoformat(target['game_time'])-timedelta(minutes=70)
        quote = {'game_id': target['game_id'], 'market': 'moneyline', 'side': 'home',
                 'line': None, 'odds': 10000, 'book_key': 'test_book', 'quote_time': at.isoformat(),
                 'is_closing': True}
        rows = run_nhl_replay(data, quotes=[quote], config=self.config)
        self.assertTrue(rows[-1]['forecast_eligible'])
        self.assertIsNone(rows[-1]['bet'])


if __name__ == '__main__':
    unittest.main()
