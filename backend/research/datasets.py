"""Historical game adapters for offline, reconstructed pregame experiments.

No live injuries, current team aggregates, or current odds are loaded here.
Result availability uses an explicit conservative convention, not the download
time. Corrected historical source files are never described as exact feed replay.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .downloads import CachedDownloader, utc_now

NFL_SCHEDULE_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
CFB_SCHEDULE_URL = "https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main/schedules/csv/cfb_schedules_{season}.csv"
NHL_ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"
CFB_ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
NFL_PBP_URL = "https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
RECONSTRUCTION_BASIS = "reconstructed_conservative_12h_after_start"
# ESPN also puts All-Star and international exhibitions in its NHL scoreboard.
# These are observed franchise IDs, not a filter inferred from game outcomes.
NHL_ESPN_CLUB_IDS = {str(value) for value in range(1, 31)} | {"37", "124292", "129764"}

NFL_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LAC": "Los Angeles Chargers", "LAR": "Los Angeles Rams",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WSH": "Washington Commanders",
}
NFL_ABBREVIATIONS = {"LA": "LAR", "WAS": "WSH", "JAC": "JAX", "OAK": "LV", "SD": "LAC", "STL": "LAR"}


def parse_time(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _bool(value):
    return value is True or str(value).strip().lower() in ("true", "1", "yes")


def _integer(value):
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _provenance(game, provenance):
    game.update(provenance or {})
    start = parse_time(game["game_time"])
    game["available_at"] = (start + timedelta(hours=12)).isoformat() if game["completed"] else None
    game["availability_basis"] = RECONSTRUCTION_BASIS
    game["historical_evidence"] = "reconstructed_prior_game_statistics"
    game["game_time_quality"] = "scheduled_start"
    return game


def parse_nflverse_schedule(content: bytes | str, seasons=None, provenance=None):
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    reader = csv.DictReader(io.StringIO(text))
    required = {"game_id", "season", "game_type", "gameday", "gametime", "home_team", "away_team", "home_score", "away_score"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("nflverse schedule schema is missing required columns")
    selected = set(map(int, seasons)) if seasons is not None else None
    games = []
    for row in reader:
        season = _integer(row.get("season"))
        if season is None or (selected is not None and season not in selected) or row["game_type"] not in ("REG", "WC", "DIV", "CON", "SB"):
            continue
        if not row.get("gameday") or not row.get("gametime"):
            raise ValueError(f"NFL schedule lacks a kickoff time for {row['game_id']}")
        start = datetime.fromisoformat(row["gameday"] + "T" + row["gametime"]).replace(tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
        home = NFL_ABBREVIATIONS.get(row["home_team"], row["home_team"])
        away = NFL_ABBREVIATIONS.get(row["away_team"], row["away_team"])
        home_score, away_score = _integer(row["home_score"]), _integer(row["away_score"])
        game = {"league": "nfl", "game_id": "nfl:" + row["game_id"], "source_game_id": row["game_id"],
                "season": season, "season_type": "regular" if row["game_type"] == "REG" else "postseason",
                "week": _integer(row.get("week")), "game_time": start.isoformat(),
                "home_team_id": "nfl:" + home, "away_team_id": "nfl:" + away,
                "home_team": NFL_NAMES.get(home, home), "away_team": NFL_NAMES.get(away, away),
                "home_abbreviation": home, "away_abbreviation": away,
                "home_score": home_score, "away_score": away_score,
                "completed": home_score is not None and away_score is not None,
                "neutral_site": row.get("location", "").lower() == "neutral",
                "overtime": _bool(row.get("overtime")), "score_semantics": "final_score_including_overtime",
                "provider_ids": {"nflverse": row["game_id"], "espn": row.get("espn") or None},
                "closing_home_spread": -_number(row["spread_line"]) if _number(row.get("spread_line")) is not None else None,
                "closing_total": _number(row.get("total_line")),
                "closing_home_moneyline": _number(row.get("home_moneyline")),
                "closing_away_moneyline": _number(row.get("away_moneyline")),
                "closing_home_spread_odds": _number(row.get("home_spread_odds")),
                "closing_away_spread_odds": _number(row.get("away_spread_odds")),
                "closing_over_odds": _number(row.get("over_odds")),
                "closing_under_odds": _number(row.get("under_odds")),
                "closing_quote_time": None,
                "closing_data_use": "baseline_only_no_historical_quote_timestamp"}
        for side, team in (("home", home), ("away", away)):
            if team == "WSH" and season in (2020, 2021):
                game[side + "_team"] = "Washington Football Team"
        games.append(_provenance(game, provenance))
    return sorted(games, key=lambda game: (game["game_time"], game["game_id"]))


def parse_cfb_schedule(content: bytes | str, seasons=None, provenance=None):
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    reader = csv.DictReader(io.StringIO(text))
    required = {"game_id", "season", "start_date", "home_id", "away_id", "home_points", "away_points"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("CFB schedule schema is missing required columns")
    selected = set(map(int, seasons)) if seasons is not None else None
    games = []
    for row in reader:
        season = _integer(row.get("season"))
        if season is None or (selected is not None and season not in selected):
            continue
        # Keep FBS games, including FBS-FCS opponents needed for ratings. Do not
        # silently treat every lower-division archived game as a priced FBS game.
        divisions = {row.get("home_division", "").lower(), row.get("away_division", "").lower()}
        if "home_division" in row and "fbs" not in divisions:
            continue
        start = parse_time(row.get("start_date"))
        if start is None or not row.get("home_id") or not row.get("away_id"):
            raise ValueError(f"CFB game has missing identity or kickoff: {row.get('game_id')}")
        home_score, away_score = _integer(row.get("home_points")), _integer(row.get("away_points"))
        completed = (_bool(row["completed"]) if "completed" in row else home_score is not None and away_score is not None)
        completed = completed and home_score is not None and away_score is not None
        game = {"league": "cfb", "game_id": "cfb:" + row["game_id"], "source_game_id": row["game_id"],
                "season": season, "season_type": row.get("season_type", "regular"), "week": _integer(row.get("week")),
                "game_time": start.isoformat(), "home_team_id": "cfb:" + row["home_id"], "away_team_id": "cfb:" + row["away_id"],
                "home_team": row.get("home_team", row["home_id"]), "away_team": row.get("away_team", row["away_id"]),
                "home_score": home_score, "away_score": away_score, "completed": completed,
                "neutral_site": _bool(row.get("neutral_site")), "home_division": row.get("home_division"),
                "away_division": row.get("away_division"), "home_conference": row.get("home_conference"),
                "away_conference": row.get("away_conference"), "venue": row.get("venue"),
                "score_semantics": "final_score_including_overtime", "start_time_tbd": _bool(row.get("start_time_tbd"))}
        # Published season-end Elo, postgame win probability and excitement
        # fields are deliberately excluded. Replay learns only from earlier games.
        games.append(_provenance(game, provenance))
    return sorted(games, key=lambda game: (game["game_time"], game["game_id"]))


def parse_espn_scoreboard(payload: dict, league: str, seasons=None, provenance=None):
    if league not in ("nfl", "cfb", "nhl"):
        raise ValueError("Unsupported research league")
    if not isinstance(payload.get("events"), list):
        raise ValueError("ESPN historical scoreboard lacks events")
    selected = set(map(int, seasons)) if seasons is not None else None
    games = []
    for event in payload["events"]:
        event_season = event.get("season", {})
        year = _integer(event_season.get("year"))
        if year is None or _integer(event_season.get("type")) == 1:
            continue
        season = year - 1 if league == "nhl" else year
        if selected is not None and season not in selected:
            continue
        competition = (event.get("competitions") or [{}])[0]
        participants = competition.get("competitors", [])
        sides = {item.get("homeAway"): item for item in participants}
        if "home" not in sides or "away" not in sides:
            raise ValueError("ESPN historical game lacks explicit home/away identities")
        if league == "nhl" and any(str(sides[side].get("team", {}).get("id", sides[side].get("id", ""))) not in NHL_ESPN_CLUB_IDS for side in ("home", "away")):
            continue
        start = parse_time(competition.get("date", event.get("date")))
        if start is None:
            raise ValueError("ESPN historical game lacks a timezone-qualified start")
        status = event.get("status", competition.get("status", {})).get("type", {})
        identity_prefix = league + (":espn:" if league == "nhl" else ":")
        game = {"league": league, "game_id": identity_prefix + str(event["id"]),
                "source_game_id": str(event["id"]), "season": season,
                "season_type": "postseason" if _integer(event_season.get("type")) == 3 else "regular",
                "week": event.get("week", {}).get("number"), "game_time": start.isoformat(),
                "completed": bool(status.get("completed", False)), "neutral_site": bool(competition.get("neutralSite", False)),
                "end_type": status.get("description", ""), "provider_ids": {"espn": str(event["id"])},
                "score_semantics": "final_score_including_overtime_and_shootout" if league == "nhl" else "final_score_including_overtime"}
        for side, item in ((side, sides[side]) for side in ("home", "away")):
            team = item.get("team", {})
            identity = str(team.get("id", item.get("id", "")))
            if not identity:
                raise ValueError("ESPN historical game lacks a team ID")
            game[side + "_team_id"] = identity_prefix + identity
            game[side + "_team"] = team.get("displayName", identity)
            game[side + "_abbreviation"] = team.get("abbreviation")
            score = item.get("score")
            if isinstance(score, dict):
                score = score.get("value", score.get("displayValue"))
            game[side + "_score"] = _integer(score)
            if league == "nhl":
                periods = item.get("linescores") or []
                regulation = [period.get("value") for period in periods[:3]]
                if len(regulation) == 3 and all(_number(value) is not None for value in regulation):
                    game[side + "_regulation_score"] = int(sum(map(float, regulation)))
        if game["completed"] and (game["home_score"] is None or game["away_score"] is None):
            raise ValueError("Completed historical game has no final score")
        games.append(_provenance(game, provenance))
    return games


def parse_nhl_club_schedule(payload: dict, seasons=None, provenance=None):
    """Adapter for official NHL /v1/club-schedule-season responses.

    This official-host endpoint has no supported public API contract. Its adapter
    is optional; the backfill uses the accessible ESPN archive by default.
    """
    selected = set(map(int, seasons)) if seasons is not None else None
    games = []
    for row in payload.get("games", []):
        season = int(str(row.get("season", ""))[:4])
        if (selected is not None and season not in selected) or row.get("gameType") not in (2, 3):
            continue
        start = parse_time(row.get("startTimeUTC"))
        if start is None:
            raise ValueError("NHL schedule lacks startTimeUTC")
        game = {"league": "nhl", "game_id": "nhl:" + str(row["id"]), "source_game_id": str(row["id"]),
                "season": season, "season_type": "regular" if row["gameType"] == 2 else "postseason",
                "game_time": start.isoformat(), "completed": row.get("gameState") in ("OFF", "FINAL"),
                "neutral_site": bool(row.get("neutralSite", False)),
                "score_semantics": "final_score_including_overtime_and_shootout",
                "provider_ids": {"nhl": str(row["id"])}, "end_type": row.get("gameOutcome", {}).get("lastPeriodType")}
        for side in ("home", "away"):
            team = row[side + "Team"]
            game[side + "_team_id"] = "nhl:" + str(team["id"])
            place, common = team.get("placeName", {}), team.get("commonName", {})
            game[side + "_team"] = " ".join(value for value in (place.get("default"), common.get("default")) if value) or team.get("abbrev", str(team["id"]))
            game[side + "_abbreviation"] = team.get("abbrev")
            game[side + "_score"] = _integer(team.get("score"))
        games.append(_provenance(game, provenance))
    return games


def _deduplicate(games):
    unique = {}
    identity = ("league", "season", "game_time", "home_team_id", "away_team_id", "home_score", "away_score", "completed")
    for game in games:
        previous = unique.get(game["game_id"])
        if previous is not None and any(previous.get(key) != game.get(key) for key in identity):
            if previous["completed"] != game["completed"]:
                unique[game["game_id"]] = game if game["completed"] else previous
                continue
            raise ValueError(f"Conflicting historical records for {game['game_id']}")
        unique[game["game_id"]] = game
    return sorted(unique.values(), key=lambda game: (game["game_time"], game["game_id"]))


def write_normalized_games(games, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = _deduplicate(games)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    if path.exists():
        previous = path.read_bytes()
        if previous != temporary.read_bytes():
            versions = path.parent.parent / "normalized_versions"
            versions.mkdir(parents=True, exist_ok=True)
            revision = versions / (path.stem + "_" + hashlib.sha256(previous).hexdigest() + path.suffix)
            if not revision.exists():
                revision.write_bytes(previous)
    temporary.replace(path)
    return path


def read_normalized_games(path, league=None, seasons=None):
    path = Path(path)
    if path.is_dir():
        directory = path / "normalized" if (path / "normalized").is_dir() else path
        files = sorted(directory.glob("*.jsonl"))
    else:
        files = [path]
    selected = set(map(int, seasons)) if seasons is not None else None
    games = []
    for filename in files:
        with filename.open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                game = json.loads(line)
                if league is not None and game.get("league") != league:
                    continue
                if selected is not None and game.get("season") not in selected:
                    continue
                games.append(game)
    return _deduplicate(games)


def _nhl_months(season):
    return [(season, month) for month in (9, 10, 11, 12)] + [(season + 1, month) for month in range(1, 8)]


def backfill_schedules(league, seasons, output_dir, *, refresh=False, workers=4, downloader=None):
    """Download public schedules only; never calls a paid/keyed source.

    Seasons use their starting year, including NHL (2021 = 2021-22).
    Returns a coverage manifest and writes one normalized JSONL per season.
    A failed/incomplete season is reported and is not silently presented as full.
    """
    if league not in ("nfl", "cfb", "nhl"):
        raise ValueError("league must be nfl, cfb, or nhl")
    seasons = sorted(set(map(int, seasons)))
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    downloader = downloader or CachedDownloader(destination / "cache")
    report = {"league": league, "seasons": [], "generated_at": utc_now(),
              "evidence_type": "reconstructed_prior_game_statistics",
              "availability_basis": RECONSTRUCTION_BASIS, "errors": []}
    if league == "nfl":
        resource = downloader.fetch(NFL_SCHEDULE_URL, refresh=refresh)
        all_games = parse_nflverse_schedule(resource.read_bytes(), seasons, resource.provenance())
        tasks = []
    elif league == "cfb":
        tasks = [(season, CFB_SCHEDULE_URL.format(season=season)) for season in seasons]
        all_games = []
    else:
        tasks = [(season, NHL_ESPN_URL + "?" + urllib.parse.urlencode({"dates": f"{year}{month:02d}", "limit": 1000}))
                 for season in seasons for year, month in _nhl_months(season)]
        all_games = []

    def load(task):
        season, url = task
        resource = downloader.fetch(url, refresh=refresh)
        if league == "cfb":
            rows = parse_cfb_schedule(resource.read_bytes(), [season], resource.provenance())
        else:
            payload = json.loads(resource.read_bytes())
            if len(payload.get("events", [])) >= 1000:
                raise ValueError("Historical scoreboard reached the result cap")
            rows = parse_espn_scoreboard(payload, league, [season], resource.provenance())
        return season, rows

    if tasks:
        with ThreadPoolExecutor(max_workers=min(max(int(workers), 1), 6)) as executor:
            pending = {executor.submit(load, task): task for task in tasks}
            for future in as_completed(pending):
                task = pending[future]
                try:
                    _, rows = future.result()
                    all_games.extend(rows)
                except Exception as error:
                    report["errors"].append({"season": task[0], "source_url": task[1],
                                             "error": str(error)[:240]})
    if league == "cfb":
        # Some archived CSVs end at Army-Navy. Add only missing FBS postseason
        # games from the provider's December/January archive, preserving originals.
        for season in seasons:
            existing = [game for game in all_games if game["season"] == season]
            if any(game.get("season_type") == "postseason" for game in existing):
                continue
            fbs_ids, team_meta = set(), {}
            for game in existing:
                for side in ("home", "away"):
                    identity = game[side + "_team_id"]
                    if game.get(side + "_division") == "fbs":
                        fbs_ids.add(identity)
                    team_meta[identity] = {"name": game[side + "_team"], "division": game.get(side + "_division"),
                                           "conference": game.get(side + "_conference")}
            for year, month in ((season, 12), (season + 1, 1)):
                url = CFB_ESPN_URL + "?" + urllib.parse.urlencode({"dates": f"{year}{month:02d}", "limit": 1000, "groups": 80})
                try:
                    resource = downloader.fetch(url, refresh=refresh)
                    payload = json.loads(resource.read_bytes())
                    if len(payload.get("events", [])) >= 1000:
                        raise ValueError("Historical CFB supplement reached the result cap")
                    supplements = parse_espn_scoreboard(payload, "cfb", [season], resource.provenance())
                    known = {game["game_id"] for game in all_games}
                    for game in supplements:
                        if game["game_id"] in known or game["season_type"] != "postseason" or not all(game[side + "_team_id"] in fbs_ids for side in ("home", "away")):
                            continue
                        for side in ("home", "away"):
                            meta = team_meta[game[side + "_team_id"]]
                            game[side + "_team_aliases"] = [game[side + "_team"]]
                            game[side + "_team"] = meta["name"]
                            game[side + "_division"] = meta["division"]
                            game[side + "_conference"] = meta["conference"]
                        all_games.append(game)
                        known.add(game["game_id"])
                except Exception as error:
                    report["errors"].append({"season": season, "source_url": url, "error": str(error)[:240]})
    all_games = _deduplicate(all_games)
    for season in seasons:
        rows = [game for game in all_games if game["season"] == season]
        complete = [game for game in rows if game["completed"]]
        path = destination / "normalized" / f"{league}_{season}.jsonl"
        write_normalized_games(rows, path)
        errors = [error for error in report["errors"] if error["season"] == season]
        report["seasons"].append({"season": season, "games": len(rows), "completed_games": len(complete),
                                  "regular_completed": sum(game["season_type"] == "regular" for game in complete),
                                  "postseason_completed": sum(game["season_type"] == "postseason" for game in complete),
                                  "first_game": rows[0]["game_time"] if rows else None,
                                  "last_game": rows[-1]["game_time"] if rows else None,
                                  "output": str(path), "source_errors": len(errors),
                                  "coverage_status": "partial" if errors or not rows else "source_rows_loaded_not_independently_certified"})
    (destination / f"coverage_{league}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def attach_nfl_play_metrics(games, output_dir, *, seasons=None, refresh=False, downloader=None):
    """Optional larger free downloads; persist enriched seasons after each pull."""
    from data.football import _aggregate_nflverse
    downloader = downloader or CachedDownloader(Path(output_dir) / "cache", timeout=60)
    selected = sorted(set(seasons or [game["season"] for game in games if game["league"] == "nfl"]))
    by_id = {game["source_game_id"]: game for game in games if game["league"] == "nfl"}
    matched = 0
    outputs, raw_bytes = [], 0
    for season in selected:
        resource = downloader.fetch(NFL_PBP_URL.format(season=season), refresh=refresh)
        raw_bytes += resource.size_bytes
        for row in _aggregate_nflverse(resource.read_bytes()):
            game = by_id.get(row["game_id"])
            if game is None:
                continue
            team = NFL_ABBREVIATIONS.get(row["team"], row["team"])
            side = "home" if game["home_abbreviation"] == team else "away" if game["away_abbreviation"] == team else None
            if side is None:
                continue
            # Closing prices, actual starter names and outcome IDs are labels or
            # audit data, not features for the same game's pregame prediction.
            allowed = {"plays", "total_plays", "dropbacks", "rush_plays", "epa_per_play", "pass_epa", "rush_epa",
                       "success_rate", "explosive_rate", "sack_rate", "defensive_plays", "defensive_dropbacks",
                       "defensive_total_plays", "defensive_epa_per_play", "defensive_pass_epa", "defensive_rush_epa",
                       "defensive_success_rate", "defensive_explosive_rate", "pressure_sack_rate"}
            game[side + "_metrics"] = {key: row.get(key) for key in allowed}
            game["metrics_provenance"] = {**resource.provenance(), "availability_basis": RECONSTRUCTION_BASIS}
            game["metrics_available_at"] = game.get("available_at")
            matched += 1
        path = Path(output_dir) / "normalized" / f"nfl_{season}.jsonl"
        rows = [game for game in games if game.get("league") == "nfl" and game.get("season") == season]
        write_normalized_games(rows, path)
        outputs.append(str(path))
    return {"team_games_with_metrics": matched, "seasons": selected, "outputs": outputs,
            "raw_size_bytes": raw_bytes, "availability_basis": RECONSTRUCTION_BASIS}
