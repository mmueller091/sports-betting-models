"""
Main data pipeline orchestrator.
Fetches: games, lineups, pitcher stats, odds, weather, umpires, injuries, line movement.
"""

import asyncio
import os
from datetime import date
from typing import Optional

from data.mlb_stats import (
    get_todays_games,
    get_pitcher_stats,
    get_pitcher_splits,
    get_pitcher_recent_starts,
    get_batter_stats,
    get_batter_last_n_games,
    get_batter_vs_pitcher,
    get_batter_splits,
    get_batter_vs_hand_splits,
    get_players_hands,
    get_lineup,
    get_projected_lineup,
    get_team_recent_results,
    get_team_bullpen_usage,
    get_team_bullpen_quality,
)
from data.odds import get_game_odds
from data.source_health import safe_fetch
from data.weather import get_game_weather
from data.savant import (
    get_xstats_batter,
    get_xstats_pitcher,
    get_park_factor,
    get_hit_park_factors,
)
from data.savant_advanced import get_team_defense_oaa, apply_oaa_to_games
from data.umpire import get_game_umpires
from data.espn_injuries import get_mlb_injuries, apply_injuries_to_mlb_games
from data.line_tracker import record_opening_lines, get_line_movement, apply_line_movement_to_games
from data.prop_tracker import (
    get_event_ids_for_today,
    fetch_hit_prop_lines,
    record_prop_snapshot,
    build_player_prop_lookup,
    player_lookup_key,
    prop_lines_stale,
)


async def build_daily_snapshot(game_date: Optional[date] = None) -> dict:
    """
    Full pipeline run. Returns a snapshot dict with all enriched game data.
    """
    target_date = game_date or date.today()
    date_str = target_date.strftime("%Y-%m-%d")

    # Parallel: fetch games, odds, umpires, injuries, defense, and hit park factors.
    games_res, odds_res, umpires_res, injuries_res, oaa_res, park_res = await asyncio.gather(
        _safe(get_todays_games(target_date), []),
        _safe(get_game_odds(), []),
        _safe(get_game_umpires(date_str), {}),
        _safe(get_mlb_injuries(), {}),
        _safe(get_team_defense_oaa(target_date.year), {}),
        _safe(get_hit_park_factors(target_date.year), {}),
        return_exceptions=False,
    )

    games = games_res if games_res else []
    odds_list = odds_res if odds_res else []
    umpires = umpires_res if umpires_res else {}
    injuries = injuries_res if injuries_res else {}
    team_oaa = oaa_res if oaa_res else {}
    hit_park_factors = park_res if park_res else {}

    # Record opening lines (no-op if already recorded today)
    try:
        record_opening_lines(odds_list, target_date)
    except Exception:
        pass

    # Line movement (sharp money signal)
    try:
        movements = get_line_movement(odds_list, target_date)
    except Exception:
        movements = {}

    # Fetch hit prop lines for today's games (uses Odds API credits — cached)
    try:
        event_ids = get_event_ids_for_today(odds_list)
        if os.getenv("ENABLE_HIT_PROP_ODDS", "true").lower() == "true" and event_ids and prop_lines_stale(target_date):
            prop_lines = await fetch_hit_prop_lines(event_ids)
            if prop_lines:
                record_prop_snapshot(prop_lines, target_date)
    except Exception:
        pass

    # Build prop lookup for the model
    prop_lookup = build_player_prop_lookup(target_date)

    odds_by_teams = _index_odds(odds_list)
    team_contexts = await _build_team_contexts(games, target_date)

    # Batch-fetch handedness for all probable pitchers (one API call)
    pitcher_ids = [
        p["id"]
        for game in games
        for p in (game.get("away_probable_pitcher"), game.get("home_probable_pitcher"))
        if p
    ]
    pitcher_hands = await _safe(get_players_hands(pitcher_ids), {})

    # Enrich games concurrently (semaphore keeps us polite to the APIs)
    upcoming = [g for g in games if g["status"] not in ("Final", "Live")]
    sem = asyncio.Semaphore(4)

    async def _enrich_with_limit(g):
        async with sem:
            return await _enrich_game(
                g,
                odds_by_teams,
                umpires,
                team_contexts,
                prop_lookup,
                pitcher_hands,
                target_date.year,
                hit_park_factors,
            )

    results = await asyncio.gather(
        *(_enrich_with_limit(g) for g in upcoming), return_exceptions=True
    )
    enriched_games = [r for r in results if not isinstance(r, Exception)]

    # Apply cross-game data
    enriched_games = apply_injuries_to_mlb_games(enriched_games, injuries)
    enriched_games = apply_line_movement_to_games(enriched_games, movements)
    enriched_games = apply_oaa_to_games(enriched_games, team_oaa)

    return {
        "date": target_date.isoformat(),
        "games": enriched_games,
        "odds_raw": odds_list,
        "injuries": injuries,
        "line_movements": movements,
        "prop_lookup": prop_lookup,
    }


