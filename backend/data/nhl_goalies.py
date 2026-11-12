"""
NHL confirmed starting goalie scraper.
Sources (tried in order):
  1. Daily Faceoff (dailyfaceoff.com/starting-goalies/) — most reliable, posts 2-3h before puck drop
  2. NHL API roster + recent game pattern fallback

This is the single highest-value data point for NHL betting.
Confirmed starter vs "probable" can flip a bet's edge entirely.
"""

import httpx
import re
import unicodedata
from typing import Optional
from datetime import date
from data.source_health import warn

# Daily Faceoff headers to avoid bot detection
DF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.dailyfaceoff.com/",
}

# Team name aliases for matching Daily Faceoff names to our team names
DF_TEAM_ALIASES = {
    "Ducks": "Anaheim Ducks", "Bruins": "Boston Bruins", "Sabres": "Buffalo Sabres",
    "Flames": "Calgary Flames", "Hurricanes": "Carolina Hurricanes",
    "Blackhawks": "Chicago Blackhawks", "Avalanche": "Colorado Avalanche",
    "Blue Jackets": "Columbus Blue Jackets", "Stars": "Dallas Stars",
    "Red Wings": "Detroit Red Wings", "Oilers": "Edmonton Oilers",
    "Panthers": "Florida Panthers", "Kings": "Los Angeles Kings",
    "Wild": "Minnesota Wild", "Canadiens": "Montreal Canadiens",
    "Predators": "Nashville Predators", "Devils": "New Jersey Devils",
    "Islanders": "New York Islanders", "Rangers": "New York Rangers",
    "Senators": "Ottawa Senators", "Flyers": "Philadelphia Flyers",
    "Penguins": "Pittsburgh Penguins", "Sharks": "San Jose Sharks",
    "Kraken": "Seattle Kraken", "Blues": "St. Louis Blues",
    "Lightning": "Tampa Bay Lightning", "Maple Leafs": "Toronto Maple Leafs",
    "Hockey Club": "Utah Hockey Club", "Canucks": "Vancouver Canucks",
    "Golden Knights": "Vegas Golden Knights", "Capitals": "Washington Capitals",
    "Jets": "Winnipeg Jets",
}


async def get_confirmed_goalies(game_date: Optional[date] = None) -> dict[str, dict]:
    """
    Scrape Daily Faceoff for confirmed starting goalies.
    Returns {team_name: {name, confirmed, source}}
    """
    goalies = {}
    if game_date is not None and game_date != date.today():
        warn("Historical or future starting-goalie confirmation unavailable from today's feed")
        return goalies

    # Try Daily Faceoff first
    try:
        goalies = await _scrape_daily_faceoff()
        if goalies:
            return goalies
    except Exception:
        pass

    # Fallback: try ESPN NHL headlines for goalie news
    try:
        goalies = await _espn_goalie_fallback()
        if goalies:
            return goalies
    except Exception:
        pass

    if not goalies:
        warn("NHL starting-goalie confirmations unavailable; likely starters remain unconfirmed")
    return goalies


