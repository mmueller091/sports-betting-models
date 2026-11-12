"""
FastAPI backend.
MLB:  GET /api/daily, /api/best-bets, /api/hit-props
NHL:  GET /api/nhl/daily, /api/nhl/best-bets, /api/nhl/goal-scorers
      POST /api/refresh, /api/nhl/refresh
      GET /api/health
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dotenv import load_dotenv

load_dotenv()

from data.pipeline import build_daily_snapshot
from data.nhl_pipeline import build_nhl_snapshot
from data.prop_tracker import (
    fetch_hit_prop_lines,
    record_prop_snapshot,
    build_player_prop_lookup,
    get_prop_movement,
    get_latest_prop_lines,
    get_event_ids_for_today,
)
from data.results_tracker import (
    record_picks,
    update_closing_lines,
    update_prop_closing,
    grade_pending,
    performance_summary,
)
from data.bts_tracker import (
    record_bts_predictions,
    load_calibration_rows,
    grade_bts_predictions,
    bts_performance_summary,
)
from models.game_bets import find_best_bets
from models.hit_props import (
    find_hit_candidates,
    select_best_hit_props,
    find_best_bts_pair,
)
from models.bts_model import model_status
from models.nhl_bets import find_nhl_best_bets
from models.nhl_goals import find_nhl_goal_scorers
from train_bts_model import train_if_ready

# ─── Background prop polling ──────────────────────────────────────────────────

PROP_POLL_INTERVAL = 30 * 60  # 30 minutes


async def _poll_props_loop():
    """Poll hit prop lines every 30 minutes and store movement snapshots."""
    while True:
        try:
            await asyncio.sleep(PROP_POLL_INTERVAL)
            # Reuse already-cached MLB snapshot's event IDs to save credits
            today_key = f"mlb:{date.today().isoformat()}"
            snapshot = _snapshot_cache.get(today_key)
            if snapshot:
                event_ids = get_event_ids_for_today(snapshot.get("odds_raw", []))
                if event_ids:
                    lines = await fetch_hit_prop_lines(event_ids)
                    if lines:
                        record_prop_snapshot(lines, date.today())
        except Exception:
            pass


# Daily auto-refresh: rebuild the snapshot late morning (local time) so the
# Beat the Streak picks are fresh and recorded by noon without a manual refresh.
DAILY_REFRESH_HOUR = 11
DAILY_REFRESH_MINUTE = 15


async def _daily_refresh_loop():
    while True:
        try:
            now = datetime.now()
            target = now.replace(
                hour=DAILY_REFRESH_HOUR, minute=DAILY_REFRESH_MINUTE, second=0, microsecond=0
            )
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())

            d = date.today()
            await _grade_history_once()
            key = f"mlb:{d.isoformat()}"
            async with _cache_lock:
                _snapshot_cache.pop(key, None)
                _snapshot_cached_at.pop(key, None)
            snapshot = await get_snapshot(d, sport="mlb")

            # Record today's picks so tracking works even if the app isn't opened
            bets = find_best_bets(snapshot, top_n=5)
            calibration_rows = load_calibration_rows(d)
            candidates = find_hit_candidates(snapshot, calibration_rows=calibration_rows)
            props = select_best_hit_props(candidates, top_n=5)
            record_picks(d, bets=bets, props=props)
            record_bts_predictions(d, candidates)
            update_closing_lines(snapshot.get("odds_raw", []), d)
        except Exception:
            await asyncio.sleep(60)


async def _grade_history_once():
    """Grade completed games without making any request path wait for it."""
    try:
        await grade_pending()
    except Exception:
        pass
    try:
        await grade_bts_predictions()
    except Exception:
        pass
    try:
        async with _training_lock:
            await asyncio.to_thread(train_if_ready)
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    prop_task = asyncio.create_task(_poll_props_loop())
    refresh_task = asyncio.create_task(_daily_refresh_loop())
    grade_task = asyncio.create_task(_grade_history_once())
    yield
    prop_task.cancel()
    refresh_task.cancel()
    grade_task.cancel()


app = FastAPI(
    title="Sports Betting Analyzer",
    description="Daily MLB + NHL betting analysis",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory cache keyed by "{sport}:{date}"
_snapshot_cache: dict[str, dict] = {}
_snapshot_cached_at: dict[str, float] = {}
_snapshot_tasks: dict[str, asyncio.Task] = {}
_cache_lock = asyncio.Lock()
_training_lock = asyncio.Lock()
MLB_CACHE_TTL_SECONDS = 15 * 60


async def get_snapshot(game_date: Optional[date] = None, sport: str = "mlb") -> dict:
    """Get or build a daily snapshot (cached by sport+date)."""
    d = game_date or date.today()
    key = f"{sport}:{d.isoformat()}"

    async with _cache_lock:
        if key in _snapshot_cache:
            cached_age = time.monotonic() - _snapshot_cached_at.get(key, 0.0)
            is_live_mlb_slate = sport == "mlb" and d == date.today()
            if not is_live_mlb_slate or cached_age < MLB_CACHE_TTL_SECONDS:
                return _snapshot_cache[key]
            _snapshot_cache.pop(key, None)
            _snapshot_cached_at.pop(key, None)
        task = _snapshot_tasks.get(key)
        if task is None:
            if sport == "nhl":
                task = asyncio.create_task(build_nhl_snapshot(d))
            else:
                task = asyncio.create_task(build_daily_snapshot(d))
            _snapshot_tasks[key] = task

    try:
        snapshot = await task
    finally:
        async with _cache_lock:
            if _snapshot_tasks.get(key) is task:
                _snapshot_tasks.pop(key, None)

    async with _cache_lock:
        _snapshot_cache[key] = snapshot
        _snapshot_cached_at[key] = time.monotonic()
        if len(_snapshot_cache) > 4:
            oldest_key = sorted(_snapshot_cache.keys())[0]
            del _snapshot_cache[oldest_key]
            _snapshot_cached_at.pop(oldest_key, None)

    return snapshot


@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/daily")
async def daily_snapshot(
    date_str: Optional[str] = Query(None, description="Date as YYYY-MM-DD. Defaults to today.")
):
    """Full daily snapshot with all games, odds, lineups, and weather."""
    game_date = _parse_date(date_str)
    try:
        snapshot = await get_snapshot(game_date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data fetch failed: {e}")
    return {
        "date": snapshot["date"],
        "games_count": len(snapshot["games"]),
        "games": _summarize_games(snapshot["games"]),
    }


@app.get("/api/best-bets")
async def best_bets(
    date_str: Optional[str] = Query(None, description="Date as YYYY-MM-DD. Defaults to today."),
    top_n: int = Query(5, ge=1, le=20, description="Number of bets to return"),
):
    """Top N best bets ranked by expected value."""
    game_date = _parse_date(date_str)
    try:
        snapshot = await get_snapshot(game_date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data fetch failed: {e}")

    bets = find_best_bets(snapshot, top_n=top_n)

    # Record picks + refresh closing-line proxy (never break the endpoint)
    try:
        d = game_date or date.today()
        record_picks(d, bets=bets)
        update_closing_lines(snapshot.get("odds_raw", []), d)
    except Exception:
        pass

    return {"date": snapshot["date"], "count": len(bets), "bets": bets}


@app.get("/api/hit-props")
async def hit_props(
    date_str: Optional[str] = Query(None, description="Date as YYYY-MM-DD. Defaults to today."),
    top_n: int = Query(5, ge=1, le=20, description="Number of players to return"),
):
    """Top N batters most likely to record a hit."""
    game_date = _parse_date(date_str)
    try:
        snapshot = await get_snapshot(game_date)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data fetch failed: {e}")

    d = game_date or date.today()
    calibration_rows = load_calibration_rows(d)
    candidates = find_hit_candidates(snapshot, calibration_rows=calibration_rows)
    props = select_best_hit_props(candidates, top_n=top_n)
    best_pair = find_best_bts_pair(candidates)

    try:
        record_picks(d, props=props)
        record_bts_predictions(d, candidates, selected_count=top_n)
        update_prop_closing(snapshot.get("prop_lookup", {}), d)
    except Exception:
        pass

    return {
        "date": snapshot["date"],
        "count": len(props),
        "candidate_count": len(candidates),
        "players": props,
        "best_pair": best_pair,
        "model": {
            **model_status(),
            "calibration_rows": len(calibration_rows),
        },
    }


@app.get("/api/prop-lines")
async def prop_lines(
    date_str: Optional[str] = Query(None, description="Date as YYYY-MM-DD. Defaults to today.")
):
    """
    Current hit prop lines + movement since opening.
    Polls are automatic every 30 min. Use /api/prop-lines/refresh to force a new poll.
    """
    game_date = _parse_date(date_str)
    d = game_date or date.today()
    lines = get_latest_prop_lines(d)
    movement = get_prop_movement(d)
    return {
        "date": d.isoformat(),
        "lines_count": len(lines),
        "lines": lines,
        "movement": movement,
        "note": "Polls automatically every 30 min. Lines cost ~10 Odds API credits per game.",
    }


@app.post("/api/prop-lines/refresh")
async def refresh_prop_lines(
    date_str: Optional[str] = Query(None, description="Date to refresh. Defaults to today.")
):
    """Force a fresh prop line fetch right now (uses Odds API credits)."""
    game_date = _parse_date(date_str)
    d = game_date or date.today()
    today_key = f"mlb:{d.isoformat()}"
    snapshot = _snapshot_cache.get(today_key)
    if not snapshot:
        raise HTTPException(status_code=400, detail="MLB snapshot not loaded yet. Fetch /api/daily first.")
    event_ids = get_event_ids_for_today(snapshot.get("odds_raw", []))
    if not event_ids:
        raise HTTPException(status_code=404, detail="No event IDs found for today.")
    lines = await fetch_hit_prop_lines(event_ids)
    if lines:
        record_prop_snapshot(lines, d)
    movement = get_prop_movement(d)
    return {
        "refreshed": True,
        "lines_fetched": len(lines),
        "movement_detected": len(movement),
        "movement": movement,
    }


@app.get("/api/performance")
async def performance():
    """
    Grade pending picks against final scores, then return the
    performance report: ROI, win rates, calibration, and CLV.
    """
    try:
        graded = await grade_pending()
    except Exception:
        graded = 0
    try:
        graded_bts = await grade_bts_predictions()
    except Exception:
        graded_bts = 0
    summary = performance_summary()
    summary["graded_this_call"] = graded
    summary["bts"] = bts_performance_summary()
    summary["bts"]["graded_this_call"] = graded_bts
    return summary


@app.post("/api/refresh")
async def refresh_cache(
    date_str: Optional[str] = Query(None, description="Date to refresh. Defaults to today.")
):
    """Force-refresh the MLB cache for a given date."""
    game_date = _parse_date(date_str)
    key = f"mlb:{(game_date or date.today()).isoformat()}"
    async with _cache_lock:
        if key in _snapshot_cache:
            del _snapshot_cache[key]
            _snapshot_cached_at.pop(key, None)
    snapshot = await get_snapshot(game_date, sport="mlb")
    return {"refreshed": True, "date": snapshot["date"], "games": len(snapshot["games"])}


# ─── NHL Endpoints ────────────────────────────────────────────────────────────

@app.get("/api/nhl/daily")
async def nhl_daily(
    date_str: Optional[str] = Query(None, description="Date as YYYY-MM-DD. Defaults to today.")
):
    """Today's NHL games with odds, goalie info, and standings context."""
    game_date = _parse_date(date_str)
    try:
        snapshot = await get_snapshot(game_date, sport="nhl")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data fetch failed: {e}")
    return {
        "date": snapshot["date"],
        "games_count": len(snapshot["games"]),
        "games": _summarize_nhl_games(snapshot["games"]),
    }


