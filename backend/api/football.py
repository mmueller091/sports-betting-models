"""Weekly football endpoints shared by the local API and GitHub export."""
import asyncio
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from data.football import build_football_snapshot
from models.football_bets import find_football_bets

router = APIRouter()
_cache = {}
_locks = {"nfl": asyncio.Lock(), "cfb": asyncio.Lock()}
CACHE_SECONDS = 15 * 60


async def weekly(league, date_str=None, refresh=False):
    try:
        target = date.fromisoformat(date_str) if date_str else datetime.now(ZoneInfo("America/New_York")).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Use a date in YYYY-MM-DD format.")
    key = (league, target.isoformat())
    async with _locks[league]:
        cached = _cache.get(key)
        if not refresh and cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        try:
            snapshot = await build_football_snapshot(league, target)
            result = await asyncio.to_thread(find_football_bets, snapshot, 5)
        except Exception:
            raise HTTPException(status_code=502, detail=f"{league.upper()} data is temporarily unavailable.") from None
        _cache[key] = (time.monotonic(), result)
        for old_key in list(_cache):
            if old_key[0] == league and old_key != key:
                _cache.pop(old_key, None)
        return result


@router.get("/api/nfl/weekly")
async def nfl_weekly(date_str: str | None = Query(None)):
    return await weekly("nfl", date_str)


@router.get("/api/cfb/weekly")
async def cfb_weekly(date_str: str | None = Query(None)):
    return await weekly("cfb", date_str)


@router.post("/api/nfl/refresh")
async def nfl_refresh():
    return await weekly("nfl", refresh=True)


@router.post("/api/cfb/refresh")
async def cfb_refresh():
    return await weekly("cfb", refresh=True)
