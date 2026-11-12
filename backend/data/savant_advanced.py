"""
Advanced Baseball Savant data fetcher.
Fetches team defensive OAA (Outs Above Average) — key for hit probability.
Teams with high OAA defense convert more balls in play to outs = fewer hits.
No API key. Rate limit gently.
"""

import httpx
import io
import csv

SAVANT_BASE = "https://baseballsavant.mlb.com"

# Map Savant 3-letter abbrevs -> full team names (as returned by MLB Stats API)
_TEAM_NAME_MAP = {
    "AZ": "Arizona Diamondbacks",
    "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs",
    "CWS": "Chicago White Sox",
    "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",
    "DET": "Detroit Tigers",
    "HOU": "Houston Astros",
    "KC": "Kansas City Royals",
    "LAA": "Los Angeles Angels",
    "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",
    "NYM": "New York Mets",
    "NYY": "New York Yankees",
    "OAK": "Oakland Athletics",
    "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates",
    "SD": "San Diego Padres",
    "SF": "San Francisco Giants",
    "SEA": "Seattle Mariners",
    "STL": "St. Louis Cardinals",
    "TB": "Tampa Bay Rays",
    "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",
    "WSH": "Washington Nationals",
    "ATH": "Athletics",  # Relocated
}

# Last word of team name -> full name (for fuzzy matching)
_LAST_WORD_MAP = {v.split()[-1]: v for v in _TEAM_NAME_MAP.values()}

_oaa_cache: dict = {}  # date_str -> {team_name: oaa}


async def get_team_defense_oaa(season: int = 2026) -> dict:
    """
    Fetch team Outs Above Average from Baseball Savant.
    Returns dict of full_team_name -> oaa_per_game (normalized).
    Positive OAA = elite defense (suppresses hits). Negative = poor defense (allows more hits).
    """
    cache_key = str(season)
    if cache_key in _oaa_cache:
        return _oaa_cache[cache_key]

    url = (
        f"{SAVANT_BASE}/leaderboard/outs_above_average"
        f"?type=Fielding_Team&startYear={season}&endYear={season}"
        f"&split=yes&team=&range=year&min=0&pos=all&roles=&viz=show&csv=true"
    )
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        reader = csv.DictReader(io.StringIO(resp.text))
        result = {}
        for row in reader:
            # Try to find team abbrev column
            abbrev = (
                row.get("team_abbrev") or row.get("team_id") or
                row.get("Team") or row.get("team") or ""
            ).strip().upper()
            oaa_raw = (
                row.get("outs_above_average") or row.get("oaa") or
                row.get("OAA") or "0"
            )
            games = int(row.get("g", row.get("games", 100)) or 100)
            oaa = _safe_float(oaa_raw)
            oaa_per_game = round(oaa / max(games, 1), 3)

            full_name = _TEAM_NAME_MAP.get(abbrev)
            if not full_name:
                # Try partial match
                for k, v in _TEAM_NAME_MAP.items():
                    if abbrev in k or k in abbrev:
                        full_name = v
                        break
            if full_name:
                result[full_name] = oaa_per_game

        _oaa_cache[cache_key] = result
        return result
    except Exception:
        return {}


def apply_oaa_to_games(games: list[dict], oaa: dict) -> list[dict]:
    """
    Attach the opposing team's OAA to each game.
    Away batters face home defense; home batters face away defense.
    """
    for game in games:
        away_team = game.get("away_team", "")
        home_team = game.get("home_team", "")
        # Fuzzy lookup by last word of team name
        game["away_defense_oaa"] = _lookup_oaa(away_team, oaa)
        game["home_defense_oaa"] = _lookup_oaa(home_team, oaa)
    return games


def _lookup_oaa(team_name: str, oaa: dict) -> float:
    if team_name in oaa:
        return oaa[team_name]
    # Try last word
    last = team_name.split()[-1] if team_name else ""
    for k, v in oaa.items():
        if k.split()[-1] == last:
            return v
    return 0.0


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default