@app.get("/api/nhl/best-bets")
async def nhl_best_bets(
    date_str: Optional[str] = Query(None, description="Date as YYYY-MM-DD. Defaults to today."),
    top_n: int = Query(5, ge=1, le=20),
):
    """Top N NHL bets ranked by expected value."""
    game_date = _parse_date(date_str)
    try:
        snapshot = await get_snapshot(game_date, sport="nhl")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data fetch failed: {e}")
    bets = find_nhl_best_bets(snapshot, top_n=top_n)
    return {"date": snapshot["date"], "count": len(bets), "bets": bets}


@app.get("/api/nhl/goal-scorers")
async def nhl_goal_scorers(
    date_str: Optional[str] = Query(None, description="Date as YYYY-MM-DD. Defaults to today."),
    top_n: int = Query(5, ge=1, le=20),
):
    """Top N NHL players most likely to score a goal."""
    game_date = _parse_date(date_str)
    try:
        snapshot = await get_snapshot(game_date, sport="nhl")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Data fetch failed: {e}")
    scorers = find_nhl_goal_scorers(snapshot, top_n=top_n)
    return {"date": snapshot["date"], "count": len(scorers), "players": scorers}


@app.post("/api/nhl/refresh")
async def nhl_refresh_cache(
    date_str: Optional[str] = Query(None, description="Date to refresh. Defaults to today.")
):
    """Force-refresh the NHL cache."""
    game_date = _parse_date(date_str)
    key = f"nhl:{(game_date or date.today()).isoformat()}"
    async with _cache_lock:
        if key in _snapshot_cache:
            del _snapshot_cache[key]
            _snapshot_cached_at.pop(key, None)
    snapshot = await get_snapshot(game_date, sport="nhl")
    return {"refreshed": True, "date": snapshot["date"], "games": len(snapshot["games"])}


