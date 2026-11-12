import copy
import unittest
from datetime import datetime, timedelta, timezone

from models.football_bets import (
    _candidates,
    _history,
    _qb_alerts,
    expected_value,
    find_football_bets,
    FootballRatings,
    margin_settlement,
    settlement_probabilities,
    validate_history,
)


NOW = datetime(2026, 11, 11, 17, tzinfo=timezone.utc)


def snapshot():
    return {
        "league": "nfl", "date": "2026-11-11", "week_start": "2026-11-10", "week_end": "2026-11-16",
        "teams": {"home": {"metrics": {}}, "away": {"metrics": {}}},
        "historical_games": [],
        "games": [{"game_id": "future", "game_time": "2026-11-15T18:00:00Z", "home_team_id": "home", "away_team_id": "away",
                   "home_team": "Home", "away_team": "Away", "status": "pre", "odds": {}}],
    }


class FootballProbabilityTests(unittest.TestCase):
    def test_integer_spread_preserves_push_mass_and_half_point_does_not(self):
        home_win, push, home_loss = settlement_probabilities(3, 14, 3)
        self.assertAlmostEqual(home_win, home_loss)
        self.assertGreater(push, 0)
        self.assertAlmostEqual(home_win + push + home_loss, 1)
        _, half_push, _ = settlement_probabilities(3, 14, 3.5)
        self.assertEqual(half_push, 0)

    def test_spread_sign_and_exact_book_price(self):
        state = snapshot()
        game = state["games"][0]
        game["odds"] = {
            "run_lines": {"book_a": {"home": {"line": -3, "odds": -110}}, "book_b": {"home": {"line": -7.5, "odds": 105}}},
            "market_updated_at": {"spreads": {"book_a": "2026-11-11T16:00:00Z", "book_b": "2026-11-11T15:00:00Z"}},
        }
        prediction = {"margin": 3, "total": 47, "home_score": 25, "away_score": 22}
        quotes = _candidates(game, state, prediction, 14, 14, NOW)
        a, b = quotes
        self.assertEqual((a["line"], a["odds"], a["book_key"]), (-3, -110, "book_a"))
        self.assertEqual((b["line"], b["odds"], b["book_key"]), (-7.5, 105, "book_b"))
        self.assertAlmostEqual(a["conditional_probability"], .5)
        self.assertLess(b["conditional_probability"], a["conditional_probability"])
        self.assertEqual(a["odds_age_hours"], 1)

    def test_ev_excludes_push_from_loss(self):
        self.assertAlmostEqual(expected_value(.50, .45, 100), 5)
        self.assertAlmostEqual(expected_value(.55, .45, -110), 5)

    def test_college_moneyline_cannot_push_on_tie(self):
        win, push, loss = margin_settlement(0, 18, 0, allow_ties=False)
        self.assertEqual(push, 0)
        self.assertAlmostEqual(win, .5)
        self.assertAlmostEqual(win + loss, 1)
        w, p, l = margin_settlement(2, 18, 3, allow_ties=False)
        self.assertGreater(p, 0)
        self.assertAlmostEqual(w + p + l, 1)

    def test_total_over_under_are_complements_including_push(self):
        over = settlement_probabilities(47, 14, 47, "over")
        under = settlement_probabilities(47, 14, 47, "under")
        self.assertAlmostEqual(over[0], under[2])
        self.assertAlmostEqual(over[1], under[1])
        self.assertAlmostEqual(over[0] + under[0] + over[1], 1)


