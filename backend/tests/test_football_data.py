import csv
import gzip
import io
import unittest
from datetime import date, datetime, timezone

from data.football import (
    _aggregate_nflverse, _attach_nfl_metrics, _attach_odds,
    _parse_event, _parse_injuries, _parse_team_metrics, _select_week,
)


class FootballDataTests(unittest.TestCase):
    def test_league_calendar_selects_nfl_week_across_monday(self):
        board = {"leagues": [{"calendar": [{"value": "2", "entries": [
            {"value": "10", "label": "Week 10", "startDate": "2026-11-11T08:00Z", "endDate": "2026-11-18T07:59Z"},
            {"value": "11", "label": "Week 11", "startDate": "2026-11-18T08:00Z", "endDate": "2026-11-25T07:59Z"},
        ]}]}]}
        week, _, _ = _select_week(board, date(2026, 11, 16))
        self.assertEqual(week["number"], 10)
        following, _, _ = _select_week(board, date(2026, 11, 18))
        self.assertEqual(following["number"], 11)
        with self.assertRaises(ValueError):
            _select_week({"events": []}, date(2026, 11, 16))

    def test_parse_event_uses_explicit_home_away_and_completion(self):
        event = {"id": "123", "date": "2026-11-16T01:20Z", "season": {"year": 2026, "type": 2},
                 "status": {"type": {"state": "post", "completed": True}}, "competitions": [{
                     "neutralSite": True, "competitors": [
                         {"homeAway": "away", "score": "17", "team": {"id": "2", "displayName": "Away"}},
                         {"homeAway": "home", "score": {"value": 21}, "team": {"id": "1", "displayName": "Home"}},
                     ]}]}
        game, teams = _parse_event(event)
        self.assertEqual(game["home_team_id"], "1")
        self.assertEqual(game["away_score"], 17)
        self.assertTrue(game["neutral_site"])
        self.assertTrue(game["completed"])
        self.assertEqual(set(teams), {"1", "2"})

    def test_old_cfb_injuries_are_not_treated_as_current_absences(self):
        payload = {"injuries": [{"id": "333", "injuries": [
            {"date": "2020-11-21T18:31Z", "status": "Out", "athlete": {"displayName": "Former Player"}},
            {"date": "2026-11-11T18:31Z", "status": "Questionable", "athlete": {"displayName": "Current Player"}},
        ]}]}
        injuries, current, ignored = _parse_injuries(payload, 2026, datetime(2026, 11, 12, tzinfo=timezone.utc))
        self.assertEqual((current, ignored), (1, 1))
        self.assertEqual(injuries["333"][0]["name"], "Current Player")

    def test_odds_identity_must_also_match_kickoff(self):
        teams = {"1": {"aliases": ["Home"]}, "2": {"aliases": ["Away"]}}
        game = {"home_team_id": "1", "away_team_id": "2", "game_time": "2026-11-15T18:00:00Z", "odds": {}}
        previous_week = {"home_team": "Home", "away_team": "Away", "commence_time": "2026-11-08T18:00:00Z"}
        _attach_odds([game], [previous_week], teams, {"updated_at": "2026-11-11T18:00Z"})
        self.assertEqual(game["odds"], {})
        current = {**previous_week, "commence_time": game["game_time"]}
        _attach_odds([game], [current], teams, {"updated_at": "2026-11-11T18:00Z"})
        self.assertEqual(game["odds"]["commence_time"], game["game_time"])

    def test_missing_efficiency_inputs_remain_missing(self):
        data = {"season": {"year": 2026}, "results": {"stats": {"categories": [
            {"name": "passing", "stats": [{"name": "teamGamesPlayed", "value": 8}, {"name": "netPassingYards", "value": 1000}]},
            {"name": "rushing", "stats": [{"name": "rushingYards", "value": 600}, {"name": "totalYards", "value": 1700}, {"name": "totalOffensivePlays", "value": 320}]},
        ]}, "opponent": []}}
        games, metrics = _parse_team_metrics(data, 2026, "cfb")
        self.assertEqual(games, 8)
        self.assertEqual(metrics["yards_per_play"], 5)
        self.assertIsNone(metrics["yards_per_play_allowed"])
        self.assertNotIn("red_zone_td_pct", metrics)
        with self.assertRaises(ValueError):
            _parse_team_metrics(data, 2025, "cfb")

    def test_nfl_play_filters_preserve_full_game_pace_and_defense_weights(self):
        base = {"game_id": "2026_01_ARI_LA", "game_date": "2026-09-13", "home_team": "LA", "away_team": "ARI", "season_type": "REG",
                "posteam": "ARI", "defteam": "LA", "epa": "1", "play_type": "pass", "wp": ".5", "qb_kneel": "0", "qb_spike": "0",
                "qb_dropback": "1", "yards_gained": "25", "sack": "0", "spread_line": "3.5", "total_line": "45.5", "passer_player_name": "K.Murray", "passer_player_id": "1"}
        rows = [base,
                {**base, "wp": ".99", "epa": "3"},
                {**base, "qb_kneel": "1", "play_type": "run"},
                {**base, "posteam": "LA", "defteam": "ARI", "epa": "-1", "yards_gained": "-7", "sack": "1", "passer_player_name": "M.Stafford", "passer_player_id": "2"}]
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=list(base))
        writer.writeheader()
        writer.writerows(rows)
        result = _aggregate_nflverse(gzip.compress(stream.getvalue().encode()))
        away = next(row for row in result if row["team"] == "ARI")
        self.assertEqual(away["total_plays"], 2)
        self.assertEqual(away["plays"], 1)
        self.assertEqual(away["epa_per_play"], 1)
        self.assertEqual(away["defensive_plays"], 1)
        self.assertEqual(away["defensive_epa_per_play"], -1)
        self.assertEqual(away["pressure_sack_rate"], 1)
        teams = {"22": {"abbreviation": "ARI", "metrics": {}, "source_coverage": []},
                 "14": {"abbreviation": "LAR", "metrics": {}, "source_coverage": []}}
        history = [{"game_time": "2026-09-13T20:00Z", "home_team_id": "14", "away_team_id": "22"}]
        self.assertEqual(_attach_nfl_metrics(history, teams, result, "2026-09-16"), 1)
        self.assertEqual(history[0]["closing_home_spread"], -3.5)
        self.assertEqual(history[0]["closing_total"], 45.5)
        self.assertEqual(teams["22"]["recent_primary_passer"], "K.Murray")
        self.assertEqual(_attach_nfl_metrics([], teams, result, "2026-09-13"), 0)


if __name__ == "__main__":
    unittest.main()
