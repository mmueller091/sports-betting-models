"""
NHL Stats API fetcher.
Uses the official NHL API (api-web.nhle.com) — free, no key required.
"""

import httpx
from datetime import date, timedelta
from typing import Optional

BASE = "https://api-web.nhle.com/v1"

# Team abbreviation → full name map (for odds matching)
TEAM_ABBREV = {
    "ANA": "Anaheim Ducks", "ARI": "Arizona Coyotes", "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres", "CGY": "Calgary Flames", "CAR": "Carolina Hurricanes",
    "CHI": "Chicago Blackhawks", "COL": "Colorado Avalanche", "CBJ": "Columbus Blue Jackets",
    "DAL": "Dallas Stars", "DET": "Detroit Red Wings", "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers", "LAK": "Los Angeles Kings", "MIN": "Minnesota Wild",
    "MTL": "Montreal Canadiens", "NSH": "Nashville Predators", "NJD": "New Jersey Devils",
    "NYI": "New York Islanders", "NYR": "New York Rangers", "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers", "PIT": "Pittsburgh Penguins", "SJS": "San Jose Sharks",
    "SEA": "Seattle Kraken", "STL": "St. Louis Blues", "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs", "UTA": "Utah Hockey Club", "VAN": "Vancouver Canucks",
    "VGK": "Vegas Golden Knights", "WSH": "Washington Capitals", "WPG": "Winnipeg Jets",
}

# Arenas for park factor / context
ARENA_MAP = {
    "Anaheim Ducks": "Honda Center",
    "Boston Bruins": "TD Garden",
    "Buffalo Sabres": "KeyBank Center",
    "Calgary Flames": "Scotiabank Saddledome",
    "Carolina Hurricanes": "PNC Arena",
    "Chicago Blackhawks": "United Center",
    "Colorado Avalanche": "Ball Arena",
    "Columbus Blue Jackets": "Nationwide Arena",
    "Dallas Stars": "American Airlines Center",
    "Detroit Red Wings": "Little Caesars Arena",
    "Edmonton Oilers": "Rogers Place",
    "Florida Panthers": "Amerant Bank Arena",
    "Los Angeles Kings": "Crypto.com Arena",
    "Minnesota Wild": "Xcel Energy Center",
    "Montreal Canadiens": "Bell Centre",
    "Nashville Predators": "Bridgestone Arena",
    "New Jersey Devils": "Prudential Center",
    "New York Islanders": "UBS Arena",
    "New York Rangers": "Madison Square Garden",
    "Ottawa Senators": "Canadian Tire Centre",
    "Philadelphia Flyers": "Wells Fargo Center",
    "Pittsburgh Penguins": "PPG Paints Arena",
    "San Jose Sharks": "SAP Center",
    "Seattle Kraken": "Climate Pledge Arena",
    "St. Louis Blues": "Enterprise Center",
    "Tampa Bay Lightning": "Amalie Arena",
    "Toronto Maple Leafs": "Scotiabank Arena",
    "Utah Hockey Club": "Delta Center",
    "Vancouver Canucks": "Rogers Arena",
    "Vegas Golden Knights": "T-Mobile Arena",
    "Washington Capitals": "Capital One Arena",
    "Winnipeg Jets": "Canada Life Centre",
}


async def get_todays_nhl_games(game_date: Optional[date] = None) -> list[dict]:
    """Return scheduled NHL games for the given date."""
    d = game_date or date.today()
    url = f"{BASE}/schedule/{d.strftime('%Y-%m-%d')}"
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    data = resp.json()
    games = []
    for week in data.get("gameWeek", []):
        for g in week.get("games", []):
            parsed = _parse_nhl_game(g)
            if parsed and _game_belongs_to_requested_date(g, parsed, d):
                games.append(parsed)
    return games


def _parse_nhl_game(g: dict) -> Optional[dict]:
    state = g.get("gameState", "")
    if state == "FINAL" or state == "OFF":
        return None
    away = g.get("awayTeam", {})
    home = g.get("homeTeam", {})
    away_name = TEAM_ABBREV.get(away.get("abbrev", ""), away.get("name", {}).get("default", "Unknown"))
    home_name = TEAM_ABBREV.get(home.get("abbrev", ""), home.get("name", {}).get("default", "Unknown"))
    return {
        "game_id": g.get("id"),
        "status": state,
        "game_date": g.get("gameDate"),
        "game_time": g.get("startTimeUTC"),
        "venue": g.get("venue", {}).get("default", ARENA_MAP.get(home_name, "Unknown")),
        "away_team": away_name,
        "away_team_abbrev": away.get("abbrev", ""),
        "away_team_id": away.get("id"),
        "home_team": home_name,
        "home_team_abbrev": home.get("abbrev", ""),
        "home_team_id": home.get("id"),
    }