def _parse_date(date_str: Optional[str]) -> Optional[date]:
    if not date_str:
        return None
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {date_str}. Use YYYY-MM-DD.")


def _summarize_nhl_games(games: list[dict]) -> list[dict]:
    summary = []
    for g in games:
        away_goalie = g.get("away_team_stats", {}).get("starting_goalie", {})
        home_goalie = g.get("home_team_stats", {}).get("starting_goalie", {})
        away_st = g.get("away_standings", {})
        home_st = g.get("home_standings", {})
        summary.append({
            "game_id": g.get("game_id"),
            "matchup": f"{g.get('away_team')} @ {g.get('home_team')}",
            "venue": g.get("venue"),
            "game_time": g.get("game_time"),
            "away_goalie": away_goalie.get("name", "TBD"),
            "away_goalie_sv": away_goalie.get("save_pct"),
            "away_goalie_gaa": away_goalie.get("gaa"),
            "home_goalie": home_goalie.get("name", "TBD"),
            "home_goalie_sv": home_goalie.get("save_pct"),
            "home_goalie_gaa": home_goalie.get("gaa"),
            "away_win_pct": away_st.get("win_pct"),
            "home_win_pct": home_st.get("win_pct"),
            "away_l10": away_st.get("l10_wins"),
            "home_l10": home_st.get("l10_wins"),
            "away_back_to_back": g.get("away_back_to_back", False),
            "home_back_to_back": g.get("home_back_to_back", False),
            "consensus_total": g.get("odds", {}).get("consensus_total"),
            "moneyline_away": g.get("odds", {}).get("best_moneyline_away"),
            "moneyline_home": g.get("odds", {}).get("best_moneyline_home"),
        })
    return summary


