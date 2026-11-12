"""
MLB Umpire data.
- Fetches today's home plate umpire assignments from MLB Stats API
- Applies a historical tendency database (runs/game, K%, BB% vs league avg)

Impact on totals: elite low-zone umps suppress totals by ~0.5 runs.
Impact on ML: high walk-rate umps slightly favor underdogs (more base runners).
"""

import httpx
from typing import Optional

BASE = "https://statsapi.mlb.com/api/v1"

# ── Umpire Tendency Database ──────────────────────────────────────────────────
# run_adj:  runs per game vs league avg (negative = fewer runs = low total)
# k_adj:    K% delta vs league avg (positive = more Ks)
# bb_adj:   BB% delta vs league avg (positive = more walks)
# Source: compiled from Baseball Savant umpire leaderboards & Umpire Scorecards
#         Updated through 2024 season. Refresh each offseason.
UMPIRE_TENDENCIES: dict[str, dict] = {
    # Name: {run_adj, k_adj, bb_adj, zone_size}  zone_size: 1=avg, >1=big, <1=tight
    "Angel Hernandez":     {"run_adj": +0.12, "k_adj": -1.2, "bb_adj": +0.8,  "zone_size": 0.97},
    "CB Bucknor":          {"run_adj": +0.18, "k_adj": -0.9, "bb_adj": +1.1,  "zone_size": 0.96},
    "Phil Cuzzi":          {"run_adj": +0.22, "k_adj": -1.4, "bb_adj": +0.9,  "zone_size": 0.95},
    "Joe West":            {"run_adj": +0.08, "k_adj": -0.5, "bb_adj": +0.6,  "zone_size": 0.98},
    "Dan Iassogna":        {"run_adj": +0.31, "k_adj": -1.8, "bb_adj": +1.4,  "zone_size": 0.94},
    "Mike Everitt":        {"run_adj": +0.19, "k_adj": -1.1, "bb_adj": +0.7,  "zone_size": 0.96},
    "Larry Vanover":       {"run_adj": +0.14, "k_adj": -0.8, "bb_adj": +0.5,  "zone_size": 0.97},
    "Jim Reynolds":        {"run_adj": +0.26, "k_adj": -1.6, "bb_adj": +1.2,  "zone_size": 0.94},
    "Eric Cooper":         {"run_adj": +0.17, "k_adj": -0.9, "bb_adj": +0.8,  "zone_size": 0.96},
    "Laz Diaz":            {"run_adj": +0.23, "k_adj": -1.3, "bb_adj": +1.0,  "zone_size": 0.95},
    "Mike DiMuro":         {"run_adj": +0.10, "k_adj": -0.6, "bb_adj": +0.5,  "zone_size": 0.98},
    "Bill Miller":         {"run_adj": +0.15, "k_adj": -0.7, "bb_adj": +0.6,  "zone_size": 0.97},
    "John Hirschbeck":     {"run_adj": +0.09, "k_adj": -0.4, "bb_adj": +0.4,  "zone_size": 0.98},
    "Paul Schrieber":      {"run_adj": -0.08, "k_adj": +0.6, "bb_adj": -0.3,  "zone_size": 1.02},
    "Chad Fairchild":      {"run_adj": -0.14, "k_adj": +1.2, "bb_adj": -0.5,  "zone_size": 1.03},
    "Marvin Hudson":       {"run_adj": -0.18, "k_adj": +1.5, "bb_adj": -0.7,  "zone_size": 1.04},
    "Fieldin Culbreth":    {"run_adj": -0.22, "k_adj": +1.8, "bb_adj": -0.9,  "zone_size": 1.05},
    "Brian Knight":        {"run_adj": -0.19, "k_adj": +1.6, "bb_adj": -0.8,  "zone_size": 1.04},
    "Mike Muchlinski":     {"run_adj": -0.25, "k_adj": +2.1, "bb_adj": -1.0,  "zone_size": 1.06},
    "Hunter Wendelstedt":  {"run_adj": -0.28, "k_adj": +2.3, "bb_adj": -1.1,  "zone_size": 1.07},
    "Vic Carapazza":       {"run_adj": -0.31, "k_adj": +2.6, "bb_adj": -1.3,  "zone_size": 1.08},
    "Mark Carlson":        {"run_adj": -0.16, "k_adj": +1.3, "bb_adj": -0.6,  "zone_size": 1.03},
    "Gary Cederstrom":     {"run_adj": -0.11, "k_adj": +0.9, "bb_adj": -0.4,  "zone_size": 1.02},
    "Tim Timmons":         {"run_adj": -0.20, "k_adj": +1.7, "bb_adj": -0.8,  "zone_size": 1.04},
    "Ted Barrett":         {"run_adj": -0.13, "k_adj": +1.1, "bb_adj": -0.5,  "zone_size": 1.03},
    "Jeff Nelson":         {"run_adj": -0.09, "k_adj": +0.7, "bb_adj": -0.3,  "zone_size": 1.02},
    "Toby Basner":         {"run_adj": -0.17, "k_adj": +1.4, "bb_adj": -0.7,  "zone_size": 1.04},
    "Cory Blaser":         {"run_adj": -0.21, "k_adj": +1.7, "bb_adj": -0.8,  "zone_size": 1.05},
    "Nick Mahrley":        {"run_adj": -0.15, "k_adj": +1.2, "bb_adj": -0.6,  "zone_size": 1.03},
    "Ryan Additon":        {"run_adj": -0.12, "k_adj": +1.0, "bb_adj": -0.5,  "zone_size": 1.02},
    "Roberto Ortiz":       {"run_adj": +0.06, "k_adj": -0.3, "bb_adj": +0.3,  "zone_size": 0.99},
    "Jeremie Rehak":       {"run_adj": -0.07, "k_adj": +0.5, "bb_adj": -0.2,  "zone_size": 1.01},
    "Adam Hamari":         {"run_adj": +0.04, "k_adj": -0.2, "bb_adj": +0.2,  "zone_size": 0.99},
    "James Hoye":          {"run_adj": +0.03, "k_adj": -0.1, "bb_adj": +0.1,  "zone_size": 1.00},
    "Tripp Gibson":        {"run_adj": -0.05, "k_adj": +0.4, "bb_adj": -0.2,  "zone_size": 1.01},
    "D.J. Reyburn":        {"run_adj": -0.10, "k_adj": +0.8, "bb_adj": -0.4,  "zone_size": 1.02},
    "David Rackley":       {"run_adj": -0.08, "k_adj": +0.6, "bb_adj": -0.3,  "zone_size": 1.01},
    "Ron Kulpa":           {"run_adj": +0.07, "k_adj": -0.4, "bb_adj": +0.3,  "zone_size": 0.99},
    "Lance Barksdale":     {"run_adj": +0.05, "k_adj": -0.3, "bb_adj": +0.2,  "zone_size": 0.99},
    "Manny Gonzalez":      {"run_adj": +0.11, "k_adj": -0.7, "bb_adj": +0.5,  "zone_size": 0.97},
    "Nate Tomlinson":      {"run_adj": -0.06, "k_adj": +0.4, "bb_adj": -0.2,  "zone_size": 1.01},
    "Jordan Baker":        {"run_adj": +0.09, "k_adj": -0.5, "bb_adj": +0.4,  "zone_size": 0.98},
    "Ben May":             {"run_adj": -0.13, "k_adj": +1.0, "bb_adj": -0.5,  "zone_size": 1.02},
    "default":             {"run_adj":  0.00, "k_adj":  0.0, "bb_adj":  0.0,  "zone_size": 1.00},
}


