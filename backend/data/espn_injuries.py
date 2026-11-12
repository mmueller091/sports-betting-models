"""
ESPN Injury Report fetcher.
Uses ESPN's undocumented but stable public API — no key required.
Returns injury status for all players: Out, Questionable, Day-To-Day, Doubtful.

Impact: key player injuries dramatically shift expected output.
A top-line NHL center being out = -0.25 to -0.40 expected goals.
An MLB cleanup hitter being out = significant lineup downgrade.
"""

import httpx
from typing import Optional

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# Status severity: higher = more impactful (more likely to miss game)
INJURY_SEVERITY = {
    "Out": 1.0,
    "Doubtful": 0.75,
    "Questionable": 0.40,
    "Day-To-Day": 0.25,
    "Probable": 0.10,
    "IR": 1.0,
    "IR-NR": 1.0,
    "Suspended": 1.0,
}


async def get_nhl_injuries() -> dict[str, list[dict]]:
    """
    Fetch current NHL injury report from ESPN.
    Returns {team_name: [{player, status, detail, severity}]}
    """
    return await _fetch_injuries("hockey", "nhl")


async def get_mlb_injuries() -> dict[str, list[dict]]:
    """
    Fetch current MLB injury report from ESPN.
    Returns {team_name: [{player, position, status, detail, severity}]}
    """
    return await _fetch_injuries("baseball", "mlb")


async def _fetch_injuries(sport: str, league: str) -> dict[str, list[dict]]:
    url = f"{ESPN_BASE}/{sport}/{league}/injuries"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    result = {}
    for team_entry in data.get("injuries", []):
        team_name = (
            team_entry.get("team", {}).get("displayName")
            or team_entry.get("team", {}).get("name", "Unknown")
        )
        players = []
        for injury in team_entry.get("injuries", []):
            athlete = injury.get("athlete", {})
            status = injury.get("status", "Questionable")
            severity = INJURY_SEVERITY.get(status, 0.25)
            players.append({
                "player_id": athlete.get("id"),
                "name": athlete.get("displayName", "Unknown"),
                "position": athlete.get("position", {}).get("abbreviation", ""),
                "status": status,
                "detail": injury.get("details", {}).get("detail", ""),
                "type": injury.get("details", {}).get("type", ""),
                "severity": severity,
            })
        if players:
            result[team_name] = players
    return result


def get_key_injuries(injuries: dict[str, list[dict]], team: str) -> list[dict]:
    """Return only significant injuries (Out or Doubtful) for a team."""
    team_injuries = injuries.get(team, [])
    return [i for i in team_injuries if i["severity"] >= 0.75]


def injury_impact_nhl(injuries: dict[str, list[dict]], team: str) -> dict:
    """
    Estimate the goal-scoring impact of injuries for an NHL team.
    Returns {total_xg_lost, key_players_out, notes}
    """
    key = get_key_injuries(injuries, team)
    if not key:
        return {"total_xg_lost": 0.0, "key_players_out": [], "notes": []}

    xg_lost = 0.0
    notes = []
    for p in key:
        pos = p.get("position", "")
        # Estimate xG lost by position (rough averages for impactful players)
        if pos in ("C", "LW", "RW"):
            xg_lost += 0.20   # Top-6 forward assumption
            notes.append(f"{p['name']} ({pos}) {p['status']}")
        elif pos == "D":
            xg_lost += 0.08   # Defensemen score less
            notes.append(f"{p['name']} (D) {p['status']}")
        elif pos == "G":
            xg_lost += 0.35   # Backup goalie facing starter
            notes.append(f"{p['name']} (G) {p['status']} — backup likely")

    return {
        "total_xg_lost": round(xg_lost, 2),
        "key_players_out": key,
        "notes": notes,
    }


def injury_impact_mlb(injuries: dict[str, list[dict]], team: str) -> dict:
    """
    Estimate batting average / run impact of MLB lineup injuries.
    Returns {run_impact, key_players_out, notes}
    """
    key = get_key_injuries(injuries, team)
    if not key:
        return {"run_impact": 0.0, "key_players_out": [], "notes": []}

    run_impact = 0.0
    notes = []
    for p in key:
        pos = p.get("position", "")
        if pos in ("SP",):
            run_impact -= 0.3   # Starting pitcher out = use worse SP
            notes.append(f"{p['name']} (SP) {p['status']}")
        elif pos in ("1B", "2B", "3B", "SS", "OF", "DH", "C"):
            run_impact -= 0.10  # Position player out
            notes.append(f"{p['name']} ({pos}) {p['status']}")
        elif pos in ("RP", "CL"):
            run_impact -= 0.05  # Reliever out
            notes.append(f"{p['name']} ({pos}) {p['status']}")

    return {
        "run_impact": round(run_impact, 2),
        "key_players_out": key,
        "notes": notes,
    }


def apply_injuries_to_nhl_games(games: list[dict], injuries: dict[str, list[dict]]) -> list[dict]:
    """Tag each game with injury data for both teams."""
    for game in games:
        for side in ("away", "home"):
            team = game.get(f"{side}_team", "")
            impact = injury_impact_nhl(injuries, team)
            game[f"{side}_injury_impact"] = impact
    return games


def apply_injuries_to_mlb_games(games: list[dict], injuries: dict[str, list[dict]]) -> list[dict]:
    """Tag each game with injury data for both teams."""
    for game in games:
        for side in ("away", "home"):
            team = game.get(f"{side}_team", "")
            impact = injury_impact_mlb(injuries, team)
            game[f"{side}_injury_impact"] = impact
    return games