def _game_belongs_to_requested_date(raw_game: dict, parsed_game: dict, requested_date: date) -> bool:
    """
    The NHL schedule endpoint can group U.S. evening games under the next UTC day.
    Prefer an explicit local-ish gameDate field when present, then fall back to
    the requested date for nearby UTC-midnight starts.
    """
    raw_game_date = raw_game.get("gameDate") or parsed_game.get("game_date")
    if raw_game_date:
        try:
            return date.fromisoformat(raw_game_date) == requested_date
        except ValueError:
            pass

    start_time = parsed_game.get("game_time")
    if not start_time:
        return False

    try:
        from datetime import datetime, timedelta

        dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        utc_date = dt.date()
        # U.S. evening puck drops often appear just after midnight UTC.
        return utc_date in {
            requested_date,
            requested_date + timedelta(days=1),
        }
    except Exception:
        return False


async def get_team_stats(team_abbrev: str, season: str = "20252026") -> dict:
    """Fetch team-level season stats (goals for/against, PP%, PK%, record)."""
    url = f"{BASE}/club-stats/{team_abbrev}/now"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        data = resp.json()
        return _parse_team_stats(data)
    except Exception:
        return {}


def _parse_team_stats(data: dict) -> dict:
    skater_stats = data.get("skaters", [])
    goalie_stats = data.get("goalies", [])
    # Team-level aggregate from skater totals would need standings
    # Return what we can from this endpoint
    return {
        "skater_count": len(skater_stats),
        "goalie_count": len(goalie_stats),
        "top_scorers": _top_scorers(skater_stats, 5),
        "starting_goalie": _likely_starter(goalie_stats),
    }


def _top_scorers(skaters: list, n: int) -> list[dict]:
    scored = []
    for s in skaters:
        scored.append({
            "id": s.get("playerId"),
            "name": f"{s.get('firstName', {}).get('default', '')} {s.get('lastName', {}).get('default', '')}".strip(),
            "position": s.get("positionCode", "F"),
            "goals": s.get("goals", 0),
            "assists": s.get("assists", 0),
            "points": s.get("points", 0),
            "shots": s.get("shots", 0),
            "games_played": s.get("gamesPlayed", 1),
            "goals_per_game": round(s.get("goals", 0) / max(s.get("gamesPlayed", 1), 1), 3),
            "shots_per_game": round(s.get("shots", 0) / max(s.get("gamesPlayed", 1), 1), 2),
            "pp_goals": s.get("powerPlayGoals", 0),
        })
    scored.sort(key=lambda x: x["goals"], reverse=True)
    return scored[:n]


def _likely_starter(goalies: list) -> dict:
    """Pick the goalie with the most games played as likely starter."""
    if not goalies:
        return {}
    starter = max(goalies, key=lambda g: g.get("gamesStarted", 0))
    gp = max(starter.get("gamesPlayed", 1), 1)
    return {
        "id": starter.get("playerId"),
        "name": f"{starter.get('firstName', {}).get('default', '')} {starter.get('lastName', {}).get('default', '')}".strip(),
        "games_started": starter.get("gamesStarted", 0),
        "save_pct": starter.get("savePctg", 0.900),
        "gaa": starter.get("goalsAgainstAvg", 3.00),
        "wins": starter.get("wins", 0),
        "losses": starter.get("losses", 0),
        "shutouts": starter.get("shutouts", 0),
    }


async def get_player_stats(player_id: int) -> dict:
    """Fetch season stats for a player (skater or goalie)."""
    url = f"{BASE}/player/{player_id}/landing"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        data = resp.json()
        return _parse_player_landing(data)
    except Exception:
        return {}


def _parse_player_landing(data: dict) -> dict:
    featured = data.get("featuredStats", {})
    season = featured.get("regularSeason", {}).get("subSeason", {})
    career = data.get("careerTotals", {}).get("regularSeason", {})
    pos = data.get("position", "F")
    gp = max(season.get("gamesPlayed", 1), 1)
    return {
        "id": data.get("playerId"),
        "name": f"{data.get('firstName', {}).get('default', '')} {data.get('lastName', {}).get('default', '')}".strip(),
        "position": pos,
        "shoots": data.get("shootsCatches", "R"),
        "season_goals": season.get("goals", 0),
        "season_assists": season.get("assists", 0),
        "season_points": season.get("points", 0),
        "season_shots": season.get("shots", 0),
        "season_gp": gp,
        "goals_per_game": round(season.get("goals", 0) / gp, 3),
        "shots_per_game": round(season.get("shots", 0) / gp, 2),
        "pp_goals": season.get("powerPlayGoals", 0),
        "pp_points": season.get("powerPlayPoints", 0),
        "shooting_pct": season.get("shootingPctg", 0.0),
        # Goalie
        "save_pct": season.get("savePctg", None),
        "gaa": season.get("goalsAgainstAvg", None),
    }