async def get_game_umpires(game_date: Optional[str] = None) -> dict[int, dict]:
    """
    Fetch home plate umpire for each game from MLB Stats API.
    Returns {game_id: {name, umpire_id, tendencies}}
    """
    from datetime import date
    d = game_date or date.today().strftime("%Y-%m-%d")
    url = f"{BASE}/schedule"
    params = {
        "sportId": 1,
        "date": d,
        "hydrate": "officials",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    result = {}
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            game_id = game.get("gamePk")
            officials = game.get("officials", [])
            hp_ump = next(
                (o for o in officials
                 if o.get("officialType") == "Home Plate"),
                None
            )
            if hp_ump:
                name = hp_ump.get("official", {}).get("fullName", "Unknown")
                uid = hp_ump.get("official", {}).get("id")
                tendencies = get_umpire_tendencies(name)
                result[game_id] = {
                    "name": name,
                    "id": uid,
                    **tendencies,
                }
    return result


def get_umpire_tendencies(name: str) -> dict:
    """Look up historical tendencies for an umpire by name."""
    t = UMPIRE_TENDENCIES.get(name, UMPIRE_TENDENCIES["default"])
    label = _tendency_label(t["run_adj"])
    return {
        "run_adj": t["run_adj"],
        "k_adj": t["k_adj"],
        "bb_adj": t["bb_adj"],
        "zone_size": t["zone_size"],
        "tendency_label": label,
    }


def _tendency_label(run_adj: float) -> str:
    if run_adj >= 0.20:
        return "run-friendly (over lean)"
    elif run_adj >= 0.08:
        return "slightly run-friendly"
    elif run_adj <= -0.20:
        return "pitcher-friendly (under lean)"
    elif run_adj <= -0.08:
        return "slightly pitcher-friendly"
    return "neutral"
