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

MONEYPUCK_BASE = "https://moneypuck.com/moneypuck/playerData/careers"

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


async def get_team_xg_stats(season: int = 2025) -> dict[str, dict]:
    """
    Fetch MoneyPuck team-level xG stats.
    Returns {team_name: {xgf_pct, xga_per_game, hd_chance_pct, ...}}

    xGF% (expected goals for %) is the single best predictor of future team performance.
    League average = 50%. Above 52% = legitimately good. Below 48% = legitimately bad.
    """
    url = f"{MONEYPUCK_BASE}/{season}/regular/teams.csv"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        result = {}
        for row in reader:
            abbrev = row.get("team", "").upper()
            team_name = MP_TEAM_MAP.get(abbrev)
            if not team_name:
                continue
            gp = max(int(row.get("games_played", 1) or 1), 1)
            xgf = _safe_float(row.get("xGoalsFor"))
            xga = _safe_float(row.get("xGoalsAgainst"))
            xgf_pct = xgf / (xgf + xga) * 100 if (xgf + xga) > 0 else 50.0
            result[team_name] = {
                "xgf_pct": round(xgf_pct, 2),
                "xgf_per_game": round(xgf / gp, 3),
                "xga_per_game": round(xga / gp, 3),
                "hd_xgf_pct": _safe_float(row.get("highDangerxGoalsFor")),
                "corsi_for_pct": _safe_float(row.get("corsiForPct")),
                "fenwick_for_pct": _safe_float(row.get("fenwickForPct")),
                "sh_pct": _safe_float(row.get("shootingPct")),
                "sv_pct": _safe_float(row.get("savePct")),
                "pdo": _safe_float(row.get("pdo")),  # PDO = sh% + sv%, regresses to 1.000
            }
        return result
    except Exception:
        return {}


async def get_goalie_xg_stats(season: int = 2025) -> dict[str, dict]:
    """
    Fetch MoneyPuck goalie-level xG stats.
    Returns {player_name: {gsax, xga_per_60, hd_sv_pct, ...}}

    GSAx (Goals Saved Above Expected) is the best goalie metric.
    A goalie with GSAx > +10 is genuinely elite. < -10 is genuinely bad.
    """
    url = f"{MONEYPUCK_BASE}/{season}/regular/goalies.csv"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        result = {}
        for row in reader:
            name = row.get("name", "").strip()
            if not name:
                continue
            result[name] = {
                "gsax": _safe_float(row.get("GSAx")),          # Goals saved above expected
                "xga_per_60": _safe_float(row.get("xGoalsAgainst")),
                "hd_sv_pct": _safe_float(row.get("highDangerSavePct")),  # High-danger save%
                "md_sv_pct": _safe_float(row.get("mediumDangerSavePct")),
                "ld_sv_pct": _safe_float(row.get("lowDangerSavePct")),
                "games_played": int(row.get("games_played", 0) or 0),
                "quality_starts_pct": _safe_float(row.get("qualityStartPct")),
            }
        return result
    except Exception:
        return {}


async def get_skater_xg_stats(season: int = 2025) -> dict[str, dict]:
    """
    Fetch MoneyPuck skater-level xG stats.
    Returns {player_name: {xg_per_60, ixg_per_60, hd_chances, ...}}

    Individual xG (ixG) is the best predictor of future goal scoring
    — better than goals/game because it removes goalie/luck variance.
    """
    url = f"{MONEYPUCK_BASE}/{season}/regular/skaters.csv"
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        result = {}
        for row in reader:
            name = row.get("name", "").strip()
            situation = row.get("situation", "")
            if not name or situation != "all":  # Only take "all situations" rows
                continue
            result[name] = {
                "ixg_per_60": _safe_float(row.get("I_F_xGoals")),    # Individual xG
                "ixg_total": _safe_float(row.get("I_F_xGoals")),
                "hd_chances_per_60": _safe_float(row.get("I_F_highDangerShots")),
                "shots_per_60": _safe_float(row.get("I_F_shotsOnGoal")),
                "goals_above_expected": _safe_float(row.get("I_F_goalsAboveExpected")),
                "on_ice_xgf_pct": _safe_float(row.get("OnIce_F_xGoals")),
                "toi_per_game": _safe_float(row.get("icetime")),
                "pp_toi_pct": _safe_float(row.get("powerPlayToiPct")),
            }
        return result
    except Exception:
        return {}


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
            gname = goalie.get("name", "")
            if gname and gname in goalie_xg:
                goalie["xg_stats"] = goalie_xg[gname]
                stats["starting_goalie"] = goalie

            # Skater xG for top scorers
            scorers = game.get(f"{side}_scorers", [])
            for scorer in scorers:
                sname = scorer.get("name", "")
                if sname in skater_xg:
                    scorer["xg_stats"] = skater_xg[sname]

    return games


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default
