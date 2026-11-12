"""
MoneyPuck NHL data fetcher.
MoneyPuck provides free CSV downloads with Statcast-equivalent NHL data.
Best metrics: xGoals%, xGoals against, high-danger chance %

URLs: https://moneypuck.com/data.htm
"""

import httpx
import io
import csv
from typing import Optional
from data.nhl_stats import season_start
from data.source_health import warn

MONEYPUCK_BASE = "https://moneypuck.com/moneypuck/playerData/seasonSummary"

# Team abbreviation map: MoneyPuck uses standard NHL abbrevs
MP_TEAM_MAP = {
    "ANA": "Anaheim Ducks", "BOS": "Boston Bruins", "BUF": "Buffalo Sabres",
    "CGY": "Calgary Flames", "CAR": "Carolina Hurricanes", "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche", "CBJ": "Columbus Blue Jackets", "DAL": "Dallas Stars",
    "DET": "Detroit Red Wings", "EDM": "Edmonton Oilers", "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings", "MIN": "Minnesota Wild", "MTL": "Montreal Canadiens",
    "NSH": "Nashville Predators", "NJD": "New Jersey Devils", "NYI": "New York Islanders",
    "NYR": "New York Rangers", "OTT": "Ottawa Senators", "PHI": "Philadelphia Flyers",
    "PIT": "Pittsburgh Penguins", "SJS": "San Jose Sharks", "SEA": "Seattle Kraken",
    "STL": "St. Louis Blues", "TBL": "Tampa Bay Lightning", "TOR": "Toronto Maple Leafs",
    "UTA": "Utah Hockey Club", "VAN": "Vancouver Canucks", "VGK": "Vegas Golden Knights",
    "WSH": "Washington Capitals", "WPG": "Winnipeg Jets",
}


async def _season_rows(kind: str, season: Optional[int]) -> list[dict]:
    year = season if season is not None else season_start()
    url = f"{MONEYPUCK_BASE}/{year}/regular/{kind}.csv"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(response.text)))
        if not rows or "situation" not in rows[0]:
            raise ValueError("Expected situation-tagged MoneyPuck CSV")
        return [row for row in rows if not row.get("season") or str(row["season"]) == str(year)]
    except Exception:
        warn(f"MoneyPuck {kind} data unavailable for {year}; advanced inputs missing")
        return []


def _number(row, key):
    value = _safe_float(row.get(key), None)
    return value if value is not None and value == value and abs(value) != float("inf") else None


def _ratio(numerator, denominator, scale=1.0):
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator * scale


def _available(values):
    return {key: value for key, value in values.items() if value is not None}


async def get_team_xg_stats(season: Optional[int] = None) -> dict[str, dict]:
    """Read all-situation season totals and convert counts to per-game rates."""
    result = {}
    for row in await _season_rows("teams", season):
        if row.get("situation") != "all":
            continue
        name = MP_TEAM_MAP.get(row.get("team", "").upper())
        gp = _number(row, "games_played")
        xgf, xga = _number(row, "xGoalsFor"), _number(row, "xGoalsAgainst")
        if not name or not gp or gp <= 0 or xgf is None or xga is None or xgf + xga <= 0:
            continue
        shots_for, shots_against = _number(row, "shotsOnGoalFor"), _number(row, "shotsOnGoalAgainst")
        shooting = _ratio(_number(row, "goalsFor"), shots_for)
        against_rate = _ratio(_number(row, "goalsAgainst"), shots_against)
        save = None if against_rate is None else 1 - against_rate
        result[name] = _available({
            "xgf_pct": xgf / (xgf + xga) * 100,
            "xgf_per_game": xgf / gp, "xga_per_game": xga / gp,
            "sh_pct": shooting, "sv_pct": save,
            "pdo": shooting + save if shooting is not None and save is not None else None,
            "situation": "all", "season": season if season is not None else season_start(),
            "games_played": gp,
        })
    if not result:
        warn("MoneyPuck team rows missing required all-situation goals and games-played fields")
    return result


