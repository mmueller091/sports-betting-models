"""
Natural Stat Trick (naturalstattrick.com) NHL data scraper.
Provides 5v5 Corsi%, Fenwick%, xGF% — the most predictive NHL team metrics.

These numbers are more stable than raw goals and regress to mean faster,
making them better for betting model inputs than standings-based win%.

Rate limit: scrape at most once per day.
"""

import httpx
import re
from typing import Optional

NST_BASE = "https://www.naturalstattrick.com"
NST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.naturalstattrick.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# NST team names → our canonical names
NST_TEAM_MAP = {
    "Anaheim Ducks": "Anaheim Ducks", "Boston Bruins": "Boston Bruins",
    "Buffalo Sabres": "Buffalo Sabres", "Calgary Flames": "Calgary Flames",
    "Carolina Hurricanes": "Carolina Hurricanes", "Chicago Blackhawks": "Chicago Blackhawks",
    "Colorado Avalanche": "Colorado Avalanche", "Columbus Blue Jackets": "Columbus Blue Jackets",
    "Dallas Stars": "Dallas Stars", "Detroit Red Wings": "Detroit Red Wings",
    "Edmonton Oilers": "Edmonton Oilers", "Florida Panthers": "Florida Panthers",
    "Los Angeles Kings": "Los Angeles Kings", "Minnesota Wild": "Minnesota Wild",
    "Montréal Canadiens": "Montreal Canadiens", "Montreal Canadiens": "Montreal Canadiens",
    "Nashville Predators": "Nashville Predators", "New Jersey Devils": "New Jersey Devils",
    "New York Islanders": "New York Islanders", "New York Rangers": "New York Rangers",
    "Ottawa Senators": "Ottawa Senators", "Philadelphia Flyers": "Philadelphia Flyers",
    "Pittsburgh Penguins": "Pittsburgh Penguins", "San Jose Sharks": "San Jose Sharks",
    "Seattle Kraken": "Seattle Kraken", "St. Louis Blues": "St. Louis Blues",
    "Tampa Bay Lightning": "Tampa Bay Lightning", "Toronto Maple Leafs": "Toronto Maple Leafs",
    "Utah Hockey Club": "Utah Hockey Club", "Vancouver Canucks": "Vancouver Canucks",
    "Vegas Golden Knights": "Vegas Golden Knights", "Washington Capitals": "Washington Capitals",
    "Winnipeg Jets": "Winnipeg Jets",
}


async def get_team_corsi_xg(season_start: int = 2024, situation: str = "5on5") -> dict[str, dict]:
    """
    Fetch team Corsi, Fenwick, and xGF% from Natural Stat Trick team table.
    situation: '5on5', 'all', 'pk', 'pp'

    5-on-5 is most predictive (removes PP/PK quality effects).
    Returns {team_name: {cf_pct, ff_pct, xgf_pct, hdcf_pct, ...}}
    """
    season_str = f"{season_start}{season_start + 1}"
    url = f"{NST_BASE}/teamtable.php"
    params = {
        "sit": situation,
        "score": "all",
        "st": "S",
        "l": "50",       # Last 50 games
        "seasonid": season_str,
        "fromseason": season_str,
        "thruseason": season_str,
    }

    try:
        async with httpx.AsyncClient(
            timeout=20, headers=NST_HEADERS, follow_redirects=True
        ) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        return _parse_nst_table(resp.text)
    except Exception:
        return {}


def _parse_nst_table(html: str) -> dict[str, dict]:
    """
    Parse the NST team table HTML into a dict.
    The table has columns: Team, GP, TOI, CF, CA, CF%, FF, FA, FF%,
    xGF, xGA, xGF%, SCF, SCA, HDCF, HDCA, HDCF%, ...
    """
    result = {}

    # Find table rows
    row_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
    cell_pattern = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)
    tag_strip = re.compile(r"<[^>]+>")

    rows = row_pattern.findall(html)
    headers = []

    for row in rows:
        cells = cell_pattern.findall(row)
        clean = [tag_strip.sub("", c).strip() for c in cells]

        if not headers:
            if any("CF%" in c or "xGF%" in c for c in clean):
                headers = clean
            continue

        if len(clean) < 5:
            continue

        try:
            team_raw = clean[0]
            team_name = NST_TEAM_MAP.get(team_raw, team_raw)

            row_data = {}
            for h, v in zip(headers, clean):
                row_data[h] = v

            result[team_name] = {
                "cf_pct": _pct(row_data.get("CF%")),
                "ff_pct": _pct(row_data.get("FF%")),
                "xgf_pct": _pct(row_data.get("xGF%")),
                "hdcf_pct": _pct(row_data.get("HDCF%")),
                "xgf_raw": _f(row_data.get("xGF")),
                "xga_raw": _f(row_data.get("xGA")),
                "hdcf_raw": _f(row_data.get("HDCF")),
                "hdca_raw": _f(row_data.get("HDCA")),
                "situation": "5v5",
            }
        except Exception:
            continue

    return result


def apply_nst_to_games(games: list[dict], nst_data: dict[str, dict]) -> list[dict]:
    """Attach NST xG data to each game."""
    for game in games:
        for side in ("away", "home"):
            team = game.get(f"{side}_team", "")
            nst = nst_data.get(team)
            if nst:
                stats = game.get(f"{side}_team_stats", {})
                stats["nst"] = nst
                game[f"{side}_team_stats"] = stats
    return games


def _pct(val: Optional[str]) -> float:
    try:
        return round(float(val.replace("%", "")), 2)
    except Exception:
        return 50.0


def _f(val: Optional[str]) -> float:
    try:
        return float(val)
    except Exception:
        return 0.0