async def _scrape_daily_faceoff() -> dict[str, dict]:
    """Scrape dailyfaceoff.com/starting-goalies/ for confirmed starters."""
    url = "https://www.dailyfaceoff.com/starting-goalies/"
    async with httpx.AsyncClient(
        timeout=15, headers=DF_HEADERS, follow_redirects=True
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    html = resp.text
    goalies = {}

    # Parse goalie cards — Daily Faceoff uses consistent class structure
    # Pattern: find team name + goalie name + confirmed/expected status
    # We use regex on the raw HTML since we're avoiding beautifulsoup dependency

    # Match blocks like: team name → goalie name → confirmed/expected/probable
    # Daily Faceoff structure: each matchup has two goalie sections
    team_pattern = re.compile(
        r'class="[^"]*team-name[^"]*"[^>]*>\s*([^<]+)\s*</[^>]+>',
        re.IGNORECASE
    )
    goalie_pattern = re.compile(
        r'class="[^"]*goalie-name[^"]*"[^>]*>\s*([^<]+)\s*</[^>]+>',
        re.IGNORECASE
    )
    status_pattern = re.compile(
        r'class="[^"]*goalie-status[^"]*"[^>]*>\s*([^<]+)\s*</[^>]+>',
        re.IGNORECASE
    )

    teams = team_pattern.findall(html)
    goalie_names = goalie_pattern.findall(html)
    statuses = status_pattern.findall(html)

    if not teams or len(teams) != len(goalie_names) or len(statuses) != len(teams):
        warn("Starting-goalie page did not provide aligned team, player and status records")
        return {}

    for i, (team_raw, goalie_raw) in enumerate(zip(teams, goalie_names)):
        team_raw = team_raw.strip()
        goalie_raw = goalie_raw.strip()
        status_raw = statuses[i].strip() if i < len(statuses) else "Expected"

        team_full = _resolve_team(team_raw)
        if team_full and goalie_raw and goalie_raw.lower() not in ("tbd", "tba", "unknown"):
            confirmed = status_raw.lower().strip() == "confirmed"
            goalies[team_full] = {
                "name": goalie_raw,
                "confirmed": confirmed,
                "status": status_raw,
                "source": "dailyfaceoff",
            }

    return goalies


async def _espn_goalie_fallback() -> dict[str, dict]:
    """
    Fallback: check ESPN NHL news for goalie announcements.
    Very rough — only catches major announcements.
    """
    url = "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/news"
    params = {"limit": 50}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    data = resp.json()

    goalies = {}
    goalie_words = ["start", "starts", "starting", "will start", "confirmed"]

    for article in data.get("articles", []):
        headline = article.get("headline", "").lower()
        description = article.get("description", "").lower()
        text = headline + " " + description

        if "goalie" in text or "goaltender" in text or "between the pipes" in text:
            if any(w in text for w in goalie_words):
                # Try to extract team from categories
                for cat in article.get("categories", []):
                    if cat.get("type") == "team":
                        team_name = cat.get("description", "")
                        # Can't reliably extract goalie name from headline without NLP
                        # Mark as "check manually"
                        goalies[team_name] = {
                            "name": "See news",
                            "confirmed": False,
                            "status": headline[:80],
                            "source": "espn_news",
                        }
    return goalies


def _resolve_team(raw: str) -> Optional[str]:
    """Map a raw team name fragment to our canonical team name."""
    raw = raw.strip()
    # Direct alias lookup
    for alias, full in DF_TEAM_ALIASES.items():
        if alias.lower() in raw.lower():
            return full
    return None


def apply_confirmed_goalies(games: list[dict], confirmed: dict[str, dict]) -> list[dict]:
    """
    Override the 'likely starter' goalie in enriched game data
    with confirmed starters from Daily Faceoff.
    """
    for game in games:
        away = game.get("away_team", "")
        home = game.get("home_team", "")

        for team_key, side in [(away, "away"), (home, "home")]:
            if team_key in confirmed:
                c = confirmed[team_key]
                if c.get("name", "").lower() in ("", "see news", "tbd", "tba", "unknown"):
                    warn(f"Starting-goalie news for {team_key} does not identify a player")
                    continue
                stats_key = f"{side}_team_stats"
                if stats_key not in game:
                    game[stats_key] = {}
                current_goalie = game[stats_key].get("starting_goalie", {})
                available = game[stats_key].get("goalies", []) + [current_goalie]
                matched = next((goalie for goalie in available
                                if _player_name(goalie.get("name", "")) == _player_name(c["name"])), None)
                if matched is None:
                    warn(f"Statistics unavailable for announced goalie {c['name']}; prior starter statistics discarded")
                    matched = {"id": None, "save_pct": None, "gaa": None, "games_started": None}
                game[stats_key]["starting_goalie"] = {
                    **matched,
                    "name": c["name"],
                    "confirmed": c["confirmed"],
                    "status": c.get("status", "Expected"),
                    "source": c.get("source", "dailyfaceoff"),
                }
    return games


def _player_name(name: str) -> str:
    return "".join(character for character in unicodedata.normalize("NFKD", name)
                   if not unicodedata.combining(character)).casefold().strip()
