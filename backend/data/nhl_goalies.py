"""
NHL confirmed starting goalie scraper.
Sources (tried in order):
  1. Daily Faceoff (dailyfaceoff.com/starting-goalies/) — most reliable, posts 2-3h before puck drop
  2. NHL API roster + recent game pattern fallback

This is the single highest-value data point for NHL betting.
Confirmed starter vs "probable" can flip a bet's edge entirely.
"""

import httpx
import json
import unicodedata
from html.parser import HTMLParser
from typing import Optional
from datetime import date, datetime
from zoneinfo import ZoneInfo
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
    "Mammoth": "Utah Mammoth",
    "Golden Knights": "Vegas Golden Knights", "Capitals": "Washington Capitals",
    "Jets": "Winnipeg Jets",
}


async def get_confirmed_goalies(game_date: Optional[date] = None) -> dict[str, dict]:
    """
    Scrape Daily Faceoff for confirmed starting goalies.
    Returns {team_name: {name, confirmed, source}}
    """
    goalies = {}
    today = datetime.now(ZoneInfo("America/New_York")).date()
    target_date = game_date or today
    if target_date != today:
        warn("Historical or future starting-goalie confirmation unavailable from today's feed")
        return goalies

    # Try Daily Faceoff first
    try:
        goalies = await _scrape_daily_faceoff(target_date)
        if goalies:
            return goalies
    except httpx.HTTPStatusError as error:
        warn(f"DailyFaceoff starting-goalie feed returned HTTP {error.response.status_code}")
    except httpx.TimeoutException:
        warn("DailyFaceoff starting-goalie feed timed out")
    except httpx.RequestError as error:
        warn(f"DailyFaceoff starting-goalie connection unavailable ({type(error).__name__})")
    except Exception as error:
        warn(f"DailyFaceoff starting-goalie processing unavailable ({type(error).__name__})")

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


async def _scrape_daily_faceoff(game_date: Optional[date] = None) -> dict[str, dict]:
    """Scrape dailyfaceoff.com/starting-goalies/ for confirmed starters."""
    url = "https://www.dailyfaceoff.com/starting-goalies/"
    async with httpx.AsyncClient(
        timeout=15, headers=DF_HEADERS, follow_redirects=True
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    target_date = game_date or datetime.now(ZoneInfo("America/New_York")).date()
    return _parse_daily_faceoff_html(resp.text, target_date)


class _DailyFaceoffJSON(HTMLParser):
    """Read the observed Next.js payload without executing page scripts."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.payloads = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and attributes.get("id") == "__NEXT_DATA__":
            self.current = []

    def handle_data(self, value):
        if self.current is not None:
            self.current.append(value)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.payloads.append("".join(self.current))
            self.current = None


def _parse_daily_faceoff_html(html: str, game_date: date) -> dict[str, dict]:
    """Bind each goalie to its own team/status in an observed matchup object."""
    parser = _DailyFaceoffJSON()
    try:
        parser.feed(html)
        parser.close()
        if len(parser.payloads) != 1:
            raise ValueError("Missing or ambiguous Next.js payload")
        props = json.loads(parser.payloads[0])["props"]["pageProps"]
        rows = props["data"]
        if not isinstance(rows, list):
            raise ValueError("Missing matchup records")
    except (ValueError, KeyError, TypeError):
        warn("Starting-goalie page did not provide valid structured matchup records")
        return {}
    target = game_date.isoformat()
    if props.get("date") != target:
        warn("Starting-goalie page date does not match the requested game date")
        return {}
    goalies, conflicting = {}, set()
    for row in rows:
        if not isinstance(row, dict) or row.get("date") != target:
            warn("Starting-goalie matchup date does not match the requested game date")
            continue
        try:
            kickoff = datetime.fromisoformat(str(row["dateGmt"]).replace("Z", "+00:00"))
            if kickoff.tzinfo is None or kickoff.astimezone(ZoneInfo("America/New_York")).date() != game_date:
                raise ValueError("Kickoff date mismatch")
        except (KeyError, TypeError, ValueError):
            warn("Starting-goalie matchup has an invalid scheduled game time")
            continue
        for side in ("home", "away"):
            team_raw, name = row.get(side + "TeamName"), row.get(side + "GoalieName")
            status = row.get(side + "NewsStrengthName")
            if not isinstance(team_raw, str) or not isinstance(name, str) or not name.strip() or name.strip().lower() in ("tbd", "tba", "unknown"):
                warn("Starting-goalie matchup has a missing team or player identity")
                continue
            team = _resolve_team(team_raw)
            if not team or (status is not None and not isinstance(status, str)):
                warn("Starting-goalie matchup has an unrecognized team or status")
                continue
            status = status.strip() if status else "Unconfirmed"
            record = {"name": name.strip(), "confirmed": status.casefold() == "confirmed", "status": status,
                      "source": "dailyfaceoff", "game_date": target, "game_time": row["dateGmt"],
                      "news_updated_at": row.get(side + "NewsCreatedAt"),
                      "news_source_url": row.get(side + "NewsSourceUrl"),
                      "news_source_name": row.get(side + "NewsSourceName"),
                      "dailyfaceoff_goalie_id": row.get(side + "GoalieId"),
                      "dailyfaceoff_team_id": row.get(side + "TeamId")}
            if team in conflicting:
                continue
            if team in goalies and goalies[team] != record:
                warn("Starting-goalie page contains conflicting records for the same team")
                goalies.pop(team)
                conflicting.add(team)
                continue
            goalies[team] = record
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
                    **{key: c[key] for key in ("game_date", "game_time", "news_updated_at", "news_source_url",
                                               "news_source_name", "dailyfaceoff_goalie_id", "dailyfaceoff_team_id")
                       if key in c},
                }
    return games


def _player_name(name: str) -> str:
    return "".join(character for character in unicodedata.normalize("NFKD", name)
                   if not unicodedata.combining(character)).casefold().strip()
