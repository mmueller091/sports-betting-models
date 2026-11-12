"""Structured fixture excerpt from the observed DailyFaceoff page, 2026-11-12.

Observed HTML SHA256: be5f01b622555390369b89b182c5f74961965043a3da3d02c8b66f87f3b21990.
Only fields consumed by the parser are retained; no fixture status is inferred.
"""
import copy
import json
import unittest
from datetime import date

from data.nhl_goalies import _parse_daily_faceoff_html, apply_confirmed_goalies
from data.source_health import begin_collection, end_collection


OBSERVED = {"props": {"pageProps": {"date": "2026-11-12", "specificDate": False, "data": [
    {"date": "2026-11-12", "dateGmt": "2026-11-13T00:00:00.000Z",
     "homeTeamId": 3, "homeTeamName": "Boston Bruins", "homeGoalieId": 28098,
     "homeGoalieName": "Jeremy Swayman", "homeNewsStrengthName": "Confirmed",
     "homeNewsCreatedAt": "2026-11-12T16:15:59.063Z", "homeNewsSourceName": "Jack Studley",
     "homeNewsSourceUrl": "https://x.com/jackstudley13/status/2120913189378650322?a=Q1jxu0AHFUu51RQaE5o&s=20",
     "awayTeamId": 16, "awayTeamName": "Montreal Canadiens", "awayGoalieId": 38444,
     "awayGoalieName": "Jacob Fowler", "awayNewsStrengthName": "Confirmed",
     "awayNewsCreatedAt": "2026-11-11T17:40:10.235Z", "awayNewsSourceName": "Stu Cowan",
     "awayNewsSourceUrl": "https://x.com/StuCowan1/status/2120571742905315526?a=vb5KDXimVspLHLTELsQ&s=20"},
    {"date": "2026-11-12", "dateGmt": "2026-11-13T00:00:00.000Z",
     "homeTeamId": 9, "homeTeamName": "Columbus Blue Jackets", "homeGoalieId": 29751,
     "homeGoalieName": "Jet Greaves", "homeNewsStrengthName": "Confirmed",
     "homeNewsCreatedAt": "2026-11-12T16:20:00.080Z", "homeNewsSourceName": "Aaron Portzline",
     "homeNewsSourceUrl": "https://x.com/Aportzline/status/2120914157566623947?a=ulRAIWvIXb1CIBRWlZg&s=20",
     "awayTeamId": 27, "awayTeamName": "Tampa Bay Lightning", "awayGoalieId": 36477,
     "awayGoalieName": "Dennis Hildeby", "awayNewsStrengthName": None,
     "awayNewsCreatedAt": None, "awayNewsSourceName": None, "awayNewsSourceUrl": None},
    {"date": "2026-11-12", "dateGmt": "2026-11-13T01:00:00.000Z",
     "homeTeamName": "Dallas Stars", "homeGoalieName": "Jake Oettinger", "homeNewsStrengthName": "Confirmed",
     "homeNewsCreatedAt": "2026-11-12T16:34:34.582Z", "homeNewsSourceName": "Bruce LeVine",
     "awayTeamName": "Winnipeg Jets", "awayGoalieName": "Stuart Skinner", "awayNewsStrengthName": "Likely",
     "awayNewsCreatedAt": "2026-11-12T17:32:45.300Z"}
]}}}


def page(payload):
    return '<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + '</script></html>'


