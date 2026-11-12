"""
NHL data pipeline orchestrator.
Fetches: games, team stats, standings, odds, confirmed goalies,
         MoneyPuck xG, Natural Stat Trick Corsi, ESPN injuries, line movement.
"""

import asyncio
from datetime import date
from typing import Optional

from data.nhl_stats import (
    get_todays_nhl_games,
    get_team_stats,
    get_team_schedule_last_n,
    get_schedule_spot_metrics,
    check_back_to_back,
    get_standings,
)
from data.odds import get_game_odds
from data.source_health import safe_fetch
from data.nhl_goalies import get_confirmed_goalies, apply_confirmed_goalies
from data.moneypuck import get_team_xg_stats, get_goalie_xg_stats, get_skater_xg_stats, apply_xg_to_games
from data.natural_stat_trick import get_team_corsi_xg, apply_nst_to_games
from data.espn_injuries import get_nhl_injuries, apply_injuries_to_nhl_games
from data.line_tracker import record_opening_lines, get_line_movement, apply_line_movement_to_games


async def build_nhl_snapshot(game_date: Optional[date] = None) -> dict:
    """
    Full NHL pipeline. Returns enriched game data for analysis.
    """
    target_date = game_date or date.today()

    # All top-level fetches in parallel
    (
        games, standings, odds_list,
        confirmed_goalies, team_xg, goalie_xg,
        skater_xg, nst_data, injuries
    ) = await asyncio.gather(
        _safe(get_todays_nhl_games(target_date), []),
        _safe(get_standings(), {}),
        _safe(get_game_odds(sport="icehockey_nhl"), []),
        _safe(get_confirmed_goalies(target_date), {}),
        _safe(get_team_xg_stats(), {}),
        _safe(get_goalie_xg_stats(), {}),
        _safe(get_skater_xg_stats(), {}),
        _safe(get_team_corsi_xg(), {}),
        _safe(get_nhl_injuries(), {}),
        return_exceptions=False,
    )

    # Record opening lines (first call of the day)
    try:
        record_opening_lines(odds_list, target_date)
    except Exception:
        pass

    try:
        movements = get_line_movement(odds_list, target_date)
    except Exception:
        movements = {}

    odds_by_teams = _index_nhl_odds(odds_list)

    enriched = []
    for game in games:
        eg = await _enrich_nhl_game(game, standings, odds_by_teams, target_date)
        enriched.append(eg)

    # Apply confirmed goalies (overrides likely-starter guess)
    if confirmed_goalies:
        enriched = apply_confirmed_goalies(enriched, confirmed_goalies)

    # Apply MoneyPuck xG
    enriched = apply_xg_to_games(enriched, team_xg, goalie_xg, skater_xg)

    # Apply Natural Stat Trick 5v5 Corsi/xG
    enriched = apply_nst_to_games(enriched, nst_data)

    # Apply ESPN injuries
    enriched = apply_injuries_to_nhl_games(enriched, injuries)

    # Apply line movement (sharp signal)
    enriched = apply_line_movement_to_games(enriched, movements)

    return {
        "date": target_date.isoformat(),
        "games": enriched,
        "standings": standings,
        "confirmed_goalies": confirmed_goalies,
        "injuries": injuries,
        "line_movements": movements,
    }


async def _enrich_nhl_game(
    game: dict,
    standings: dict,
    odds_by_teams: dict,
    game_date: date,
) -> dict:
    away_abbrev = game["away_team_abbrev"]
    home_abbrev = game["home_team_abbrev"]

    results = await asyncio.gather(
        _safe(get_team_stats(away_abbrev), {}),
        _safe(get_team_stats(home_abbrev), {}),
        _safe(get_team_schedule_last_n(away_abbrev, 5), []),
        _safe(get_team_schedule_last_n(home_abbrev, 5), []),
        _safe(get_schedule_spot_metrics(away_abbrev, game_date), {}),
        _safe(get_schedule_spot_metrics(home_abbrev, game_date), {}),
        _safe(check_back_to_back(away_abbrev, game_date), False),
        _safe(check_back_to_back(home_abbrev, game_date), False),
        return_exceptions=False,
    )

    away_team_stats = results[0]
    home_team_stats = results[1]
    away_recent = results[2]
    home_recent = results[3]
    away_schedule_spot = results[4]
    home_schedule_spot = results[5]
    away_b2b = results[6]
    home_b2b = results[7]

    away_standings = standings.get(away_abbrev, {})
    home_standings = standings.get(home_abbrev, {})

    odds = _find_nhl_odds(game, odds_by_teams)

    return {
        **game,
        "away_standings": away_standings,
        "home_standings": home_standings,
        "away_team_stats": away_team_stats,
        "home_team_stats": home_team_stats,
        "away_recent_form": away_recent,
        "home_recent_form": home_recent,
        "away_schedule_spot": away_schedule_spot,
        "home_schedule_spot": home_schedule_spot,
        "away_back_to_back": away_b2b,
        "home_back_to_back": home_b2b,
        "odds": odds,
        "away_scorers": away_team_stats.get("top_scorers", []),
        "home_scorers": home_team_stats.get("top_scorers", []),
    }


def _index_nhl_odds(odds_list: list[dict]) -> dict:
    index = {}
    for g in odds_list:
        key = (g["away_team"], g["home_team"])
        index[key] = g
        index[(g["away_team"].split()[-1], g["home_team"].split()[-1])] = g
    return index


def _find_nhl_odds(game: dict, odds_index: dict) -> dict:
    away = game["away_team"]
    home = game["home_team"]
    odds = odds_index.get((away, home))
    if not odds:
        odds = odds_index.get((away.split()[-1], home.split()[-1]), {})
    return odds or {}


async def _safe(coro, default):
    return await safe_fetch(coro, default)
