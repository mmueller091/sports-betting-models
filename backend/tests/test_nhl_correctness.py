"""Numerical and quote-integrity regressions for the provisional NHL model."""
import copy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from models.nhl_bets import (
    MODEL_VERSION, calculate_ev, estimate_nhl_win_prob, find_nhl_best_bets,
    market_probabilities, nhl_score_distribution,
)
from models.nhl_goals import find_nhl_goal_scorers


NOW = datetime(2026, 11, 12, 16, tzinfo=timezone.utc)


def game_fixture():
    return {
        "game_id": 1, "away_team": "Away", "home_team": "Home",
        "game_time": "2026-11-13T00:00:00Z", "status": "FUT",
        "odds": {
            "game_id_ext": "market-event-1",
            "moneylines": {"book-a": {"home": 150, "away": -170}, "book-b": {"home": 140, "away": -160}},
            "market_updated_at": {"h2h": {"book-a": "2026-11-12T15:00:00Z", "book-b": "2026-11-12T15:30:00Z"}},
        },
    }


class NHLProbabilityTests(unittest.TestCase):
    def test_distribution_preserves_probability_total_and_subsets(self):
        for probability in (.24, .5, .76):
            for total in (3.5, 6.1, 8.0):
                with self.subTest(probability=probability, total=total):
                    distribution = nhl_score_distribution(probability, total)
                    self.assertAlmostEqual(sum(p for _, _, p in distribution), 1, places=10)
                    self.assertAlmostEqual(sum((h + a) * p for h, a, p in distribution), total, places=7)
                    self.assertTrue(all(h != a and p >= 0 for h, a, p in distribution))
                    outright = market_probabilities(distribution, "moneyline", "home")
                    cover = market_probabilities(distribution, "spread", "home", -1.5)
                    plus_half = market_probabilities(distribution, "spread", "home", .5)
                    self.assertAlmostEqual(outright["win"], probability, places=8)
                    self.assertLessEqual(cover["win"], outright["win"])
                    self.assertAlmostEqual(plus_half["win"], outright["win"], places=10)

    def test_integer_lines_retain_push_mass_and_complementary_sides(self):
        distribution = nhl_score_distribution(.6, 6.2)
        over = market_probabilities(distribution, "total", "over", 6)
        under = market_probabilities(distribution, "total", "under", 6)
        self.assertGreater(over["push"], 0)
        self.assertAlmostEqual(over["push"], under["push"])
        self.assertAlmostEqual(over["win"] + under["win"] + over["push"], 1)
        spread = market_probabilities(distribution, "spread", "home", -1)
        self.assertGreater(spread["push"], 0)
        self.assertAlmostEqual(sum(spread.values()), 1)
        self.assertEqual(calculate_ev(.45, 100, .10), 0)
        self.assertEqual(calculate_ev(.45, 100), -10)

    def test_infeasible_probability_total_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "incompatible"):
            nhl_score_distribution(.99, 1.1)

    def test_better_goalie_increases_own_win_probability(self):
        game = game_fixture()
        for side in ("away", "home"):
            game[f"{side}_team_stats"] = {"starting_goalie": {"save_pct": .906, "gaa": 3.0}}
        baseline = estimate_nhl_win_prob(game)[1]
        better = copy.deepcopy(game)
        better["home_team_stats"]["starting_goalie"].update(save_pct=.930, gaa=2.0)
        worse = copy.deepcopy(game)
        worse["home_team_stats"]["starting_goalie"].update(save_pct=.880, gaa=4.0)
        self.assertGreater(estimate_nhl_win_prob(better)[1], baseline)
        self.assertLess(estimate_nhl_win_prob(worse)[1], baseline)

    def test_totals_cannot_qualify_with_negative_ev(self):
        game = game_fixture()
        game["odds"] = {"totals": {"book": {
            "over": {"line": 5.5, "odds": -120}, "under": {"line": 5.5, "odds": -120},
        }}}
        # The previous no-vig gate selected 53.6% at -120 despite negative EV.
        with patch("models.nhl_bets.market_probabilities", return_value={"win": .536, "push": 0, "loss": .464}):
            self.assertEqual(find_nhl_best_bets({"games": [game]}, now=NOW), [])

    def test_one_probability_drives_ev_and_book_provenance(self):
        game = game_fixture()
        picks = find_nhl_best_bets({"games": [game]}, now=NOW)
        self.assertEqual(len(picks), 1)
        pick = picks[0]
        self.assertEqual(pick["book"], "book-a")
        self.assertEqual(pick["odds"], 150)
        self.assertEqual(pick["odds_updated_at"], "2026-11-12T15:00:00Z")
        self.assertEqual(pick["odds_event_id"], "market-event-1")
        self.assertEqual(pick["model_version"], MODEL_VERSION)
        self.assertEqual(pick["ev_per_100"], calculate_ev(pick["our_prob"], pick["odds"], pick["push_probability"]))
        self.assertAlmostEqual(pick["our_prob"] + pick["push_probability"] + pick["loss_probability"], 1)
        boosted = copy.deepcopy(game)
        boosted["line_movement"] = {"sharp_signal": "home", "sharp_signal_desc": "claimed sharp action"}
        boosted["odds"]["market_metrics"] = {"best_vs_consensus_home": .2}
        self.assertEqual(picks, find_nhl_best_bets({"games": [boosted]}, now=NOW))

    def test_pregame_model_does_not_select_started_games(self):
        game = game_fixture()
        self.assertTrue(find_nhl_best_bets({"games": [game]}, now=NOW))
        game["status"] = "LIVE"
        self.assertEqual(find_nhl_best_bets({"games": [game]}, now=NOW), [])
        game["status"] = "FUT"
        game["game_time"] = NOW.isoformat()
        self.assertEqual(find_nhl_best_bets({"games": [game]}, now=NOW), [])

    def test_invalid_prices_and_non_halfpoint_lines_are_not_picks(self):
        game = game_fixture()
        for bad_price in (0, 90, -99, float("nan"), float("inf")):
            game["odds"] = {"moneylines": {"book": {"home": bad_price}}}
            self.assertEqual(find_nhl_best_bets({"games": [game]}, now=NOW), [])
        for bad_line in (float("nan"), float("inf"), 5.25, -1):
            game["odds"] = {"totals": {"book": {"over": {"line": bad_line, "odds": 1000}}}}
            self.assertEqual(find_nhl_best_bets({"games": [game]}, now=NOW), [])

    def test_scorer_recommendations_are_pregame_only(self):
        game = game_fixture()
        game["away_scorers"] = [{"id": 7, "name": "Skater", "games_played": 10, "goals": 4, "shots": 30}]
        self.assertEqual(len(find_nhl_goal_scorers({"games": [game]}, now=NOW)), 1)
        game["status"] = "LIVE"
        self.assertEqual(find_nhl_goal_scorers({"games": [game]}, now=NOW), [])
        game["status"] = "FUT"
        game["game_time"] = NOW.isoformat()
        self.assertEqual(find_nhl_goal_scorers({"games": [game]}, now=NOW), [])


if __name__ == "__main__":
    unittest.main()