async def _enrich_game(
    game: dict,
    odds_by_teams: dict,
    umpires: dict = {},
    team_contexts: dict[int, dict] = {},
    prop_lookup: dict = {},
    pitcher_hands: dict = {},
    season: int | None = None,
    hit_park_factors: dict = {},
) -> dict:
    """Enrich a single game with pitcher stats, lineup, weather, odds, umpire."""
    game_id = game["game_id"]
    venue = game.get("venue", "default")

    # Fetch lineup, weather, and park factor in parallel
    lineup_task = asyncio.create_task(_safe(get_lineup(game_id), {}))
    weather_task = asyncio.create_task(_safe(get_game_weather(venue, game.get("game_time")), {}))

    lineup, weather = await asyncio.gather(lineup_task, weather_task)

    # Lineup not posted yet → fall back to last game's batting order (projected)
    lineup = lineup or {}
    lineup_projected = {"away": False, "home": False}
    for side, team_id_key in (("away", "away_team_id"), ("home", "home_team_id")):
        if not lineup.get(side) and game.get(team_id_key):
            projected = await _safe(get_projected_lineup(game[team_id_key]), [])
            if projected:
                lineup[side] = projected
                lineup_projected[side] = True

    park_factor = get_park_factor(venue)

    # Enrich pitchers
    away_pitcher = game.get("away_probable_pitcher")
    home_pitcher = game.get("home_probable_pitcher")

    away_pitcher_data = {}
    home_pitcher_data = {}

    pitcher_tasks = []
    if away_pitcher:
        pitcher_tasks.append(_enrich_pitcher(away_pitcher["id"], season))
    else:
        pitcher_tasks.append(_safe_empty())

    if home_pitcher:
        pitcher_tasks.append(_enrich_pitcher(home_pitcher["id"], season))
    else:
        pitcher_tasks.append(_safe_empty())

    results = await asyncio.gather(*pitcher_tasks, return_exceptions=True)
    away_pitcher_data = results[0] if not isinstance(results[0], Exception) else {}
    home_pitcher_data = results[1] if not isinstance(results[1], Exception) else {}

    # Attach throwing hand (batters' platoon splits key off this)
    if away_pitcher:
        away_pitcher_data["hand"] = pitcher_hands.get(away_pitcher["id"], {}).get("pitch_hand", "R")
    if home_pitcher:
        home_pitcher_data["hand"] = pitcher_hands.get(home_pitcher["id"], {}).get("pitch_hand", "R")

    # Enrich batters in lineup
    away_batters = await _enrich_batters(
        lineup.get("away", []),
        home_pitcher.get("id") if home_pitcher else None,
        prop_lookup,
        season,
    )
    home_batters = await _enrich_batters(
        lineup.get("home", []),
        away_pitcher.get("id") if away_pitcher else None,
        prop_lookup,
        season,
    )

    # Match odds
    odds = _find_odds(game, odds_by_teams)

    # Umpire data
    umpire = umpires.get(game_id, {})
    away_context = team_contexts.get(game.get("away_team_id"), {})
    home_context = team_contexts.get(game.get("home_team_id"), {})

    return {
        **game,
        "venue": venue,
        "park_factor": park_factor,
        "hit_park_factor": hit_park_factors.get(venue, park_factor),
        "weather": weather,
        "odds": odds,
        "umpire": umpire,
        "away_pitcher_data": away_pitcher_data,
        "home_pitcher_data": home_pitcher_data,
        "away_lineup": away_batters,
        "home_lineup": home_batters,
        "away_lineup_projected": lineup_projected["away"],
        "home_lineup_projected": lineup_projected["home"],
        "away_team_context": away_context,
        "home_team_context": home_context,
    }


async def _enrich_pitcher(pitcher_id: int, season: int | None = None) -> dict:
    current_season = season or date.today().year
    stats, previous, splits, xstats, recent = await asyncio.gather(
        _safe(get_pitcher_stats(pitcher_id, current_season), {}),
        _safe(get_pitcher_stats(pitcher_id, current_season - 1), {}),
        _safe(get_pitcher_splits(pitcher_id, current_season), {}),
        _safe(get_xstats_pitcher(pitcher_id, current_season), {}),
        _safe(get_pitcher_recent_starts(pitcher_id, season=current_season), {}),
    )
    return {
        "season_stats": stats,
        "previous_stats": previous,
        "splits": splits,
        "xstats": xstats,
        "recent_starts": recent,
    }