class FootballDataGatesTests(unittest.TestCase):
    def test_no_odds_and_sparse_history_never_force_a_pick(self):
        result = find_football_bets(snapshot(), now=NOW)
        self.assertEqual(result["picks"], [])
        self.assertEqual(result["games"][0]["status"], "no_odds")
        self.assertTrue(result["no_bet_reason"])
        self.assertEqual(result["validation"]["holdout_games"], 0)

    def test_started_games_are_excluded(self):
        state = snapshot()
        state["games"][0]["game_time"] = "2026-11-11T16:59:00Z"
        result = find_football_bets(state, now=NOW)
        self.assertEqual(result["games"], [])

    def test_missing_quote_timestamp_is_not_fresh(self):
        state = snapshot()
        state["games"][0]["odds"] = {"moneylines": {"book": {"home": 200}}}
        result = find_football_bets(state, now=NOW)
        self.assertEqual(result["picks"], [])
        self.assertIn("Bookmaker quote timestamp is unavailable", result["watchlist"][0]["reasons"])

    def test_future_and_failed_refresh_quotes_are_blocked(self):
        state = snapshot()
        state["odds_status"] = {"stale": True}
        state["games"][0]["odds"] = {"moneylines": {"book": {"home": 200}}, "market_updated_at": {"h2h": {"book": "2026-11-12T17:00:00Z"}}}
        result = find_football_bets(state, now=NOW)
        reasons = result["watchlist"][0]["reasons"]
        self.assertIn("Bookmaker quote has an invalid future timestamp", reasons)
        self.assertIn("The latest odds refresh failed; cached prices need confirmation", reasons)

    def test_retrieval_timestamp_does_not_impersonate_book_quote_timestamp(self):
        state = snapshot()
        state['odds_status'] = {'updated_at': '2026-11-11T16:59:00Z'}
        state['games'][0]['odds'] = {
            'moneylines': {'book': {'home': 200}}, 'updated_at': '2026-11-11T16:59:00Z'}
        result = find_football_bets(state, now=NOW)
        self.assertEqual(result['picks'], [])
        pick = result['watchlist'][0]
        self.assertIsNone(pick['odds_updated_at'])
        self.assertEqual(pick['odds_retrieved_at'], '2026-11-11T16:59:00Z')
        self.assertIn('Bookmaker quote timestamp is unavailable', pick['reasons'])

    def test_incomplete_history_blocks_an_otherwise_qualifying_pick(self):
        state = snapshot()
        start = datetime(2026, 8, 2, 18, tzinfo=timezone.utc)
        for week in range(10):
            for pair in range(8):
                state["historical_games"].append({
                    "game_id": f"{week}-{pair}", "game_time": (start + timedelta(weeks=week)).isoformat(),
                    "home_team_id": str(pair * 2), "away_team_id": str(pair * 2 + 1),
                    "home_score": 35, "away_score": 14,
                })
        state["games"][0].update({"home_team_id": "0", "away_team_id": "1", "odds": {
            "moneylines": {"book": {"home": 100}},
            "market_updated_at": {"h2h": {"book": "2026-11-11T16:00:00Z"}},
        }})
        complete = find_football_bets(state, now=NOW)
        self.assertEqual(len(complete["picks"]), 1)
        state["history_complete"] = False
        incomplete = find_football_bets(state, now=NOW)
        self.assertEqual(incomplete["picks"], [])
        self.assertIn("Season history is incomplete because a source fetch failed", incomplete["watchlist"][0]["reasons"])

    def test_current_week_results_never_enter_training(self):
        state = snapshot()
        old = {"game_id": "old", "game_time": "2026-11-08T18:00:00Z", "home_team_id": "home", "away_team_id": "away", "home_score": 28, "away_score": 7}
        new = {**old, "game_id": "new", "game_time": "2026-11-10T18:00:00Z", "home_score": 7, "away_score": 50}
        state["historical_games"] = [old, new, old]
        history = _history(state, NOW)
        self.assertEqual([row["game_id"] for row in history], ["old"])

    def test_qb_report_is_a_flag_not_an_invented_point_penalty(self):
        alerts = _qb_alerts({"injuries": [{"athlete": {"displayName": "A Quarterback", "position": {"abbreviation": "QB"}}, "status": "Questionable"}]})
        self.assertEqual(len(alerts), 1)
        self.assertIn("A Quarterback", alerts[0])

    def test_known_backup_injury_does_not_impersonate_primary_passer(self):
        team = {"recent_primary_passer": "C.Williams", "injuries": [
            {"name": "Case Keenum", "position": "QB", "status": "Out", "detail": "coach's decision; emergency No. 3 quarterback"},
            {"name": "Backup Player", "position": "QB", "status": "Injured Reserve"},
        ]}
        self.assertEqual(_qb_alerts(team), [])
        team["injuries"].append({"name": "Caleb Williams", "position": "QB", "status": "Questionable"})
        self.assertEqual(len(_qb_alerts(team)), 1)

    def test_temporal_validation_counts_only_later_weeks(self):
        state = snapshot()
        start = datetime(2026, 8, 2, 18, tzinfo=timezone.utc)
        # Eight independent games each week, with repeat teams to provide history.
        for week in range(10):
            for pair in range(8):
                state["historical_games"].append({"game_id": f"{week}-{pair}", "game_time": (start + timedelta(weeks=week)).isoformat(),
                    "home_team_id": str(pair * 2), "away_team_id": str(pair * 2 + 1), "home_score": 24 + week % 3, "away_score": 20 + pair % 2})
        history = _history(state, NOW)
        validation = validate_history(history, "nfl")
        self.assertEqual(validation["training_games"], 80)
        self.assertEqual(validation["holdout_games"], 40)
        self.assertTrue(all(fold["training_games"] >= 40 for fold in validation["folds"]))
        self.assertIn("betting profitability", validation["notes"])

    def test_same_week_play_metrics_cannot_change_fitted_model(self):
        state = snapshot()
        start = datetime(2026, 8, 2, 18, tzinfo=timezone.utc)
        metrics = {"epa_per_play": .1, "defensive_epa_per_play": -.1, "plays": 60,
                   "defensive_plays": 60, "dropbacks": 35, "defensive_dropbacks": 35,
                   "pass_epa": .15, "defensive_pass_epa": -.15, "success_rate": .5,
                   "defensive_success_rate": .4, "sack_rate": .05, "pressure_sack_rate": .08}
        for week in range(5):
            for pair in range(8):
                state["historical_games"].append({"game_id": f"{week}-{pair}", "game_time": (start + timedelta(weeks=week)).isoformat(),
                    "home_team_id": str(pair * 2), "away_team_id": str(pair * 2 + 1), "home_score": 24 + week % 3, "away_score": 20 + pair % 2,
                    "home_metrics": copy.deepcopy(metrics), "away_metrics": copy.deepcopy(metrics)})
        original = _history(state, NOW)
        changed = copy.deepcopy(original)
        for game in changed[-8:]:
            game["home_metrics"]["epa_per_play"] = 99
            game["home_metrics"]["success_rate"] = 1
        a, b = FootballRatings(original, "nfl"), FootballRatings(changed, "nfl")
        self.assertTrue(a.advanced)
        # Last-week plays can inform the next prediction, but never the fit on
        # that same week's final scores. Coefficients therefore remain identical.
        self.assertTrue((a.margin_model.coef_ == b.margin_model.coef_).all())
        self.assertTrue((a.score_model.coef_ == b.score_model.coef_).all())


if __name__ == "__main__":
    unittest.main()
