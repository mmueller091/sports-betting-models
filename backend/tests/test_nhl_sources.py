"""Source-contract fixtures, never live network calls or invented observations."""
from datetime import date
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from data import moneypuck, nhl_goalies, nhl_stats, natural_stat_trick
from data.nhl_pipeline import _find_nhl_odds, _index_nhl_odds
from data.source_health import begin_collection, end_collection
from models.nhl_bets import _goalie_score
from models.nhl_goals import score_goal_scorer


class NHLIdentityTests(unittest.TestCase):
    def test_club_goalie_fields_and_explicit_missing_values(self):
        raw = {"playerId": 1, "firstName": {"default": "Test"}, "lastName": {"default": "Goalie"},
               "gamesStarted": 10, "gamesPlayed": 11, "savePercentage": .925, "goalsAgainstAverage": 2.1}
        parsed = nhl_stats._likely_starter([raw])
        self.assertEqual(parsed["save_pct"], .925)
        self.assertEqual(parsed["gaa"], 2.1)
        self.assertFalse(parsed["confirmed"])
        del raw["savePercentage"]
        del raw["goalsAgainstAverage"]
        token = begin_collection()
        missing = nhl_stats._likely_starter([raw])
        warnings = end_collection(token)
        self.assertIsNone(missing["save_pct"])
        self.assertIsNone(missing["gaa"])
        self.assertTrue(warnings)
        self.assertEqual(_goalie_score({"starting_goalie": missing}), 0)
        scorer = score_goal_scorer({"name": "Skater", "games_played": 10}, missing, False, {}, None)
        self.assertIsNone(scorer["opposing_goalie_sv"])

    def test_confirmation_replaces_entire_goalie_identity(self):
        first = {"id": 1, "name": "First Goalie", "save_pct": .930, "gaa": 2.0}
        second = {"id": 2, "name": "Second Goalie", "save_pct": .890, "gaa": 3.5}
        game = {"home_team": "Home", "away_team": "Away", "home_team_stats": {
            "starting_goalie": first, "goalies": [first, second],
        }}
        nhl_goalies.apply_confirmed_goalies([game], {"Home": {"name": "Second Goalie", "confirmed": True}})
        goalie = game["home_team_stats"]["starting_goalie"]
        self.assertEqual((goalie["id"], goalie["save_pct"], goalie["gaa"]), (2, .890, 3.5))
        nhl_goalies.apply_confirmed_goalies([game], {"Home": {"name": "Unmatched Goalie", "confirmed": True}})
        goalie = game["home_team_stats"]["starting_goalie"]
        self.assertIsNone(goalie["id"])
        self.assertIsNone(goalie["save_pct"])
        self.assertIsNone(goalie["gaa"])

    def test_odds_join_requires_matching_event_time(self):
        game = {"away_team": "St. Louis Blues", "home_team": "Utah Hockey Club", "game_time": "2026-11-13T02:00:00Z"}
        right = {"away_team": "St. Louis Blues", "home_team": "Utah Mammoth", "commence_time": "2026-11-13T02:00:00Z", "game_id_ext": "right"}
        wrong = {**right, "commence_time": "2026-11-15T02:00:00Z", "game_id_ext": "wrong"}
        self.assertEqual(_find_nhl_odds(game, _index_nhl_odds([right, wrong]))["game_id_ext"], "right")
        self.assertEqual(_find_nhl_odds(game, _index_nhl_odds([wrong])), {})

    def test_season_rollover_and_nst_team_column(self):
        self.assertEqual(nhl_stats.season_start(date(2026, 11, 12)), 2026)
        self.assertEqual(nhl_stats.season_start(date(2027, 2, 1)), 2026)
        html = '<table><tr><th>#</th><th>Team</th><th>GP</th><th>CF%</th><th>xGF%</th></tr><tr><td>1</td><td>Boston Bruins</td><td>10</td><td>52</td><td>54</td></tr></table>'
        self.assertEqual(natural_stat_trick._parse_nst_table(html)["Boston Bruins"]["xgf_pct"], 54)
        self.assertEqual(natural_stat_trick._parse_nst_table(html.replace('<td>54</td>', '<td>N/A</td>')), {})


class NHLSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_moneypuck_current_download_path_and_situation_selection(self):
        client = AsyncMock()
        client.get.return_value = httpx.Response(200, text='season,situation,team,games_played,xGoalsFor,xGoalsAgainst\n2026,all,BOS,10,30,25\n2026,5on4,BOS,10,999,1\n', request=httpx.Request('GET', 'https://test'))
        with patch.object(moneypuck.httpx, "AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = client
            result = await moneypuck.get_team_xg_stats(2026)
        self.assertEqual(client.get.call_args.args[0], 'https://moneypuck.com/moneypuck/playerData/seasonSummary/2026/regular/teams.csv')
        self.assertEqual(result["Boston Bruins"]["xgf_per_game"], 3)
        self.assertEqual(result["Boston Bruins"]["xga_per_game"], 2.5)
        self.assertNotIn("pdo", result["Boston Bruins"])

    async def test_goalie_counts_and_skater_rate_units(self):
        goalie = {"situation": "all", "name": "Goalie", "playerId": "1", "games_played": "5", "icetime": "18000", "xGoals": "15", "goals": "12", "highDangerShots": "10", "highDangerGoals": "2"}
        with patch.object(moneypuck, "_season_rows", AsyncMock(return_value=[goalie, {**goalie, "situation": "5on4", "xGoals": "999"}])):
            result = (await moneypuck.get_goalie_xg_stats(2026))["Goalie"]
        self.assertEqual(result["gsax"], 3)
        self.assertEqual(result["xga_per_60"], 3)
        self.assertEqual(result["hd_unblocked_no_goal_pct"], .8)
        self.assertNotIn("hd_sv_pct", result)
        skater = {"situation": "all", "name": "Skater", "playerId": "2", "games_played": "60", "icetime": "72000", "I_F_xGoals": "12", "I_F_goals": "15", "I_F_shotsOnGoal": "180"}
        with patch.object(moneypuck, "_season_rows", AsyncMock(return_value=[skater, {**skater, "situation": "5on4", "icetime": "18000"}])):
            result = (await moneypuck.get_skater_xg_stats(2026))["Skater"]
        self.assertEqual(result["ixg_per_60"], .6)
        self.assertEqual(result["toi_per_game"], 1200)
        self.assertEqual(result["shots_per_60"], 9)
        self.assertEqual(result["goals_above_expected"], 3)
        self.assertEqual(result["pp_toi_pct"], .25)

    async def test_failed_source_or_missing_schema_emits_warning(self):
        token = begin_collection()
        with patch.object(moneypuck, "_season_rows", AsyncMock(return_value=[{"situation": "all", "team": "BOS"}])):
            self.assertEqual(await moneypuck.get_team_xg_stats(2026), {})
        self.assertTrue(end_collection(token))
        token = begin_collection()
        with patch.object(natural_stat_trick.httpx, "AsyncClient", side_effect=RuntimeError("private URL")):
            self.assertEqual(await natural_stat_trick.get_team_corsi_xg(2026), {})
        self.assertNotIn("private URL", str(end_collection(token)))

    async def test_current_goalie_feed_not_used_for_historical_dates(self):
        with patch.object(nhl_goalies, "_scrape_daily_faceoff", AsyncMock()) as scrape:
            self.assertEqual(await nhl_goalies.get_confirmed_goalies(date(2020, 1, 1)), {})
        scrape.assert_not_called()

    async def test_recent_form_excludes_target_and_later_games(self):
        games = [{"gameDate": day, "gameState": "OFF", "homeTeam": {"abbrev": "BOS", "score": 3}, "awayTeam": {"abbrev": "MTL", "score": 2}}
                 for day in ("2026-11-10", "2026-11-12", "2026-11-14")]
        with patch.object(nhl_stats, "_get_club_schedule", AsyncMock(return_value={"games": games})):
            recent = await nhl_stats.get_team_schedule_last_n("BOS", 5, date(2026, 11, 12))
        self.assertEqual([game["date"] for game in recent], ["2026-11-10"])


if __name__ == "__main__":
    unittest.main()