async def _enrich_batters(
    batters: list[dict],
    opposing_pitcher_id: Optional[int],
    prop_lookup: dict = {},
    season: int | None = None,
) -> list[dict]:
    """Enrich each batter in the lineup concurrently."""
    tasks = [_enrich_batter(b, opposing_pitcher_id, prop_lookup, season) for b in batters]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    enriched = []
    for batter, result in zip(batters, results):
        if isinstance(result, Exception):
            enriched.append({
                **batter,
                "stats": {},
                "previous_stats": {},
                "recent": {},
                "vs_pitcher": {},
                "xstats": {},
                "splits": {},
                "vs_hand": {},
                "prop": {},
            })
        else:
            enriched.append(result)
    return enriched


async def _enrich_batter(
    batter: dict,
    opposing_pitcher_id: Optional[int],
    prop_lookup: dict = {},
    season: int | None = None,
) -> dict:
    batter_id = batter["id"]
    batter_name = batter.get("name", "")
    current_season = season or date.today().year
    tasks = [
        _safe(get_batter_stats(batter_id, current_season), {}),
        _safe(get_batter_stats(batter_id, current_season - 1), {}),
        _safe(get_batter_last_n_games(batter_id, 15, current_season), {}),
        _safe(get_xstats_batter(batter_id, current_season), {}),
        _safe(get_batter_splits(batter_id, current_season), {}),
        _safe(get_batter_vs_hand_splits(batter_id, current_season), {}),
    ]
    if opposing_pitcher_id:
        tasks.append(_safe(
            get_batter_vs_pitcher(batter_id, opposing_pitcher_id, current_season), {}
        ))
    else:
        tasks.append(_safe_empty())

    results = await asyncio.gather(*tasks, return_exceptions=True)
    prop = prop_lookup.get(batter_name, {}) or prop_lookup.get(player_lookup_key(batter_name), {})
    return {
        **batter,
        "stats": results[0] if not isinstance(results[0], Exception) else {},
        "previous_stats": results[1] if not isinstance(results[1], Exception) else {},
        "recent": results[2] if not isinstance(results[2], Exception) else {},
        "xstats": results[3] if not isinstance(results[3], Exception) else {},
        "splits": results[4] if not isinstance(results[4], Exception) else {},
        "vs_hand": results[5] if not isinstance(results[5], Exception) else {},
        "vs_pitcher": results[6] if not isinstance(results[6], Exception) else {},
        "prop": prop,
    }


def _index_odds(odds_list: list[dict]) -> dict:
    """Index odds by (away_team, home_team) tuple."""
    index = {}
    for g in odds_list:
        key = (g["away_team"], g["home_team"])
        index[key] = g
        # Also index by partial name in case of slight mismatches
        index[(g["away_team"].split()[-1], g["home_team"].split()[-1])] = g
    return index


def _find_odds(game: dict, odds_index: dict) -> dict:
    away = game["away_team"]
    home = game["home_team"]
    odds = odds_index.get((away, home))
    if not odds:
        # Try last-word match
        odds = odds_index.get((away.split()[-1], home.split()[-1]), {})
    return odds or {}


async def _safe(coro, default):
    return await safe_fetch(coro, default)


async def _safe_empty():
    return {}


async def _build_team_contexts(games: list[dict], target_date: date) -> dict[int, dict]:
    team_ids = {
        game.get("away_team_id")
        for game in games
        if game.get("away_team_id")
    } | {
        game.get("home_team_id")
        for game in games
        if game.get("home_team_id")
    }

    tasks = {
        team_id: asyncio.create_task(_fetch_team_context(team_id, target_date))
        for team_id in team_ids
    }

    contexts = {}
    for team_id, task in tasks.items():
        contexts[team_id] = await task
    return contexts


async def _fetch_team_context(team_id: int, target_date: date) -> dict:
    recent_form, bullpen, bullpen_quality = await asyncio.gather(
        _safe(get_team_recent_results(team_id, game_date=target_date), {}),
        _safe(get_team_bullpen_usage(team_id, game_date=target_date), {}),
        _safe(get_team_bullpen_quality(team_id, target_date.year), {}),
    )
    return {
        "recent_form": recent_form,
        "bullpen": bullpen,
        "bullpen_quality": bullpen_quality,
    }