async def get_team_schedule_last_n(team_abbrev: str, n: int = 5) -> list[dict]:
    """
    Fetch the last N completed games for a team to assess recent form.
    Returns list of {date, win, goals_for, goals_against}.
    """
    try:
        data = await _get_club_schedule(team_abbrev)
        games = data.get("games", [])
        completed = [g for g in games if g.get("gameState") in ("FINAL", "OFF")]
        recent = completed[-n:] if len(completed) >= n else completed
        result = []
        for g in recent:
            away = g.get("awayTeam", {})
            home = g.get("homeTeam", {})
            team_is_home = home.get("abbrev") == team_abbrev
            team_score = home.get("score", 0) if team_is_home else away.get("score", 0)
            opp_score = away.get("score", 0) if team_is_home else home.get("score", 0)
            result.append({
                "date": g.get("gameDate"),
                "win": team_score > opp_score,
                "goals_for": team_score,
                "goals_against": opp_score,
            })
        return result
    except Exception:
        return []


async def get_schedule_spot_metrics(team_abbrev: str, game_date: Optional[date] = None) -> dict:
    """Return rest/schedule density metrics for the upcoming game."""
    target_date = game_date or date.today()
    try:
        data = await _get_club_schedule(team_abbrev)
        games = data.get("games", [])
    except Exception:
        return {
            "rest_days": 1,
            "games_last_4": 0,
            "games_last_6": 0,
            "road_trip_len": 0,
        }

    completed = [
        g for g in games
        if g.get("gameState") in ("FINAL", "OFF")
        and date.fromisoformat(g.get("gameDate")) < target_date
    ]
    completed.sort(key=lambda g: g.get("gameDate", ""))

    if not completed:
        return {
            "rest_days": 1,
            "games_last_4": 0,
            "games_last_6": 0,
            "road_trip_len": 0,
        }

    last_game_date = date.fromisoformat(completed[-1]["gameDate"])
    road_trip_len = 0
    for game in reversed(completed):
        if game.get("homeTeam", {}).get("abbrev") == team_abbrev:
            break
        road_trip_len += 1

    return {
        "rest_days": max((target_date - last_game_date).days - 1, 0),
        "games_last_4": _count_games_in_window(completed, target_date, 4),
        "games_last_6": _count_games_in_window(completed, target_date, 6),
        "road_trip_len": road_trip_len,
    }


async def check_back_to_back(team_abbrev: str, game_date: Optional[date] = None) -> bool:
    """Check if a team played yesterday (back-to-back situation)."""
    yesterday = (game_date or date.today()) - timedelta(days=1)
    url = f"{BASE}/schedule/{yesterday.strftime('%Y-%m-%d')}"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        data = resp.json()
        for week in data.get("gameWeek", []):
            for g in week.get("games", []):
                if g.get("gameState") in ("FINAL", "OFF"):
                    if g.get("awayTeam", {}).get("abbrev") == team_abbrev:
                        return True
                    if g.get("homeTeam", {}).get("abbrev") == team_abbrev:
                        return True
    except Exception:
        pass
    return False


async def get_standings() -> dict:
    """Fetch current NHL standings for team win % context."""
    url = f"{BASE}/standings/now"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        data = resp.json()
        standings = {}
        for team in data.get("standings", []):
            abbrev = team.get("teamAbbrev", {}).get("default", "")
            gp = max(team.get("gamesPlayed", 1), 1)
            standings[abbrev] = {
                "points": team.get("points", 0),
                "wins": team.get("wins", 0),
                "losses": team.get("losses", 0),
                "ot_losses": team.get("otLosses", 0),
                "games_played": gp,
                "win_pct": round(team.get("wins", 0) / gp, 3),
                "goals_for_per_game": round(team.get("goalFor", 0) / gp, 2),
                "goals_against_per_game": round(team.get("goalAgainst", 0) / gp, 2),
                "pp_pct": team.get("powerPlayPct", 0.0),
                "pk_pct": team.get("penaltyKillPct", 0.0),
                "home_wins": team.get("homeWins", 0),
                "home_losses": team.get("homeLosses", 0),
                "road_wins": team.get("roadWins", 0),
                "road_losses": team.get("roadLosses", 0),
                "l10_wins": team.get("l10Wins", 0),
            }
        return standings
    except Exception:
        return {}


async def _get_club_schedule(team_abbrev: str) -> dict:
    url = f"{BASE}/club-schedule-season/{team_abbrev}/now"
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    return resp.json()


def _count_games_in_window(games: list[dict], target_date: date, days: int) -> int:
    return sum(
        1
        for game in games
        if 0 < (target_date - date.fromisoformat(game.get("gameDate"))).days <= days
    )
