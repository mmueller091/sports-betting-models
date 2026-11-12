"""
MLB Stats API data fetcher.
Uses the free MLB Stats API (statsapi.mlb.com) for schedules, lineups, and player stats.
"""

import asyncio
import httpx
from datetime import date, timedelta
from typing import Optional

BASE = "https://statsapi.mlb.com/api/v1"


async def get_todays_games(game_date: Optional[date] = None) -> list[dict]:
    """Return list of scheduled games for the given date."""
    d = game_date or date.today()
    url = f"{BASE}/schedule"
    params = {
        "sportId": 1,
        "date": d.strftime("%Y-%m-%d"),
        "hydrate": "team,probablePitcher,linescore,weather,venue",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    data = resp.json()
    games = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            games.append(_parse_game(g))
    return games


def _parse_game(g: dict) -> dict:
    away = g["teams"]["away"]
    home = g["teams"]["home"]
    return {
        "game_id": g["gamePk"],
        "status": g["status"]["abstractGameState"],
        "venue": g.get("venue", {}).get("name"),
        "game_time": g.get("gameDate"),
        "away_team": away["team"]["name"],
        "away_team_id": away["team"]["id"],
        "away_probable_pitcher": _extract_pitcher(away),
        "home_team": home["team"]["name"],
        "home_team_id": home["team"]["id"],
        "home_probable_pitcher": _extract_pitcher(home),
    }


def _extract_pitcher(team_entry: dict) -> Optional[dict]:
    p = team_entry.get("probablePitcher")
    if not p:
        return None
    return {"id": p["id"], "name": p["fullName"]}


async def get_pitcher_stats(pitcher_id: int, season: Optional[int] = None) -> dict:
    """Fetch season stats for a pitcher. Returns ERA, FIP proxy, K%, BB%, IP."""
    season = season or date.today().year
    url = f"{BASE}/people/{pitcher_id}/stats"
    params = {"stats": "season", "group": "pitching", "season": season}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    data = resp.json()
    stats = {}
    for split_group in data.get("stats", []):
        for split in split_group.get("splits", []):
            s = split.get("stat", {})
            stats = {
                "era": float(s.get("era", 99)),
                "whip": float(s.get("whip", 99)),
                "k_per_9": float(s.get("strikeoutsPer9Inn", 0)),
                "bb_per_9": float(s.get("walksPer9Inn", 99)),
                "innings_pitched": float(s.get("inningsPitched", 0)),
                "hits_per_9": float(s.get("hitsPer9Inn", 99)),
                "hr_per_9": float(s.get("homeRunsPer9", 99)),
                "avg_against": float(s.get("avg", 0.300)),
                "games_started": int(s.get("gamesStarted", 0) or 0),
                "batters_faced": int(s.get("battersFaced", 0) or 0),
                "hits": int(s.get("hits", 0) or 0),
                "strikeouts": int(s.get("strikeOuts", 0) or 0),
                "walks": int(s.get("baseOnBalls", 0) or 0),
            }
    return stats


async def get_pitcher_splits(pitcher_id: int, season: Optional[int] = None) -> dict:
    """Fetch vs LHB / vs RHB splits for a pitcher."""
    season = season or date.today().year
    url = f"{BASE}/people/{pitcher_id}/stats"
    params = {"stats": "statSplits", "group": "pitching", "season": season}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    data = resp.json()
    splits = {"vs_lhb": {}, "vs_rhb": {}}
    for split_group in data.get("stats", []):
        for split in split_group.get("splits", []):
            desc = split.get("split", {}).get("description", "")
            s = split.get("stat", {})
            entry = {
                "avg": float(s.get("avg", 0.300)),
                "ops": float(s.get("ops", 0.750)),
                "ab": int(s.get("atBats", 0) or 0),
                "pa": int(s.get("plateAppearances", 0) or 0),
                "hits": int(s.get("hits", 0) or 0),
            }
            if "vs. Left" in desc:
                splits["vs_lhb"] = entry
            elif "vs. Right" in desc:
                splits["vs_rhb"] = entry
    return splits


async def get_batter_stats(batter_id: int, season: Optional[int] = None) -> dict:
    """Fetch season stats for a batter."""
    season = season or date.today().year
    url = f"{BASE}/people/{batter_id}/stats"
    params = {"stats": "season", "group": "hitting", "season": season}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    data = resp.json()
    stats = {}
    for split_group in data.get("stats", []):
        for split in split_group.get("splits", []):
            s = split.get("stat", {})
            stats = {
                "avg": float(s.get("avg", 0.000)),
                "obp": float(s.get("obp", 0.000)),
                "slg": float(s.get("slg", 0.000)),
                "ops": float(s.get("ops", 0.000)),
                "at_bats": int(s.get("atBats", 0)),
                "hits": int(s.get("hits", 0)),
                "plate_appearances": int(s.get("plateAppearances", 0)),
                "strikeouts": int(s.get("strikeOuts", 0) or 0),
                "walks": int(s.get("baseOnBalls", 0) or 0),
            }
    return stats


async def get_batter_last_n_games(
    batter_id: int, n: int = 15, season: Optional[int] = None
) -> dict:
    """Fetch last-N-game rolling stats for a batter."""
    season = season or date.today().year
    url = f"{BASE}/people/{batter_id}/stats"
    params = {"stats": "lastXGames", "group": "hitting", "season": season, "limit": n}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    data = resp.json()
    stats = {}
    for split_group in data.get("stats", []):
        for split in split_group.get("splits", []):
            s = split.get("stat", {})
            stats = {
                "avg": float(s.get("avg", 0.000)),
                "obp": float(s.get("obp", 0.000)),
                "ops": float(s.get("ops", 0.000)),
                "hits": int(s.get("hits", 0)),
                "at_bats": int(s.get("atBats", 0)),
                "plate_appearances": int(s.get("plateAppearances", 0) or 0),
                "strikeouts": int(s.get("strikeOuts", 0) or 0),
                "walks": int(s.get("baseOnBalls", 0) or 0),
            }
    return stats


async def get_batter_vs_pitcher(
    batter_id: int, pitcher_id: int, season: Optional[int] = None
) -> dict:
    """Fetch career batter vs pitcher matchup stats."""
    season = season or date.today().year
    url = f"{BASE}/people/{batter_id}/stats"
    params = {
        "stats": "vsPlayer",
        "group": "hitting",
        "opposingPlayerId": pitcher_id,
        "season": season,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    data = resp.json()
    stats = {"pa": 0, "hits": 0, "avg": 0.0, "ops": 0.0}
    for split_group in data.get("stats", []):
        for split in split_group.get("splits", []):
            s = split.get("stat", {})
            pa = int(s.get("plateAppearances", 0))
            if pa >= 5:
                stats = {
                    "pa": pa,
                    "hits": int(s.get("hits", 0)),
                    "avg": float(s.get("avg", 0.0)),
                    "ops": float(s.get("ops", 0.0)),
                }
    return stats


async def get_lineup(game_id: int) -> dict:
    """
    Fetch the live/confirmed lineup for a game.
    Returns {'away': [...batters], 'home': [...batters]}
    Each batter: {id, name, batting_order, bats (L/R/S)}
    """
    url = f"{BASE}.1/game/{game_id}/feed/live"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    data = resp.json()
    boxscore = data.get("liveData", {}).get("boxscore", {})
    teams = boxscore.get("teams", {})
    result = {}
    for side in ("away", "home"):
        batters = []
        team_data = teams.get(side, {})
        batting_order = team_data.get("battingOrder", [])
        players = team_data.get("players", {})
        for order_idx, player_id in enumerate(batting_order):
            key = f"ID{player_id}"
            player = players.get(key, {})
            person = player.get("person", {})
            bat_side = player.get("batSide", {}).get("code") or person.get("batSide", {}).get("code") or "R"
            batters.append({
                "id": player_id,
                "name": person.get("fullName", "Unknown"),
                "batting_order": order_idx + 1,
                "bats": bat_side,
            })
        result[side] = batters
    return result


async def get_projected_lineup(team_id: int, before_date: Optional[date] = None) -> list[dict]:
    """
    Projected lineup: the batting order from the team's most recent completed game.
    Used when today's lineup isn't posted yet (e.g. picking before noon).
    Returns the same shape as get_lineup() batters.
    """
    target = before_date or date.today()
    games = await _fetch_team_schedule(
        team_id, target - timedelta(days=6), target - timedelta(days=1)
    )
    completed = [_parse_completed_team_game(g, team_id) for g in games]
    completed = [g for g in completed if g]
    if not completed:
        return []
    completed.sort(key=lambda g: g["date"])
    last_game = completed[-1]

    feed = await _fetch_game_feed(last_game["game_id"])
    teams = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
    for side in ("away", "home"):
        team_data = teams.get(side, {})
        if team_data.get("team", {}).get("id") != team_id:
            continue
        batting_order = team_data.get("battingOrder", [])
        players = team_data.get("players", {})
        batters = []
        for order_idx, player_id in enumerate(batting_order[:9]):
            player = players.get(f"ID{player_id}", {})
            person = player.get("person", {})
            bat_side = player.get("batSide", {}).get("code") or "R"
            batters.append({
                "id": player_id,
                "name": person.get("fullName", "Unknown"),
                "batting_order": order_idx + 1,
                "bats": bat_side,
                "projected": True,
            })
        return batters
    return []


async def get_pitcher_recent_starts(
    pitcher_id: int, n: int = 5, season: Optional[int] = None
) -> dict:
    """
    Fetch the last N starts for a pitcher from the game log.
    Returns ERA/WHIP/hits-allowed over the recent stretch — much more predictive than season totals.
    """
    season = season or date.today().year
    url = f"{BASE}/people/{pitcher_id}/stats"
    params = {"stats": "gameLog", "group": "pitching", "season": season}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        data = resp.json()
        starts = []
        for split_group in data.get("stats", []):
            for split in split_group.get("splits", []):
                s = split.get("stat", {})
                ip = float(s.get("inningsPitched", 0) or 0)
                if ip < 1.0:
                    continue  # Skip relief appearances
                starts.append({
                    "date": split.get("date", ""),
                    "ip": ip,
                    "hits": int(s.get("hits", 0) or 0),
                    "runs": int(s.get("runs", 0) or 0),
                    "er": int(s.get("earnedRuns", 0) or 0),
                    "k": int(s.get("strikeOuts", 0) or 0),
                    "bb": int(s.get("baseOnBalls", 0) or 0),
                })
        recent = starts[-n:] if starts else []
        if not recent:
            return {"recent_era": None, "recent_whip": None, "recent_hits_per_ip": None, "starts": 0}

        total_ip = sum(g["ip"] for g in recent)
        total_er = sum(g["er"] for g in recent)
        total_hits = sum(g["hits"] for g in recent)
        total_bb = sum(g["bb"] for g in recent)
        if total_ip < 1:
            return {"recent_era": None, "recent_whip": None, "recent_hits_per_ip": None, "starts": 0}

        return {
            "recent_era": round(total_er / total_ip * 9, 2),
            "recent_whip": round((total_hits + total_bb) / total_ip, 3),
            "recent_hits_per_ip": round(total_hits / total_ip, 3),
            "starts": len(recent),
        }
    except Exception:
        return {"recent_era": None, "recent_whip": None, "recent_hits_per_ip": None, "starts": 0}


async def get_batter_splits(batter_id: int, season: Optional[int] = None) -> dict:
    """
    Fetch home/away and day/night splits for a batter.
    Returns home_avg, away_avg, day_avg, night_avg.
    """
    season = season or date.today().year
    url = f"{BASE}/people/{batter_id}/stats"
    params = {
        "stats": "statSplits",
        "group": "hitting",
        "season": season,
        "sitCodes": "h,a,d,n",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        data = resp.json()
        splits = {}
        for split_group in data.get("stats", []):
            for split in split_group.get("splits", []):
                desc = split.get("split", {}).get("description", "")
                s = split.get("stat", {})
                ab = int(s.get("atBats", 0) or 0)
                if ab < 20:
                    continue  # Not enough sample
                avg = float(s.get("avg", 0.0) or 0.0)
                if "Home" in desc:
                    splits["home_avg"] = avg
                    splits["home_ab"] = ab
                elif "Away" in desc:
                    splits["away_avg"] = avg
                    splits["away_ab"] = ab
                elif "Day" in desc:
                    splits["day_avg"] = avg
                elif "Night" in desc:
                    splits["night_avg"] = avg
        return splits
    except Exception:
        return {}


async def get_players_hands(player_ids: list[int]) -> dict[int, dict]:
    """
    Batch-fetch pitch hand and bat side for a list of players.
    Returns {player_id: {"pitch_hand": "R", "bat_side": "L"}}
    """
    if not player_ids:
        return {}
    url = f"{BASE}/people"
    params = {"personIds": ",".join(str(i) for i in player_ids)}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    data = resp.json()
    hands = {}
    for person in data.get("people", []):
        hands[person["id"]] = {
            "pitch_hand": person.get("pitchHand", {}).get("code", "R"),
            "bat_side": person.get("batSide", {}).get("code", "R"),
        }
    return hands


async def get_batter_vs_hand_splits(
    batter_id: int, season: Optional[int] = None
) -> dict:
    """
    Fetch batter splits vs LHP and RHP.
    Returns {"vs_lhp": {avg, ops, ab}, "vs_rhp": {avg, ops, ab}}
    """
    season = season or date.today().year
    url = f"{BASE}/people/{batter_id}/stats"
    params = {
        "stats": "statSplits",
        "group": "hitting",
        "season": season,
        "sitCodes": "vl,vr",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        data = resp.json()
        splits = {"vs_lhp": {}, "vs_rhp": {}}
        for split_group in data.get("stats", []):
            for split in split_group.get("splits", []):
                desc = split.get("split", {}).get("description", "")
                s = split.get("stat", {})
                entry = {
                    "avg": float(s.get("avg", 0.0) or 0.0),
                    "ops": float(s.get("ops", 0.0) or 0.0),
                    "ab": int(s.get("atBats", 0) or 0),
                }
                if "Left" in desc:
                    splits["vs_lhp"] = entry
                elif "Right" in desc:
                    splits["vs_rhp"] = entry
        return splits
    except Exception:
        return {}


async def get_team_bullpen_quality(team_id: int, season: Optional[int] = None) -> dict:
    """
    Fetch team bullpen (relief pitching) quality: ERA, WHIP, avg against.
    Uses the team statSplits endpoint with the reliever situation code.
    """
    season = season or date.today().year
    url = f"{BASE}/teams/{team_id}/stats"
    params = {
        "stats": "statSplits",
        "group": "pitching",
        "season": season,
        "sitCodes": "rp",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        data = resp.json()
        for split_group in data.get("stats", []):
            for split in split_group.get("splits", []):
                s = split.get("stat", {})
                return {
                    "bullpen_era": float(s.get("era", 0) or 0),
                    "bullpen_whip": float(s.get("whip", 0) or 0),
                    "bullpen_avg_against": float(s.get("avg", 0) or 0),
                    "bullpen_ip": float(s.get("inningsPitched", 0) or 0),
                    "bullpen_batters_faced": int(s.get("battersFaced", 0) or 0),
                }
        return {}
    except Exception:
        return {}


async def get_final_scores_for_date(game_date: date) -> dict:
    """
    Fetch final scores for all games on a date (for grading picks).
    Returns {(away_team, home_team): {...}} plus game_id keys.
    """
    url = f"{BASE}/schedule"
    params = {
        "sportId": 1,
        "date": game_date.strftime("%Y-%m-%d"),
        "hydrate": "team,linescore",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    data = resp.json()
    results = {}
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            away = g["teams"]["away"]
            home = g["teams"]["home"]
            entry = {
                "game_id": g["gamePk"],
                "away_team": away["team"]["name"],
                "home_team": home["team"]["name"],
                "away_score": int(away.get("score", 0)),
                "home_score": int(home.get("score", 0)),
            }
            results[(entry["away_team"], entry["home_team"])] = entry
    return results


async def get_boxscore_batting(game_id: int) -> dict[int, dict]:
    """Fetch batter outcomes for a finished game, keyed by MLB player ID."""
    url = f"{BASE}/game/{game_id}/boxscore"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    data = resp.json()
    batting_lines = {}
    for side in ("away", "home"):
        players = data.get("teams", {}).get(side, {}).get("players", {})
        for key, player in players.items():
            batting = player.get("stats", {}).get("batting", {})
            if batting.get("atBats") is None and batting.get("plateAppearances") is None:
                continue
            pid = player.get("person", {}).get("id")
            if pid:
                batting_lines[pid] = {
                    "hits": int(batting.get("hits", 0) or 0),
                    "at_bats": int(batting.get("atBats", 0) or 0),
                    "plate_appearances": int(batting.get("plateAppearances", 0) or 0),
                }
    return batting_lines


async def get_boxscore_hits(game_id: int) -> dict[int, int]:
    """Compatibility wrapper returning only hits per batter."""
    batting_lines = await get_boxscore_batting(game_id)
    return {player_id: line["hits"] for player_id, line in batting_lines.items()}


async def get_team_recent_results(
    team_id: int,
    game_date: Optional[date] = None,
    n: int = 10,
    lookback_days: int = 21,
) -> dict:
    """
    Return recent team form and schedule density metrics.
    """
    target_date = game_date or date.today()
    games = await _fetch_team_schedule(
        team_id,
        target_date - timedelta(days=lookback_days),
        target_date - timedelta(days=1),
    )
    completed = [_parse_completed_team_game(g, team_id) for g in games]
    completed = [g for g in completed if g]
    completed.sort(key=lambda g: g["date"])
    recent = completed[-n:]

    if not recent:
        return {
            "recent_games": [],
            "win_pct_last_10": 0.5,
            "runs_for_per_game_last_10": 4.3,
            "runs_against_per_game_last_10": 4.3,
            "run_diff_per_game_last_10": 0.0,
            "rest_days": 1,
            "games_last_3": 0,
            "games_last_5": 0,
            "road_trip_len": 0,
        }

    wins = sum(1 for g in recent if g["win"])
    runs_for = sum(g["runs_for"] for g in recent)
    runs_against = sum(g["runs_against"] for g in recent)
    last_game_date = date.fromisoformat(recent[-1]["date"])
    road_trip_len = 0
    for g in reversed(recent):
        if g["is_home"]:
            break
        road_trip_len += 1

    return {
        "recent_games": recent,
        "win_pct_last_10": round(wins / max(len(recent), 1), 3),
        "runs_for_per_game_last_10": round(runs_for / max(len(recent), 1), 2),
        "runs_against_per_game_last_10": round(runs_against / max(len(recent), 1), 2),
        "run_diff_per_game_last_10": round((runs_for - runs_against) / max(len(recent), 1), 2),
        "rest_days": max((target_date - last_game_date).days - 1, 0),
        "games_last_3": _count_games_since(completed, target_date, 3),
        "games_last_5": _count_games_since(completed, target_date, 5),
        "road_trip_len": road_trip_len,
    }


async def get_team_bullpen_usage(
    team_id: int,
    game_date: Optional[date] = None,
    lookback_days: int = 3,
) -> dict:
    """Return recent bullpen workload and a fatigue score."""
    target_date = game_date or date.today()
    games = await _fetch_team_schedule(
        team_id,
        target_date - timedelta(days=lookback_days + 3),
        target_date - timedelta(days=1),
    )
    completed = [_parse_completed_team_game(g, team_id) for g in games]
    completed = [g for g in completed if g]
    relevant = [
        g for g in completed
        if 0 < (target_date - date.fromisoformat(g["date"])).days <= lookback_days
    ]

    if not relevant:
        return {
            "games_tracked": 0,
            "pitches_last_1d": 0,
            "pitches_last_2d": 0,
            "pitches_last_3d": 0,
            "relievers_used_last_3d": 0,
            "high_usage_arms_last_2d": 0,
            "fatigue_index": 0.0,
            "notes": ["Bullpen rested"],
        }

    feed_tasks = [_fetch_game_feed(g["game_id"]) for g in relevant]
    feeds = await asyncio.gather(*feed_tasks, return_exceptions=True)

    usage_rows = []
    for game_meta, feed in zip(relevant, feeds):
        if isinstance(feed, Exception):
            continue
        usage_rows.append(_extract_team_bullpen_usage(feed, team_id, game_meta["date"]))

    usage_rows = [u for u in usage_rows if u]
    pitches_last_1d = _sum_recent_bullpen_pitches(usage_rows, target_date, 1)
    pitches_last_2d = _sum_recent_bullpen_pitches(usage_rows, target_date, 2)
    pitches_last_3d = _sum_recent_bullpen_pitches(usage_rows, target_date, 3)
    relievers_used = sum(r["relievers_used"] for r in usage_rows)
    high_usage_arms = sum(
        r["high_usage_arms"]
        for r in usage_rows
        if (target_date - date.fromisoformat(r["date"])).days <= 2
    )

    fatigue_index = round(
        min(
            3.0,
            (pitches_last_1d / 35)
            + (max(pitches_last_2d - pitches_last_1d, 0) / 45)
            + (max(pitches_last_3d - pitches_last_2d, 0) / 60)
            + high_usage_arms * 0.35,
        ),
        2,
    )

    notes = []
    if pitches_last_1d >= 35:
        notes.append(f"Heavy bullpen load yesterday ({pitches_last_1d} pitches)")
    if high_usage_arms:
        notes.append(f"{high_usage_arms} high-usage reliever(s) in last 2 days")
    if not notes:
        notes.append("Bullpen workload normal")

    return {
        "games_tracked": len(usage_rows),
        "pitches_last_1d": pitches_last_1d,
        "pitches_last_2d": pitches_last_2d,
        "pitches_last_3d": pitches_last_3d,
        "relievers_used_last_3d": relievers_used,
        "high_usage_arms_last_2d": high_usage_arms,
        "fatigue_index": fatigue_index,
        "notes": notes,
    }


async def _fetch_team_schedule(team_id: int, start_date: date, end_date: date) -> list[dict]:
    url = f"{BASE}/schedule"
    params = {
        "sportId": 1,
        "teamId": team_id,
        "startDate": start_date.strftime("%Y-%m-%d"),
        "endDate": end_date.strftime("%Y-%m-%d"),
        "hydrate": "linescore,team,venue",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    data = resp.json()
    games = []
    for date_entry in data.get("dates", []):
        games.extend(date_entry.get("games", []))
    return games


def _parse_completed_team_game(game: dict, team_id: int) -> Optional[dict]:
    if game.get("status", {}).get("abstractGameState") != "Final":
        return None
    away = game.get("teams", {}).get("away", {})
    home = game.get("teams", {}).get("home", {})
    away_id = away.get("team", {}).get("id")
    home_id = home.get("team", {}).get("id")
    if team_id not in (away_id, home_id):
        return None

    is_home = home_id == team_id
    team_score = int(home.get("score", 0) if is_home else away.get("score", 0))
    opp_score = int(away.get("score", 0) if is_home else home.get("score", 0))
    return {
        "game_id": game.get("gamePk"),
        "date": game.get("gameDate", "")[:10] or game.get("officialDate"),
        "win": team_score > opp_score,
        "runs_for": team_score,
        "runs_against": opp_score,
        "is_home": is_home,
        "venue": game.get("venue", {}).get("name"),
    }


async def _fetch_game_feed(game_id: int) -> dict:
    url = f"{BASE}.1/game/{game_id}/feed/live"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    return resp.json()


def _extract_team_bullpen_usage(feed: dict, team_id: int, game_date: str) -> Optional[dict]:
    teams = feed.get("liveData", {}).get("boxscore", {}).get("teams", {})
    for side in ("away", "home"):
        team_data = teams.get(side, {})
        current_team_id = team_data.get("team", {}).get("id")
        if current_team_id != team_id:
            continue

        pitchers = team_data.get("pitchers", [])
        players = team_data.get("players", {})
        reliever_ids = pitchers[1:] if len(pitchers) > 1 else []

        total_pitches = 0
        relievers_used = 0
        high_usage_arms = 0
        for pitcher_id in reliever_ids:
            player = players.get(f"ID{pitcher_id}", {})
            pitches = int(
                player.get("stats", {}).get("pitching", {}).get("numberOfPitches", 0) or 0
            )
            if pitches <= 0:
                continue
            relievers_used += 1
            total_pitches += pitches
            if pitches >= 20:
                high_usage_arms += 1

        return {
            "date": game_date,
            "bullpen_pitches": total_pitches,
            "relievers_used": relievers_used,
            "high_usage_arms": high_usage_arms,
        }
    return None


def _sum_recent_bullpen_pitches(rows: list[dict], target_date: date, days: int) -> int:
    return sum(
        row["bullpen_pitches"]
        for row in rows
        if 0 < (target_date - date.fromisoformat(row["date"])).days <= days
    )


def _count_games_since(games: list[dict], target_date: date, days: int) -> int:
    return sum(
        1
        for game in games
        if 0 < (target_date - date.fromisoformat(game["date"])).days <= days
    )
