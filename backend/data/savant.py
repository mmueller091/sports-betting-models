"""
Baseball Savant data fetcher.
Uses Savant's public CSV export endpoints for Statcast xStats.
No API key needed. Rate limit gently.
"""

import csv
import httpx
import io
from datetime import date
from typing import Optional

SAVANT_BASE = "https://baseballsavant.mlb.com"

# Park factors from Baseball Savant (2024 season, runs per game index, 100 = average)
# Values above 100 = hitter-friendly, below 100 = pitcher-friendly
PARK_FACTORS_2024 = {
    "Coors Field": 121,
    "Great American Ball Park": 108,
    "Yankee Stadium": 107,
    "Globe Life Field": 106,
    "American Family Field": 105,
    "Citizens Bank Park": 104,
    "Fenway Park": 103,
    "Busch Stadium": 102,
    "Chase Field": 101,
    "Truist Park": 101,
    "Guaranteed Rate Field": 100,
    "Angel Stadium": 99,
    "Target Field": 99,
    "Camden Yards": 99,
    "Kauffman Stadium": 98,
    "Progressive Field": 98,
    "Citi Field": 97,
    "PNC Park": 97,
    "Nationals Park": 97,
    "Rogers Centre": 96,
    "Comerica Park": 96,
    "Minute Maid Park": 95,
    "T-Mobile Park": 95,
    "Oracle Park": 93,
    "loanDepot park": 93,
    "Petco Park": 92,
    "Dodger Stadium": 91,
    "Tropicana Field": 90,
    "Oakland Coliseum": 89,
    "default": 100,
}


def get_park_factor(venue: str) -> int:
    return PARK_FACTORS_2024.get(venue, PARK_FACTORS_2024["default"])


async def get_xstats_batter(player_id: int, season: Optional[int] = None) -> dict:
    """
    Fetch xBA, xSLG, xwOBA, hard_hit_pct for a batter from Baseball Savant.
    Falls back to empty dict if data not available (e.g., early season).
    """
    season = season or date.today().year
    url = (
        f"{SAVANT_BASE}/leaderboard/custom"
        f"?year={season}&type=batter&filter=&sort=xba&sortDir=desc"
        f"&min=10&player_id={player_id}&csv=true"
    )
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        for row in reader:
            if str(row.get("player_id", "")) == str(player_id):
                return {
                    "xba": _safe_float(row.get("xba")),
                    "xslg": _safe_float(row.get("xslg")),
                    "xwoba": _safe_float(row.get("xwoba")),
                    "hard_hit_pct": _safe_float(row.get("hard_hit_percent")),
                    "barrel_pct": _safe_float(row.get("barrel_batted_rate")),
                    "k_pct": _safe_float(row.get("strikeout_percent")),
                    "bb_pct": _safe_float(row.get("walk_percent")),
                }
    except Exception:
        pass
    return {}


async def get_xstats_pitcher(player_id: int, season: Optional[int] = None) -> dict:
    """
    Fetch xERA, xFIP proxy, hard_hit_pct allowed for a pitcher from Baseball Savant.
    """
    season = season or date.today().year
    url = (
        f"{SAVANT_BASE}/leaderboard/custom"
        f"?year={season}&type=pitcher&filter=&sort=xera&sortDir=asc"
        f"&min=10&player_id={player_id}&csv=true"
    )
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        for row in reader:
            if str(row.get("player_id", "")) == str(player_id):
                return {
                    "xera": _safe_float(row.get("xera")),
                    "xba_against": _safe_float(row.get("xba")),
                    "hard_hit_pct_allowed": _safe_float(row.get("hard_hit_percent")),
                    "barrel_pct_allowed": _safe_float(row.get("barrel_batted_rate")),
                    "k_pct": _safe_float(row.get("strikeout_percent")),
                    "bb_pct": _safe_float(row.get("walk_percent")),
                    "gb_pct": _safe_float(row.get("gb_percent") or row.get("groundball_percent")),
                    "fb_pct": _safe_float(row.get("fb_percent") or row.get("flyball_percent")),
                    "contact_pct": _safe_float(row.get("contact_percent")),
                }
    except Exception:
        pass
    return {}


async def get_hit_park_factors(season: Optional[int] = None) -> dict[str, int]:
    """Fetch Baseball Savant's three-year rolling hit factor by venue."""
    season = season or date.today().year
    url = f"{SAVANT_BASE}/leaderboard/statcast-park-factors"
    params = {
        "type": "year",
        "year": season,
        "batSide": "",
        "stat": "index_wOBA",
        "condition": "All",
        "rolling": 3,
        "csv": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        result = {}
        for row in csv.DictReader(io.StringIO(resp.text)):
            venue = _first_value(row, "venue_name", "venue", "Venue", "name")
            hit_factor = _first_value(
                row,
                "index_H",
                "index_h",
                "H",
                "hits",
                "hit_factor",
            )
            if not venue:
                continue
            parsed = _safe_float(hit_factor, 0.0)
            if 70 <= parsed <= 130:
                result[venue] = int(round(parsed))
        return result
    except Exception:
        return {}


def _safe_float(val: Optional[str], default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _first_value(row: dict, *names: str):
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None