class NHLGoalieFeedTests(unittest.TestCase):
    def parse(self, payload):
        return _parse_daily_faceoff_html(page(payload), date(2026, 11, 12))

    def test_observed_rows_bind_their_own_team_player_and_status(self):
        goalies = self.parse(OBSERVED)
        self.assertEqual(len(goalies), 6)
        self.assertEqual(goalies["Boston Bruins"]["name"], "Jeremy Swayman")
        self.assertEqual(goalies["Montreal Canadiens"]["name"], "Jacob Fowler")
        self.assertTrue(goalies["Boston Bruins"]["confirmed"])
        self.assertEqual(goalies["Montreal Canadiens"]["news_updated_at"], "2026-11-11T17:40:10.235Z")
        self.assertEqual(goalies["Boston Bruins"]["game_time"], "2026-11-13T00:00:00.000Z")
        self.assertIn("/2120913189378650322", goalies["Boston Bruins"]["news_source_url"])
        self.assertEqual(goalies["Tampa Bay Lightning"]["name"], "Dennis Hildeby")
        self.assertFalse(goalies["Tampa Bay Lightning"]["confirmed"])
        self.assertEqual(goalies["Tampa Bay Lightning"]["status"], "Unconfirmed")
        self.assertFalse(goalies["Winnipeg Jets"]["confirmed"])
        self.assertEqual(goalies["Winnipeg Jets"]["status"], "Likely")

    def test_confirmed_is_an_exact_case_normalized_status(self):
        data = copy.deepcopy(OBSERVED)
        row = data["props"]["pageProps"]["data"][0]
        row["homeNewsStrengthName"] = "  cOnFiRmEd  "
        row["awayNewsStrengthName"] = "Unconfirmed"
        goalies = self.parse(data)
        self.assertTrue(goalies["Boston Bruins"]["confirmed"])
        self.assertFalse(goalies["Montreal Canadiens"]["confirmed"])

    def test_stale_page_and_stale_or_invalid_matchups_are_omitted(self):
        stale_page = copy.deepcopy(OBSERVED)
        stale_page["props"]["pageProps"]["date"] = "2026-11-11"
        self.assertEqual(self.parse(stale_page), {})
        data = copy.deepcopy(OBSERVED)
        rows = data["props"]["pageProps"]["data"]
        rows[0]["date"] = "2026-11-11"
        rows[1]["dateGmt"] = "2026-11-14T00:00:00Z"
        token = begin_collection()
        goalies = self.parse(data)
        warnings = end_collection(token)
        self.assertEqual(set(goalies), {"Dallas Stars", "Winnipeg Jets"})
        self.assertEqual(len(warnings), 2)

    def test_legacy_separate_lists_are_not_zipped_when_structured_data_is_missing(self):
        html = '<span class="team-name">Boston Bruins</span><span class="team-name">Montreal Canadiens</span>'
        html += '<span class="goalie-name">Jeremy Swayman</span><span class="goalie-status">Confirmed</span>'
        token = begin_collection()
        result = _parse_daily_faceoff_html(html, date(2026, 11, 12))
        warnings = end_collection(token)
        self.assertEqual(result, {})
        self.assertIn("structured matchup", warnings[0])

    def test_a_missing_player_does_not_shift_other_team_records(self):
        data = copy.deepcopy(OBSERVED)
        data["props"]["pageProps"]["data"][0]["homeGoalieName"] = None
        result = self.parse(data)
        self.assertNotIn("Boston Bruins", result)
        self.assertEqual(result["Montreal Canadiens"]["name"], "Jacob Fowler")
        self.assertEqual(result["Columbus Blue Jackets"]["name"], "Jet Greaves")

    def test_conflicting_duplicate_team_is_not_promoted(self):
        data = copy.deepcopy(OBSERVED)
        duplicate = copy.deepcopy(data["props"]["pageProps"]["data"][0])
        duplicate["homeGoalieName"] = "Different Goalie"
        data["props"]["pageProps"]["data"].append(duplicate)
        result = self.parse(data)
        self.assertNotIn("Boston Bruins", result)
        self.assertEqual(result["Montreal Canadiens"]["name"], "Jacob Fowler")

    def test_provider_ids_do_not_replace_matched_nhl_ids_and_metadata_is_preserved(self):
        records = self.parse(OBSERVED)
        self.assertNotIn("id", records["Boston Bruins"])
        game = {"home_team": "Boston Bruins", "away_team": "Other", "home_team_stats": {
            "starting_goalie": {"id": 1, "name": "Different Goalie", "save_pct": .95},
            "goalies": [{"id": 8480280, "name": "Jeremy Swayman", "save_pct": .92}]}}
        apply_confirmed_goalies([game], records)
        starter = game["home_team_stats"]["starting_goalie"]
        self.assertEqual(starter["id"], 8480280)
        self.assertEqual(starter["dailyfaceoff_goalie_id"], 28098)
        self.assertEqual(starter["save_pct"], .92)
        self.assertEqual(starter["news_source_url"], records["Boston Bruins"]["news_source_url"])
        self.assertEqual(starter["game_date"], "2026-11-12")


if __name__ == "__main__":
    unittest.main()