async def get_goalie_xg_stats(season: Optional[int] = None) -> dict[str, dict]:
    """Derive GSAx and per-60 rates from all-situation goalie counts."""
    result = {}
    for row in await _season_rows("goalies", season):
        if row.get("situation") != "all" or not row.get("name"):
            continue
        gp, ice = _number(row, "games_played"), _number(row, "icetime")
        # MoneyPuck goalie files use xGoals/goals; accept explicit equivalent
        # fields when present, but do not substitute zero for missing metrics.
        xga = _number(row, "xGoals")
        if xga is None:
            xga = _number(row, "xGoalsAgainst")
        goals = _number(row, "goals")
        gsax = _number(row, "GSAx")
        if gsax is None and xga is not None and goals is not None:
            gsax = xga - goals
        if not gp or gp <= 0 or not ice or ice <= 0 or gsax is None:
            continue
        hd_goal_rate = _ratio(_number(row, "highDangerGoals"), _number(row, "highDangerShots"))
        result[row["name"].strip()] = _available({
            "player_id": row.get("playerId"), "gsax": gsax,
            "xga_per_60": _ratio(xga, ice, 3600), "games_played": gp,
            # MoneyPuck defines highDangerShots as UNBLOCKED ATTEMPTS,
            # including misses. This is not save percentage on shots on goal.
            "hd_unblocked_no_goal_pct": None if hd_goal_rate is None else 1 - hd_goal_rate,
            "situation": "all", "season": season if season is not None else season_start(),
        })
    if not result:
        warn("MoneyPuck goalie rows missing required all-situation counts or ice time")
    return result


async def get_skater_xg_stats(season: Optional[int] = None) -> dict[str, dict]:
    """Convert skater season counts/seconds into per-60 and per-game inputs."""
    rows = await _season_rows("skaters", season)
    pp_time = {row.get("playerId") or row.get("name"): _number(row, "icetime")
               for row in rows if row.get("situation") == "5on4"}
    result = {}
    for row in rows:
        if row.get("situation") != "all" or not row.get("name"):
            continue
        gp, ice = _number(row, "games_played"), _number(row, "icetime")
        ixg = _number(row, "I_F_xGoals")
        if not gp or gp <= 0 or not ice or ice <= 0 or ixg is None:
            continue
        goals = _number(row, "I_F_goals")
        on_for, on_against = _number(row, "OnIce_F_xGoals"), _number(row, "OnIce_A_xGoals")
        on_total = on_for + on_against if on_for is not None and on_against is not None else None
        result[row["name"].strip()] = _available({
            "player_id": row.get("playerId"), "ixg_per_60": ixg * 3600 / ice,
            "ixg_total": ixg, "toi_per_game": ice / gp,
            "hd_chances_per_60": _ratio(_number(row, "I_F_highDangerShots"), ice, 3600),
            "shots_per_60": _ratio(_number(row, "I_F_shotsOnGoal"), ice, 3600),
            "goals_above_expected": None if goals is None else goals - ixg,
            "on_ice_xgf_pct": _ratio(on_for, on_total, 100),
            "pp_toi_pct": _ratio(pp_time.get(row.get("playerId") or row.get("name")), ice),
            "situation": "all", "season": season if season is not None else season_start(),
        })
    if not result:
        warn("MoneyPuck skater rows missing required all-situation xG, games or ice time")
    return result


def apply_xg_to_games(games: list[dict], team_xg: dict, goalie_xg: dict, skater_xg: dict) -> list[dict]:
    """Merge MoneyPuck xG data into enriched game objects."""
    for game in games:
        for side in ("away", "home"):
            team = game.get(f"{side}_team", "")
            stats = game.get(f"{side}_team_stats", {})

            # Team xG
            txg = team_xg.get(team, {})
            if txg:
                stats["xg"] = txg
                game[f"{side}_team_stats"] = stats

            # Goalie xG
            goalie = stats.get("starting_goalie", {})
            gxg = _player_xg(goalie_xg, goalie)
            if gxg:
                goalie["xg_stats"] = gxg
                stats["starting_goalie"] = goalie

            # Skater xG for top scorers
            scorers = game.get(f"{side}_scorers", [])
            for scorer in scorers:
                sxg = _player_xg(skater_xg, scorer)
                if sxg:
                    scorer["xg_stats"] = sxg

    return games


def _player_xg(rows: dict, player: dict) -> dict:
    player_id = player.get("id")
    if player_id is not None:
        matches = [row for row in rows.values() if str(row.get("player_id")) == str(player_id)]
        if len(matches) == 1:
            return matches[0]
    named = rows.get(player.get("name", ""), {})
    if named and player_id is not None and named.get("player_id") is not None:
        if str(named["player_id"]) != str(player_id):
            warn("MoneyPuck player identity does not match NHL player ID; advanced data omitted")
            return {}
    return named


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default