# ─── Serve React build (must be last) ────────────────────────────────────────

_BUILD_DIR = os.path.join(os.path.dirname(__file__), "../../frontend/build")

if os.path.exists(_BUILD_DIR):
    app.mount("/static", StaticFiles(directory=os.path.join(_BUILD_DIR, "static")), name="static-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        # Serve index.html for any non-API path (React Router support)
        return FileResponse(os.path.join(_BUILD_DIR, "index.html"))


def _summarize_games(games: list[dict]) -> list[dict]:
    """Strip heavy batter data from the games summary for the /daily endpoint."""
    summary = []
    for g in games:
        summary.append({
            "game_id": g.get("game_id"),
            "matchup": f"{g.get('away_team')} @ {g.get('home_team')}",
            "venue": g.get("venue"),
            "game_time": g.get("game_time"),
            "away_pitcher": g.get("away_probable_pitcher", {}).get("name", "TBD") if g.get("away_probable_pitcher") else "TBD",
            "home_pitcher": g.get("home_probable_pitcher", {}).get("name", "TBD") if g.get("home_probable_pitcher") else "TBD",
            "park_factor": g.get("park_factor"),
            "weather": {
                "temp_f": g.get("weather", {}).get("temperature_f"),
                "wind": f"{g.get('weather', {}).get('wind_speed_mph')} mph {g.get('weather', {}).get('wind_direction')}",
                "is_indoor": g.get("weather", {}).get("is_indoor", False),
                "notes": g.get("weather", {}).get("weather_factor", {}).get("notes", []),
            },
            "consensus_total": g.get("odds", {}).get("consensus_total"),
            "moneyline_away": g.get("odds", {}).get("best_moneyline_away"),
            "moneyline_home": g.get("odds", {}).get("best_moneyline_home"),
        })
    return summary
