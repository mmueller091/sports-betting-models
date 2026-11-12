import unittest

from models.hit_props import (
    find_best_bts_pair,
    plate_appearance_distribution,
    score_batter_hit_prob,
)


def batter(order=1, avg=0.300, hits=120, at_bats=400):
    return {
        "id": order,
        "name": f"Hitter {order}",
        "batting_order": order,
        "bats": "R",
        "stats": {
            "avg": avg,
            "hits": hits,
            "at_bats": at_bats,
            "plate_appearances": int(at_bats / 0.88),
            "ops": 0.820,
        },
        "previous_stats": {
            "avg": 0.290,
            "hits": 160,
            "at_bats": 550,
            "plate_appearances": 625,
            "ops": 0.800,
        },
        "recent": {"hits": 18, "at_bats": 60},
        "xstats": {"xba": avg - 0.005},
        "vs_hand": {"vs_rhp": {"avg": avg - 0.010, "ab": 250}},
        "vs_pitcher": {},
        "is_home": False,
    }


def pitcher():
    return {
        "hand": "R",
        "season_stats": {
            "avg_against": 0.248,
            "batters_faced": 500,
            "innings_pitched": 120,
            "games_started": 22,
        },
        "previous_stats": {
            "avg_against": 0.245,
            "batters_faced": 700,
            "innings_pitched": 170,
            "games_started": 30,
        },
        "splits": {"vs_rhb": {"avg": 0.247, "ab": 300}},
        "xstats": {"xba_against": 0.246},
    }


def context():
    return {
        "bullpen_quality": {
            "bullpen_avg_against": 0.246,
            "bullpen_ip": 300,
            "bullpen_batters_faced": 1250,
        },
        "bullpen": {"fatigue_index": 0.5},
    }


class HitModelTests(unittest.TestCase):
    def score(self, player):
        return score_batter_hit_prob(
            player,
            pitcher(),
            park_factor=100,
            weather_adj=0,
            lineup_support=0,
            opposing_team_context=context(),
            lineup_confirmed=True,
        )

    def test_pa_distribution_is_normalized_and_centered(self):
        distribution = plate_appearance_distribution(4.4)
        mean = sum(pa * probability for pa, probability in distribution.items())
        self.assertAlmostEqual(sum(distribution.values()), 1.0, places=8)
        self.assertAlmostEqual(mean, 4.4, delta=0.08)

    def test_earlier_lineup_position_has_higher_hit_probability(self):
        leadoff = self.score(batter(order=1))
        eighth = self.score(batter(order=8))
        self.assertGreater(leadoff["hit_probability"], eighth["hit_probability"])

    def test_stronger_hitter_has_higher_hit_probability(self):
        strong = self.score(batter(avg=0.320, hits=128))
        average = self.score(batter(avg=0.250, hits=100))
        self.assertGreater(strong["hit_probability"], average["hit_probability"])

    def test_small_bvp_sample_cannot_dominate(self):
        baseline_player = batter()
        extreme_player = batter()
        extreme_player["vs_pitcher"] = {"pa": 20, "hits": 20}
        baseline = self.score(baseline_player)
        extreme = self.score(extreme_player)
        self.assertLess(extreme["hit_probability"] - baseline["hit_probability"], 0.015)

    def test_pair_uses_joint_probability(self):
        candidates = [
            {"batter_id": 1, "name": "A", "hit_probability": 0.80, "data_quality": 90},
            {"batter_id": 2, "name": "B", "hit_probability": 0.78, "data_quality": 90},
            {"batter_id": 3, "name": "C", "hit_probability": 0.70, "data_quality": 95},
        ]
        pair = find_best_bts_pair(candidates)
        self.assertEqual([p["name"] for p in pair["players"]], ["A", "B"])
        self.assertAlmostEqual(pair["joint_probability"], 0.624)


if __name__ == "__main__":
    unittest.main()
