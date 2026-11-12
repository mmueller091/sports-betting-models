"""Public weekly football data with explicit coverage and no fabricated statistics.

ESPN's public site feeds provide schedules, scores, season team statistics and
injury reports. nflverse provides NFL play-level EPA and success data. Current
season team summaries are context only; historical per-game NFL features are
attached separately so a model can compute strictly as-of rolling predictors.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import io
import json
import math
import re
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from data.odds import ODDS_STATUS, get_game_odds
from data.storage import STORE_DIR

ESPN = "https://site.api.espn.com/apis/site/v2/sports/football"
EASTERN = ZoneInfo("America/New_York")
CACHE_DIR = STORE_DIR / "football_cache"
LEAGUES = {
    "nfl": {"slug": "nfl", "sport": "americanfootball_nfl", "label": "NFL"},
    "cfb": {"slug": "college-football", "sport": "americanfootball_ncaaf", "label": "CFB"},
}
NFLVERSE_ABBREVIATIONS = {"LA": "LAR", "WAS": "WSH", "JAC": "JAX", "OAK": "LV", "SD": "LAC", "STL": "LAR"}


def _number(value):
    try:
        if value is None or value == "":
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _ratio(numerator, denominator, factor=1.0):
    numerator, denominator = _number(numerator), _number(denominator)
    return round(numerator / denominator * factor, 6) if numerator is not None and denominator and denominator > 0 else None


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _cache_path(key):
    return CACHE_DIR / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")


def _read_cache(key):
    try:
        value = json.loads(_cache_path(key).read_text(encoding="utf-8"))
        if not isinstance(value, dict) or "data" not in value:
            return None
        return value
    except (OSError, ValueError):
        return None


def _write_cache(key, data):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(key)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"timestamp": time.time(), "data": data}, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


async def _fetch_json(client, url, params=None, ttl=3600, allow_stale=False):
    key = url + "?" + json.dumps(params or {}, sort_keys=True)
    cached = _read_cache(key)
    if cached and time.time() - cached["timestamp"] < ttl:
        return cached["data"], False
    try:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected football feed schema")
        _write_cache(key, data)
        return data, False
    except Exception:
        if allow_stale and cached and time.time() - cached["timestamp"] < 7 * 86400:
            return cached["data"], True
        # A source URL may contain credentials in callers. Only safe identifiers
        # reach published source health; raw exception messages never leave here.
        raise RuntimeError("Football source temporarily unavailable") from None


def _select_week(board, target_date):
    """Use the league calendar (CFB Mon-Sun; NFL Wed-Tue), not ISO week numbers."""
    at = datetime.combine(target_date, day_time(12), EASTERN).astimezone(timezone.utc)
    calendars = board.get("leagues", [{}])[0].get("calendar", [])
    if not isinstance(calendars, list) or not calendars or not any(isinstance(stage, dict) and "entries" in stage for stage in calendars):
        raise ValueError("Football league calendar is missing")
    candidates = []
    for stage in calendars:
        if str(stage.get("value")) not in ("2", "3"):
            continue
        for entry in stage.get("entries", []):
            start, end = _parse_time(entry.get("startDate")), _parse_time(entry.get("endDate"))
            if start is None or end is None:
                continue
            item = {"number": int(entry["value"]), "season_type": int(stage["value"]),
                    "label": entry.get("label", "Week " + str(entry["value"])),
                    "start": start, "end": end}
            candidates.append(item)
            if start <= at <= end:
                return item, candidates, calendars
    # Do not promote distant next-season games during the offseason.
    return None, candidates, calendars


def _team_from_competitor(competitor):
    team = competitor.get("team", {})
    team_id = str(team.get("id", competitor.get("id", "")))
    return team_id, {"name": team.get("displayName", "Unknown team"),
                     "abbreviation": team.get("abbreviation", ""),
                     "aliases": [team.get("displayName", ""), team.get("shortDisplayName", ""), team.get("location", "")],
                     "games_played": 0, "metrics": {}, "injuries": [], "source_coverage": []}


def _event_parts(event):
    competitions = event.get("competitions", [])
    if not competitions:
        return None
    competition = competitions[0]
    competitors = competition.get("competitors", [])
    home = next((c for c in competitors if c.get("homeAway") == "home"), None)
    away = next((c for c in competitors if c.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    home_id, home_team = _team_from_competitor(home)
    away_id, away_team = _team_from_competitor(away)
    kickoff = _parse_time(competition.get("date", event.get("date")))
    if not kickoff or not home_id or not away_id:
        return None
    status = event.get("status", competition.get("status", {})).get("type", {})
    return competition, home, away, home_id, away_id, home_team, away_team, kickoff, status


def _parse_event(event):
    parts = _event_parts(event)
    if parts is None:
        return None, {}
    competition, home, away, home_id, away_id, home_team, away_team, kickoff, status = parts
    venue = competition.get("venue", {})
    game = {
        "game_id": str(event.get("id", competition.get("id"))),
        "game_time": kickoff.isoformat(),
        "home_team_id": home_id, "away_team_id": away_id,
        "home_team": home_team["name"], "away_team": away_team["name"],
        "neutral_site": bool(competition.get("neutralSite", False)),
        "venue": {"name": venue.get("fullName", ""), "indoor": venue.get("indoor"),
                  "city": venue.get("address", {}).get("city"), "state": venue.get("address", {}).get("state")},
        "status": status.get("state", "unknown"), "completed": bool(status.get("completed", False)),
        "season": event.get("season", {}).get("year"),
        "season_type": event.get("season", {}).get("type"),
        "week": event.get("week", {}).get("number"),
        "odds": {}, "rest": {},
    }
    for side, competitor in (("home", home), ("away", away)):
        score = competitor.get("score")
        if isinstance(score, dict):
            score = score.get("value", score.get("displayValue"))
        game[side + "_score"] = _number(score)
    return game, {home_id: home_team, away_id: away_team}


def _categories(value):
    if isinstance(value, dict):
        value = value.get("categories", [])
    return {category.get("name"): {stat.get("name"): _number(stat.get("value"))
                                    for stat in category.get("stats", [])}
            for category in (value or [])}


def _parse_team_metrics(data, season, league):
    if int(data.get("season", {}).get("year", -1)) != season:
        raise ValueError("Team statistics are from a different season")
    results = data.get("results", {})
    offense, defense = _categories(results.get("stats")), _categories(results.get("opponent"))
    if not offense:
        raise ValueError("Team statistics are not available")
    passing, rushing = offense.get("passing", {}), offense.get("rushing", {})
    allowed_pass, allowed_rush = defense.get("passing", {}), defense.get("rushing", {})
    misc, allowed_misc = offense.get("miscellaneous", {}), defense.get("miscellaneous", {})
    games = offense.get("general", {}).get("gamesPlayed") or passing.get("teamGamesPlayed")
    plays, allowed_plays = rushing.get("totalOffensivePlays"), allowed_rush.get("totalOffensivePlays")
    pass_attempts, sacks_allowed = passing.get("passingAttempts"), passing.get("sacks")
    opp_attempts, sacks = allowed_pass.get("passingAttempts"), allowed_pass.get("sacks")
    net_yards = passing.get("netPassingYards")
    rush_yards = rushing.get("rushingYards")
    net_allowed = allowed_pass.get("netPassingYards")
    rush_allowed = allowed_rush.get("rushingYards")
    metrics = {
        "yards_per_play": _ratio(net_yards + rush_yards if net_yards is not None and rush_yards is not None else None, plays),
        "yards_per_play_allowed": _ratio(net_allowed + rush_allowed if net_allowed is not None and rush_allowed is not None else None, allowed_plays),
        "pass_yards_per_attempt": passing.get("yardsPerPassAttempt"),
        "pass_yards_per_attempt_allowed": allowed_pass.get("yardsPerPassAttempt"),
        "rush_yards_per_attempt": rushing.get("yardsPerRushAttempt"),
        "rush_yards_per_attempt_allowed": allowed_rush.get("yardsPerRushAttempt"),
        "plays_per_game": _ratio(plays, games),
        "points_per_game": passing.get("totalPointsPerGame"),
        "points_allowed_per_game": allowed_pass.get("totalPointsPerGame"),
        "turnover_margin_per_game": _ratio(misc.get("turnOverDifferential"), games),
        "sack_rate_allowed": _ratio(sacks_allowed, (pass_attempts + sacks_allowed) if pass_attempts is not None and sacks_allowed is not None else None),
        "sack_rate": _ratio(sacks, (opp_attempts + sacks) if opp_attempts is not None and sacks is not None else None),
        "third_down_pct": misc.get("thirdDownConvPct"),
        "third_down_allowed_pct": allowed_misc.get("thirdDownConvPct"),
    }
    # CFB feed publishes zeros for red zone/explosive fields even for strong teams;
    # these unsupported placeholders are intentionally not treated as observations.
    if league == "nfl":
        metrics["red_zone_td_pct"] = misc.get("redzoneTouchdownPct")
        metrics["red_zone_td_allowed_pct"] = allowed_misc.get("redzoneTouchdownPct")
    return int(games or 0), metrics


def _normal_name(name):
    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    text = text.replace("&", "and")
    text = re.sub(r"[^a-z0-9 ]", "", text)
    text = " ".join(text.split())
    replacements = {
        "connecticut huskies": "uconn huskies",
        "southern mississippi golden eagles": "southern miss golden eagles",
        "miami oh redhawks": "miami ohio redhawks",
        "miami redhawks": "miami ohio redhawks",
        "miami fl hurricanes": "miami hurricanes",
        "louisiana lafayette ragin cajuns": "louisiana ragin cajuns",
        "louisiana monroe warhawks": "ul monroe warhawks",
        "san jose state spartans": "san jose state spartans",
        "massachusetts minutemen": "umass minutemen",
        "ut san antonio roadrunners": "utsa roadrunners",
        "central florida knights": "ucf knights",
        "southern methodist mustangs": "smu mustangs",
        "brigham young cougars": "byu cougars",
        "sam houston state bearkats": "sam houston bearkats",
        "appalachian state mountaineers": "app state mountaineers",
    }
    return replacements.get(text, text)


def _attach_odds(games, odds_games, teams, status):
    index = {(_normal_name(o.get("away_team")), _normal_name(o.get("home_team"))): o for o in odds_games}
    for game in games:
        away = teams[game["away_team_id"]]
        home = teams[game["home_team_id"]]
        found = None
        for away_alias in away["aliases"]:
            for home_alias in home["aliases"]:
                candidate = index.get((_normal_name(away_alias), _normal_name(home_alias)))
                if candidate:
                    # Team identity alone is insufficient: match this week's kickoff.
                    kickoff = _parse_time(candidate.get("commence_time"))
                    if kickoff and abs((kickoff - _parse_time(game["game_time"])).total_seconds()) <= 12 * 3600:
                        found = candidate
        if found:
            game["odds"] = {**found, "updated_at": status.get("updated_at"), "stale": bool(status.get("stale"))}


def _parse_injuries(data, season, now):
    injuries = {}
    cutoff = datetime(season, 7, 1, tzinfo=timezone.utc)
    current_count, ignored_count = 0, 0
    for entry in data.get("injuries", []):
        team_id = str(entry.get("id", entry.get("team", {}).get("id", "")))
        team_injuries = []
        for item in entry.get("injuries", []):
            updated = _parse_time(item.get("date"))
            if updated is None or updated < cutoff or updated > now + timedelta(days=1):
                ignored_count += 1
                continue
            athlete = item.get("athlete", {})
            team_injuries.append({"player_id": str(athlete.get("id", "")),
                                  "name": athlete.get("displayName", "Unknown player"),
                                  "position": athlete.get("position", {}).get("abbreviation", ""),
                                  "status": item.get("status", "Unknown"),
                                  "updated_at": updated.isoformat(),
                                  "detail": item.get("shortComment", "")})
            current_count += 1
        if team_id and team_injuries:
            injuries[team_id] = team_injuries
    return injuries, current_count, ignored_count


def _aggregate_nflverse(content):
    """Compact neutral-situation play data into team-game observations.

    Use scrimmage pass/run plays, excluding kneels/spikes and pre-play WP outside
    5%-95%. Explosive = 20+ passing yards or 10+ rushing yards. Sack rate is a sack
    proxy, not pressure rate. Full-game play counts are retained before the WP
    filter, so pace is never mistaken for neutral-situation sample size.
    """
    reader = csv.DictReader(io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(content)), encoding="utf-8"))
    required = {"game_id", "game_date", "home_team", "away_team", "posteam", "defteam", "epa", "play_type"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise ValueError("NFL play data schema unavailable")
    aggregates = {}
    for row in reader:
        if row.get("season_type") not in ("REG", "POST") or row.get("play_type") not in ("run", "pass"):
            continue
        if _number(row.get("qb_kneel")) == 1 or _number(row.get("qb_spike")) == 1:
            continue
        team, opponent = row.get("posteam"), row.get("defteam")
        if not team or not opponent:
            continue
        key = (row["game_id"], team)
        if key not in aggregates:
            aggregates[key] = {"game_id": row["game_id"], "game_date": row["game_date"],
                               "team": team, "opponent": opponent, "home_team": row["home_team"], "away_team": row["away_team"],
                               "closing_home_margin": _number(row.get("spread_line")), "closing_total": _number(row.get("total_line")),
                               "passers": {},
                               "total_plays": 0, "plays": 0, "dropbacks": 0, "rush_plays": 0,
                               "epa_sum": 0.0, "pass_epa_sum": 0.0, "rush_epa_sum": 0.0,
                               "successes": 0, "explosives": 0, "sacks": 0}
        totals = aggregates[key]
        totals["total_plays"] += 1
        passer = row.get("passer_player_name")
        passer_id = row.get("passer_player_id")
        if passer and (_number(row.get("qb_dropback")) == 1 or row.get("play_type") == "pass"):
            identity = (passer, passer_id)
            totals["passers"][identity] = totals["passers"].get(identity, 0) + 1
        wp, epa = _number(row.get("wp")), _number(row.get("epa"))
        if epa is None or (wp is not None and not .05 <= wp <= .95):
            continue
        totals["plays"] += 1
        totals["epa_sum"] += epa
        totals["successes"] += int(epa > 0)
        dropback = _number(row.get("qb_dropback")) == 1 or row.get("play_type") == "pass"
        yards = _number(row.get("yards_gained")) or 0
        totals["explosives"] += int(yards >= (20 if dropback else 10))
        if dropback:
            totals["dropbacks"] += 1
            totals["pass_epa_sum"] += epa
            totals["sacks"] += int(_number(row.get("sack")) == 1)
        else:
            totals["rush_plays"] += 1
            totals["rush_epa_sum"] += epa
    rows = []
    for key, value in aggregates.items():
        opponent = aggregates.get((value["game_id"], value["opponent"]), {})
        primary = max(value["passers"], key=value["passers"].get) if value["passers"] else (None, None)
        rows.append({
            "game_id": value["game_id"], "game_date": value["game_date"],
            "team": value["team"], "opponent": value["opponent"],
            "home_team": value["home_team"], "away_team": value["away_team"],
            "closing_home_margin": value["closing_home_margin"], "closing_total": value["closing_total"],
            "primary_passer": primary[0], "primary_passer_id": primary[1],
            "plays": value["plays"], "total_plays": value["total_plays"],
            "dropbacks": value["dropbacks"], "pass_plays": value["dropbacks"], "rush_plays": value["rush_plays"],
            "epa_per_play": _ratio(value["epa_sum"], value["plays"]),
            "pass_epa": _ratio(value["pass_epa_sum"], value["dropbacks"]),
            "rush_epa": _ratio(value["rush_epa_sum"], value["rush_plays"]),
            "success_rate": _ratio(value["successes"], value["plays"]),
            "explosive_rate": _ratio(value["explosives"], value["plays"]),
            "sack_rate": _ratio(value["sacks"], value["dropbacks"]),
            "defensive_plays": opponent.get("plays"), "defensive_dropbacks": opponent.get("dropbacks"),
            "defensive_total_plays": opponent.get("total_plays"),
            "defensive_epa_per_play": _ratio(opponent.get("epa_sum"), opponent.get("plays")),
            "defensive_pass_epa": _ratio(opponent.get("pass_epa_sum"), opponent.get("dropbacks")),
            "defensive_rush_epa": _ratio(opponent.get("rush_epa_sum"), opponent.get("rush_plays")),
            "defensive_success_rate": _ratio(opponent.get("successes"), opponent.get("plays")),
            "defensive_explosive_rate": _ratio(opponent.get("explosives"), opponent.get("plays")),
            "pressure_sack_rate": _ratio(opponent.get("sacks"), opponent.get("dropbacks")),
        })
    return rows


async def _nflverse_metrics(client, season):
    url = f"https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
    key = "nflverse-v3-" + str(season)
    cached = _read_cache(key)
    if cached and time.time() - cached["timestamp"] < 12 * 3600:
        return cached["data"], False, url
    try:
        response = await client.get(url, timeout=90, follow_redirects=True)
        response.raise_for_status()
        if len(response.content) > 100 * 1024 * 1024:
            raise ValueError("Unexpectedly large play-data download")
        rows = await asyncio.to_thread(_aggregate_nflverse, response.content)
        if not rows:
            raise ValueError("No current-season play data")
        _write_cache(key, rows)
        return rows, False, url
    except Exception:
        if cached and time.time() - cached["timestamp"] < 7 * 86400:
            return cached["data"], True, url
        raise RuntimeError("NFL advanced play data unavailable") from None


def _attach_nfl_metrics(history, teams, rows, cutoff):
    abbreviations = {team.get("abbreviation"): team_id for team_id, team in teams.items()}
    for source, target in NFLVERSE_ABBREVIATIONS.items():
        if target in abbreviations:
            abbreviations[source] = abbreviations[target]
    lookup = {}
    for row in rows:
        if row.get("game_date", "9999") >= cutoff:
            continue
        team_id = abbreviations.get(row["team"])
        opponent_id = abbreviations.get(row["opponent"])
        if team_id and opponent_id:
            lookup[(row["game_date"], team_id, opponent_id)] = row
    matched = 0
    for game in history:
        local_date = _parse_time(game["game_time"]).astimezone(EASTERN).date().isoformat()
        for side, opponent in (("home", "away"), ("away", "home")):
            row = lookup.get((local_date, game[side + "_team_id"], game[opponent + "_team_id"]))
            if row:
                game[side + "_metrics"] = {k: v for k, v in row.items() if k not in ("team", "opponent", "home_team", "away_team")}
                # nflverse's closing spread is positive when home is favored.
                # Displayed bookmaker home handicap has the opposite sign.
                game["closing_home_margin"] = row.get("closing_home_margin")
                game["closing_home_spread"] = -row["closing_home_margin"] if row.get("closing_home_margin") is not None else None
                game["closing_total"] = row.get("closing_total")
        if "home_metrics" in game and "away_metrics" in game:
            matched += 1
    current = defaultdict(list)
    for (game_date, team_id, opponent_id), row in lookup.items():
        current[team_id].append(row)
    mappings = {
        "offense_epa_per_play": ("epa_per_play", "plays"),
        "defense_epa_per_play": ("defensive_epa_per_play", "defensive_plays"),
        "pass_epa": ("pass_epa", "dropbacks"), "rush_epa": ("rush_epa", "rush_plays"),
        "success_rate": ("success_rate", "plays"),
        "defense_success_rate": ("defensive_success_rate", "defensive_plays"),
        "explosive_rate": ("explosive_rate", "plays"),
        "defense_explosive_rate": ("defensive_explosive_rate", "defensive_plays"),
    }
    for team_id, values in current.items():
        latest = max(values, key=lambda row: row["game_date"])
        teams[team_id]["recent_primary_passer"] = latest.get("primary_passer")
        teams[team_id]["recent_primary_passer_date"] = latest.get("game_date")
        teams[team_id]["recent_primary_passer_note"] = "Most recorded dropbacks in the latest completed game; not confirmation of this week's starter."
        for output, (metric, weight) in mappings.items():
            measured = [(row.get(metric), row.get(weight)) for row in values if row.get(metric) is not None and row.get(weight)]
            if measured:
                teams[team_id]["metrics"][output] = round(sum(v * w for v, w in measured) / sum(w for _, w in measured), 6)
        teams[team_id]["source_coverage"].append("nflverse_neutral_situation_play_data")
    return matched


async def build_football_snapshot(league: str, target_date: Optional[date] = None) -> dict:
    if league not in LEAGUES:
        raise ValueError("Football league must be nfl or cfb")
    config = LEAGUES[league]
    now = datetime.now(timezone.utc)
    target = target_date or now.astimezone(EASTERN).date()
    season = target.year - 1 if target.month <= 2 else target.year
    base = f"{ESPN}/{config['slug']}"
    health, warnings, teams = [], [], {}
    semaphore = asyncio.Semaphore(8)
    limits = httpx.Limits(max_connections=12, max_keepalive_connections=8)
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, limits=limits) as client:
        try:
            calendar, _ = await _fetch_json(client, base + "/scoreboard", {"dates": target.strftime("%Y%m%d"), "limit": 200}, ttl=3600)
            selected, _, calendars = _select_week(calendar, target)
        except Exception:
            raise RuntimeError(f"{config['label']} weekly schedule is temporarily unavailable") from None
        if selected is None:
            start = target - timedelta(days=target.weekday())
            return {"league": league, "date": target.isoformat(), "season": season,
                    "week_label": f"{config['label']} offseason", "week_start": start.isoformat(), "week_end": (start + timedelta(days=6)).isoformat(),
                    "games": [], "historical_games": [], "teams": {}, "warnings": [],
                    "source_health": [{"name": "ESPN league calendar", "status": "ok", "url": base + "/scoreboard", "note": "No regular-season or postseason week is active."}],
                    "odds_status": {}}
        week_start = selected["start"].astimezone(EASTERN).date()
        # ESPN calendar days use a Pacific reporting boundary (03:00 Eastern).
        # Display the last named calendar day, rather than the following morning.
        week_end = selected["end"].astimezone(ZoneInfo("America/Los_Angeles")).date()
        params = {"dates": season, "week": selected["number"], "seasontype": selected["season_type"], "limit": 200}
        if league == "cfb":
            params["groups"] = 80
        try:
            current_board, _ = await _fetch_json(client, base + "/scoreboard", params, ttl=3600)
            if not isinstance(current_board.get("events"), list):
                raise ValueError("Missing schedule events")
        except Exception:
            raise RuntimeError(f"{config['label']} weekly schedule is temporarily unavailable") from None
        health.append({"name": "ESPN weekly schedule", "status": "ok", "url": base + "/scoreboard", "note": f"{len(current_board['events'])} scheduled games in the selected league week."})
        games, current_completed = [], []
        comparison_time = now if target == now.astimezone(EASTERN).date() else datetime.combine(target, day_time.min, EASTERN).astimezone(timezone.utc)
        for event in current_board["events"]:
            game, event_teams = _parse_event(event)
            if not game or int(game.get("season") or 0) != season:
                continue
            teams.update(event_teams)
            if game["status"] == "pre" and _parse_time(game["game_time"]) > comparison_time:
                games.append(game)
            elif game["completed"]:
                current_completed.append(game)

        historical_requests = []
        for stage in calendars:
            if str(stage.get("value")) not in ("2", "3"):
                continue
            for entry in stage.get("entries", []):
                end = _parse_time(entry.get("endDate"))
                if end and end < selected["start"]:
                    historical_requests.append((int(stage["value"]), int(entry["value"])))

        async def historical_week(stage, number):
            query = {"dates": season, "week": number, "seasontype": stage, "limit": 200}
            if league == "cfb":
                query["groups"] = 80
            try:
                async with semaphore:
                    board, stale = await _fetch_json(client, base + "/scoreboard", query, ttl=12 * 3600, allow_stale=True)
                if not isinstance(board.get("events"), list):
                    raise ValueError("Missing historical events")
                return board["events"], stale, True
            except Exception:
                return [], False, False

        prior_weeks = await asyncio.gather(*(historical_week(stage, number) for stage, number in historical_requests))
        history_by_id = {}
        for events, _, _ in prior_weeks:
            for event in events:
                game, event_teams = _parse_event(event)
                if not game or not game["completed"] or game["home_score"] is None or game["away_score"] is None:
                    continue
                if int(game.get("season") or 0) != season or _parse_time(game["game_time"]).astimezone(EASTERN).date() >= week_start:
                    continue
                history_by_id[game["game_id"]] = game
                for team_id, team in event_teams.items():
                    teams.setdefault(team_id, team)
        history = sorted(history_by_id.values(), key=lambda game: game["game_time"])
        failures = sum(not success for _, _, success in prior_weeks)
        stale_history = any(stale for _, stale, _ in prior_weeks)
        health.append({"name": "ESPN completed results", "status": "partial" if failures or stale_history else "ok", "url": base + "/scoreboard",
                       "note": f"{len(history)} completed games before this week; {failures} unavailable weekly feeds."})
        if failures:
            warnings.append(f"{failures} historical schedule feeds were unavailable; model training coverage is incomplete.")
        if stale_history:
            warnings.append("Some completed results use an older cached feed.")

        active_ids = sorted({game[side + "_team_id"] for game in games for side in ("home", "away")})
        async def team_stats(team_id):
            try:
                async with semaphore:
                    stats, stale = await _fetch_json(client, base + f"/teams/{team_id}/statistics", {"season": season}, ttl=12 * 3600, allow_stale=True)
                played, metrics = _parse_team_metrics(stats, season, league)
                teams[team_id].update({"games_played": played, "metrics": metrics, "metrics_as_of": now.isoformat(), "metrics_scope": "current_season_at_fetch"})
                teams[team_id]["source_coverage"].append("espn_season_statistics")
                return True, stale
            except Exception:
                return False, False
        stats_results = await asyncio.gather(*(team_stats(team_id) for team_id in active_ids))
        successes = sum(success for success, _ in stats_results)
        health.append({"name": "ESPN team efficiencies", "status": "ok" if successes == len(active_ids) else "partial", "url": base + "/teams",
                       "note": f"Measured current-season statistics for {successes}/{len(active_ids)} upcoming teams. Season summaries are descriptive; they are not historical validation inputs."})
        if successes < len(active_ids):
            warnings.append(f"Season efficiency statistics missing for {len(active_ids) - successes} teams.")
        if any(stale for _, stale in stats_results):
            warnings.append("Some season efficiency statistics use an older cached feed.")
        counts = defaultdict(int)
        latest_played = {}
        for previous in history + current_completed:
            for side in ("home", "away"):
                team_id = previous[side + "_team_id"]
                counts[team_id] += 1
                kickoff = _parse_time(previous["game_time"])
                if kickoff and kickoff <= now:
                    latest_played[team_id] = max(kickoff, latest_played.get(team_id, kickoff))
        for team_id in teams:
            if not teams[team_id]["games_played"]:
                teams[team_id]["games_played"] = counts[team_id]
        for game in games:
            kickoff = _parse_time(game["game_time"])
            for side in ("home", "away"):
                previous = latest_played.get(game[side + "_team_id"])
                game["rest"][side + "_days"] = round((kickoff - previous).total_seconds() / 86400, 2) if previous else None

        try:
            injury_data, stale = await _fetch_json(client, base + "/injuries", ttl=3600, allow_stale=True)
            injuries, current_count, ignored_count = _parse_injuries(injury_data, season, now)
            injury_status = "ok" if current_count and not stale else "partial" if current_count else "unavailable"
            for team_id, team in teams.items():
                team["injuries"] = injuries.get(team_id, [])
                team["injury_status"] = injury_status
            health.append({"name": "ESPN injury reports", "status": injury_status, "url": base + "/injuries",
                           "note": f"{current_count} current-season player reports; {ignored_count} obsolete or undated reports excluded. Player role and starter confirmation are not inferred."})
            if not current_count:
                warnings.append("Current injury reporting is unavailable. Confirm starting quarterbacks and important absences before considering a pick.")
        except Exception:
            health.append({"name": "ESPN injury reports", "status": "unavailable", "url": base + "/injuries", "note": "No current injury feed could be verified."})
            warnings.append("Injury reporting could not be refreshed; quarterback availability is unverified.")

        if league == "nfl":
            url = f"https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.csv.gz"
            try:
                advanced_rows, stale, url = await _nflverse_metrics(client, season)
                matched = _attach_nfl_metrics(history, teams, advanced_rows, week_start.isoformat())
                health.append({"name": "nflverse EPA and success rates", "status": "partial" if stale or matched < len(history) else "ok", "url": url,
                               "note": f"Per-game play features matched to {matched}/{len(history)} completed games. Excludes kneels/spikes and plays outside 5%-95% pre-play win probability."})
                if matched < len(history):
                    warnings.append(f"Advanced NFL play features cover {matched}/{len(history)} completed games; remaining games use the scoring-data fallback.")
                if stale:
                    warnings.append("NFL advanced play statistics use a cached download because the source refresh failed.")
            except Exception:
                health.append({"name": "nflverse EPA and success rates", "status": "unavailable", "url": url, "note": "Advanced play data unavailable; no EPA or success values are fabricated."})
                warnings.append("NFL advanced play data could not be loaded; the model uses its scoring-data fallback.")
        else:
            health.append({"name": "CFB play-level EPA", "status": "not_used", "url": "https://github.com/sportsdataverse/cfbfastR-cfb-data", "note": "Play-level CFB EPA is not integrated in this version. Opponent-adjusted scoring and measured ESPN efficiency context are available."})

        try:
            odds_games = await get_game_odds(sport=config["sport"])
            odds_status = dict(ODDS_STATUS.get(config["sport"], {}))
            _attach_odds(games, odds_games, teams, odds_status)
            priced = sum(bool(game["odds"]) for game in games)
            health.append({"name": "The Odds API", "status": "stale" if odds_status.get("stale") else "ok", "url": "https://the-odds-api.com/", "note": f"Matched bookmaker prices for {priced}/{len(games)} upcoming games; missing prices do not become betting picks."})
            if odds_status.get("stale"):
                warnings.append("Bookmaker odds are from an older cached update.")
        except Exception:
            odds_status = {"status": "unavailable"}
            health.append({"name": "The Odds API", "status": "unavailable", "url": "https://the-odds-api.com/", "note": "Bookmaker prices unavailable; no market edge can be verified."})
            warnings.append("Bookmaker prices could not be loaded. Unpriced games are analysis only.")
    return {"league": league, "date": target.isoformat(), "season": season,
            "week_label": f"{config['label']} {selected['label']}", "week_start": week_start.isoformat(), "week_end": week_end.isoformat(),
            "games": sorted(games, key=lambda game: game["game_time"]), "historical_games": history,
            "teams": teams, "source_health": health, "warnings": warnings, "odds_status": odds_status,
            "history_complete": not failures, "generated_at": now.isoformat()}
