import copy
import csv
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from research.datasets import (
    parse_cfb_schedule, parse_espn_scoreboard, parse_nflverse_schedule,
    read_normalized_games, write_normalized_games,
)
from research.downloads import CachedDownloader, DownloadRequestError
from research.odds_history import (
    exact_contract_quote, fetch_historical_requests, normalize_fetched_manifest,
    normalize_historical_quotes, plan_historical_requests,
)


def csv_bytes(rows):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def game_fixture():
    return {"league": "nfl", "game_id": "nfl:test", "game_time": "2024-11-03T19:00:00Z",
            "home_team": "Home Team", "away_team": "Away Team", "completed": True}


def odds_fixture(timestamp="2024-11-03T18:00:00Z"):
    return {"timestamp": timestamp, "data": [{
        "id": "provider-event", "sport_key": "americanfootball_nfl", "commence_time": "2024-11-03T19:00:00Z",
        "home_team": "Home Team", "away_team": "Away Team", "bookmakers": [{
            "key": "book_a", "title": "Book A", "last_update": "2024-11-03T17:59:00Z",
            "markets": [{"key": "h2h", "outcomes": [{"name": "Home Team", "price": -110}, {"name": "Away Team", "price": 100}]}]}]}]}


class HistoricalSourceTests(unittest.TestCase):
    def test_nfl_schedule_uses_eastern_kickoff_and_marks_closing_data_as_labels(self):
        row = {"game_id": "2021_10_TB_WAS", "season": "2021", "game_type": "REG", "week": "10",
               "gameday": "2021-11-14", "gametime": "13:00", "home_team": "WAS", "away_team": "TB",
               "home_score": "29", "away_score": "19", "location": "Home", "spread_line": "-9.5",
               "total_line": "51.5", "home_moneyline": "350", "away_moneyline": "-450"}
        game = parse_nflverse_schedule(csv_bytes([row]), [2021], {"retrieved_at": "2026-11-12T12:00:00Z"})[0]
        self.assertEqual(game["game_time"], "2021-11-14T18:00:00+00:00")
        self.assertEqual(game["available_at"], "2021-11-15T06:00:00+00:00")
        self.assertEqual(game["home_team_id"], "nfl:WSH")
        self.assertEqual(game["home_team"], "Washington Football Team")
        self.assertEqual(game["closing_home_spread"], 9.5)
        self.assertIsNone(game["closing_quote_time"])
        self.assertNotIn("home_metrics", game)

    def test_cfb_does_not_import_postgame_ratings_or_lower_division_only_games(self):
        row = {"game_id": "123", "season": "2024", "start_date": "2024-09-07T16:00:00Z",
               "home_id": "1", "away_id": "2", "home_points": "35", "away_points": "14",
               "home_team": "Home", "away_team": "Away", "home_division": "fbs", "away_division": "fcs",
               "completed": "TRUE", "neutral_site": "FALSE", "home_postgame_elo": "1700", "home_post_win_prob": ".97"}
        rows = parse_cfb_schedule(csv_bytes([row, {**row, "game_id": "124", "home_division": "fcs"}]), [2024])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["completed"])
        self.assertFalse(rows[0]["neutral_site"])
        self.assertNotIn("home_postgame_elo", rows[0])
        self.assertNotIn("home_post_win_prob", rows[0])

    def test_nhl_exhibitions_are_excluded_and_final_ot_scores_remain_explicit(self):
        event = {"id": "101", "date": "2022-01-01T18:00Z", "season": {"year": 2022, "type": 2},
                 "status": {"type": {"completed": True, "description": "Final/OT"}},
                 "competitions": [{"competitors": [
                     {"homeAway": "home", "score": "3", "team": {"id": "1", "displayName": "Boston Bruins"},
                      "linescores": [{"value": 1}, {"value": 1}, {"value": 0}, {"value": 1}]},
                     {"homeAway": "away", "score": "2", "team": {"id": "2", "displayName": "Buffalo Sabres"},
                      "linescores": [{"value": 1}, {"value": 0}, {"value": 1}, {"value": 0}]}]}]}
        exhibition = copy.deepcopy(event)
        exhibition["id"] = "102"
        exhibition["competitions"][0]["competitors"][0]["team"]["id"] = "110065"
        postponed = copy.deepcopy(event)
        postponed["id"] = "103"
        postponed["status"]["type"] = {"completed": False, "description": "Postponed"}
        rows = parse_espn_scoreboard({"events": [event, exhibition, postponed]}, "nhl", [2021])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["season"], 2021)
        self.assertEqual(rows[0]["home_score"], 3)
        self.assertEqual(rows[0]["home_regulation_score"], 2)
        self.assertIn("shootout", rows[0]["score_semantics"])
        self.assertIsNone(rows[1]["available_at"])

    def test_normalized_files_reject_conflicting_game_scores(self):
        game = {**game_fixture(), "season": 2024, "home_team_id": "1", "away_team_id": "2", "home_score": 3, "away_score": 1}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nfl_2024.jsonl"
            write_normalized_games([game, game], path)
            self.assertEqual(len(read_normalized_games(path)), 1)
            with self.assertRaises(ValueError):
                write_normalized_games([game, {**game, "home_score": 4}], path)

    def test_download_cache_preserves_revisions_and_redacts_authentication(self):
        calls = []

        def transport(url, headers, timeout, max_bytes):
            calls.append(url)
            return ("revision" + str(len(calls))).encode(), {"ETag": str(len(calls))}

        with tempfile.TemporaryDirectory() as directory:
            downloader = CachedDownloader(directory, transport=transport)
            url = "https://example.com/history?apiKey=private-value&date=2024"
            first = downloader.fetch(url)
            second = downloader.fetch(url)
            third = downloader.fetch(url, refresh=True)
            self.assertEqual(len(calls), 2)
            self.assertTrue(second.cache_hit)
            self.assertEqual(first.read_bytes(), b"revision1")
            self.assertEqual(third.read_bytes(), b"revision2")
            self.assertNotEqual(first.path, third.path)
            for path in Path(directory).rglob("*.json"):
                self.assertNotIn("private-value", path.read_text())

    def test_manifest_deduplicates_league_snapshots_and_excludes_unsettled_games(self):
        game = game_fixture()
        other = {**game, "game_id": "nfl:other"}
        postponed = {**game, "game_id": "nfl:postponed", "completed": False}
        nhl = {**game, "game_id": "nhl:test", "league": "nhl"}
        plan = plan_historical_requests([game, other, postponed, nhl])
        self.assertEqual(plan["request_count"], 4)
        self.assertEqual(plan["estimated_credits"], 120)
        self.assertEqual(len(plan["excluded_games"]), 1)
        self.assertEqual(sum(len(request["targets"]) for request in plan["requests"]), 6)
        self.assertFalse(plan["paid_fetch_performed"])

    def test_normalization_rejects_future_snapshots_and_future_book_updates(self):
        game = game_fixture()
        with self.assertRaises(ValueError):
            normalize_historical_quotes(odds_fixture("2024-11-03T18:05:00Z"), [game], "2024-11-03T18:00:00Z")
        payload = odds_fixture()
        payload["data"][0]["bookmakers"][0]["last_update"] = "2024-11-03T18:01:00Z"
        diagnostics = []
        self.assertEqual(normalize_historical_quotes(payload, [game], "2024-11-03T18:00:00Z", diagnostics=diagnostics), [])
        self.assertEqual(diagnostics[0]["reason"], "missing_or_future_quote_timestamp")

    def test_wrong_game_identity_or_missing_price_never_becomes_a_quote(self):
        game = game_fixture()
        payload = odds_fixture()
        payload["data"][0]["home_team"] = "Different Team"
        self.assertEqual(normalize_historical_quotes(payload, [game], "2024-11-03T18:00:00Z"), [])
        payload = odds_fixture()
        del payload["data"][0]["bookmakers"][0]["markets"][0]["outcomes"][0]["price"]
        quotes = normalize_historical_quotes(payload, [game], "2024-11-03T18:00:00Z")
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0]["side"], "away")

    def test_exact_contract_does_not_recover_a_stale_line_after_book_moves(self):
        base = {"game_id": "nfl:test", "market": "spread", "side": "home", "line": 3.5, "odds": -110,
                "book_key": "book_a", "snapshot_time": "2024-11-03T17:50:00Z", "quote_time": "2024-11-03T17:49:00Z", "is_closing": False}
        moved = {**base, "line": 2.5, "snapshot_time": "2024-11-03T17:55:00Z", "quote_time": "2024-11-03T17:54:00Z"}
        other_book = {**base, "book_key": "book_b", "snapshot_time": moved["snapshot_time"], "quote_time": moved["quote_time"]}
        arguments = {"game_id": "nfl:test", "market": "spread", "side": "home", "line": 3.5,
                     "book_key": "book_a", "decision_time": "2024-11-03T18:00:00Z"}
        self.assertIsNone(exact_contract_quote([base, moved, other_book], **arguments))
        self.assertEqual(exact_contract_quote([base, moved, other_book], **{**arguments, "line": 2.5})["odds"], -110)
        self.assertIsNone(exact_contract_quote([moved], **{**arguments, "line": 2.5, "max_age_seconds": 60}))

    def test_paid_fetch_requires_opt_in_reserves_budget_and_normalizes_offline(self):
        plan = plan_historical_requests([game_fixture()])
        calls = []

        def transport(url, headers, timeout, max_bytes):
            calls.append(url)
            date = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["date"][0]
            return json.dumps(odds_fixture(date)).encode(), {}

        with tempfile.TemporaryDirectory() as directory:
            downloader = CachedDownloader(Path(directory) / "odds_cache", retries=0, transport=transport)
            with self.assertRaises(ValueError):
                fetch_historical_requests(plan, directory, max_credits=30, api_key="test-secret", downloader=downloader)
            self.assertEqual(calls, [])
            ledger = fetch_historical_requests(plan, directory, max_credits=30, allow_paid=True,
                                               api_key="test-secret", downloader=downloader)
            self.assertEqual(len(calls), 1)
            self.assertEqual(ledger["reserved_credits"], 30)
            self.assertEqual(ledger["remaining_planned_requests"], 1)
            repeated = fetch_historical_requests(plan, directory, max_credits=30, allow_paid=True,
                                                 api_key="test-secret", downloader=downloader)
            self.assertEqual(len(calls), 1)
            report = normalize_fetched_manifest(plan, [game_fixture()], directory)
            self.assertEqual(report["entry_quote_rows"], 2)
            self.assertEqual(report["closing_quote_rows"], 0)
            self.assertEqual(report["games_with_entry_quotes"], 1)
            self.assertEqual(report["network_requests_made"], 0)
            for path in Path(directory).rglob("*.json"):
                self.assertNotIn("test-secret", path.read_text())
            changed = copy.deepcopy(plan)
            changed["requests"][0]["url"] = "https://example.com/steal"
            with self.assertRaises(ValueError):
                fetch_historical_requests(changed, directory, max_credits=60, allow_paid=True,
                                           api_key="test-secret", downloader=downloader)

    def test_paid_fetch_lock_blocks_a_second_process_before_spending(self):
        plan = plan_historical_requests([game_fixture()])
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / (".odds_fetch_" + plan["manifest_id"] + ".lock")
            lock.mkdir()
            with self.assertRaises(RuntimeError):
                fetch_historical_requests(plan, directory, max_credits=30, allow_paid=True, api_key="test-secret")
            self.assertFalse((Path(directory) / ("odds_fetch_ledger_" + plan["manifest_id"] + ".json")).exists())

    def test_paid_fetch_halts_on_http_error_and_cannot_resume_spending(self):
        plan = plan_historical_requests([game_fixture()])
        for status in (401, 402, 403, 429, 500):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                calls = []

                def transport(url, headers, timeout, max_bytes):
                    calls.append(url)
                    raise urllib.error.HTTPError(url, status, "private-test-secret", {}, None)

                downloader = CachedDownloader(Path(directory) / "odds_cache", retries=0, transport=transport)
                ledger = fetch_historical_requests(plan, directory, max_credits=60, allow_paid=True,
                                                   api_key="private-test-secret", downloader=downloader)
                self.assertEqual(len(calls), 1)
                self.assertEqual(ledger["reserved_credits"], 30)
                self.assertEqual(ledger["remaining_planned_requests"], 1)
                self.assertEqual(ledger["halted"]["http_status"], status)
                self.assertEqual(len(ledger["results"]), 0)
                self.assertEqual(next(iter(ledger["attempts"].values()))["status"], "failed")
                repeated = fetch_historical_requests(plan, directory, max_credits=60, allow_paid=True,
                                                     api_key="private-test-secret", downloader=downloader)
                self.assertEqual(len(calls), 1)
                self.assertEqual(repeated["halted"], ledger["halted"])
                self.assertEqual(repeated["reserved_credits"], 30)
                for path in Path(directory).rglob("*.json"):
                    self.assertNotIn("private-test-secret", path.read_text())

    def test_paid_fetch_halts_on_network_or_invalid_response_without_exposing_errors(self):
        plan = plan_historical_requests([game_fixture()])
        for failure in ("network", "schema", "non_object", "unknown", "unexpected"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                calls = []

                def transport(url, headers, timeout, max_bytes):
                    calls.append(url)
                    if failure == "network":
                        raise urllib.error.URLError("private-test-secret")
                    if failure == "unknown":
                        raise RuntimeError("private-test-secret " + url)
                    if failure == "unexpected":
                        raise TypeError("private-test-secret " + url)
                    if failure == "non_object":
                        return b"[]", {}
                    return json.dumps({"timestamp": "invalid", "data": []}).encode(), {}

                downloader = CachedDownloader(Path(directory) / "odds_cache", retries=0, transport=transport)
                ledger = fetch_historical_requests(plan, directory, max_credits=60, allow_paid=True,
                                                   api_key="private-test-secret", downloader=downloader)
                self.assertEqual(len(calls), 1)
                self.assertEqual(ledger["reserved_credits"], 30)
                self.assertEqual(ledger["remaining_planned_requests"], 1)
                self.assertTrue(ledger["halted"])
                self.assertNotIn("http_status", ledger["halted"])
                self.assertNotIn("private-test-secret", json.dumps(ledger))

    def test_download_transport_errors_are_typed_and_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            def transport(url, headers, timeout, max_bytes):
                raise urllib.error.HTTPError(url, 403, "private-test-secret", {}, None)

            downloader = CachedDownloader(directory, retries=0, transport=transport)
            with self.assertRaises(DownloadRequestError) as caught:
                downloader.fetch("https://example.com/history?apiKey=private-test-secret")
            self.assertIsInstance(caught.exception, RuntimeError)
            self.assertEqual(caught.exception.http_status, 403)
            self.assertEqual(str(caught.exception), "Historical source request failed (HTTP 403)")

    def test_legacy_failed_ledger_halts_before_any_further_spending(self):
        plan = plan_historical_requests([game_fixture()])
        with tempfile.TemporaryDirectory() as directory:
            request = plan["requests"][0]
            ledger_path = Path(directory) / ("odds_fetch_ledger_" + plan["manifest_id"] + ".json")
            ledger_path.write_text(json.dumps({
                "manifest_id": plan["manifest_id"], "reserved_credits": 30, "results": [],
                "attempts": {request["request_id"]: {"status": "failed", "reserved_credits": 30}},
            }))
            calls = []

            def transport(url, headers, timeout, max_bytes):
                calls.append(url)
                return json.dumps(odds_fixture()).encode(), {}

            downloader = CachedDownloader(Path(directory) / "odds_cache", retries=0, transport=transport)
            ledger = fetch_historical_requests(plan, directory, max_credits=60, allow_paid=True,
                                               api_key="test-secret", downloader=downloader)
            self.assertEqual(calls, [])
            self.assertEqual(ledger["reserved_credits"], 30)
            self.assertEqual(ledger["halted"]["reason"], "prior_failed_request_requires_review")
            self.assertEqual(json.loads(ledger_path.read_text())["halted"], ledger["halted"])

    def test_paid_fetch_cli_exits_nonzero_when_halted(self):
        from research.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            manifest_path.write_text(json.dumps(plan_historical_requests([game_fixture()])))
            report = {"halted": {"reason": "authentication_or_historical_entitlement", "http_status": 403}}
            stdout = io.StringIO()
            with patch("research.__main__.fetch_historical_requests", return_value=report), redirect_stdout(stdout):
                with self.assertRaises(SystemExit) as caught:
                    main(["odds-fetch", "--manifest", str(manifest_path), "--output", directory,
                          "--max-credits", "60", "--allow-paid"])
            self.assertTrue(caught.exception.code)
            self.assertEqual(json.loads(stdout.getvalue()), report)


if __name__ == "__main__":
    unittest.main()
