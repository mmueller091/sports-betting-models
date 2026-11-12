"""Run the existing API/models once and export public dashboard responses."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
from api.main import app
from data.odds import ODDS_STATUS
from data.source_health import begin_collection, end_collection
from train_bts_model import train_if_ready

ENDPOINTS = {
    "mlb": {"bets": "/api/best-bets", "hits": "/api/hit-props", "games": "/api/daily"},
    "nhl": {"bets": "/api/nhl/best-bets", "scorers": "/api/nhl/goal-scorers", "games": "/api/nhl/daily"},
    "nfl": {"weekly": "/api/nfl/weekly"},
    "cfb": {"weekly": "/api/cfb/weekly"},
}
ODDS_SPORTS = {"mlb": "baseball_mlb", "nhl": "icehockey_nhl", "nfl": "americanfootball_nfl", "cfb": "americanfootball_ncaaf"}

def read_previous(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

async def export_sport(client, sport, target_date, previous):
    token = begin_collection()
    data, failure = {}, None
    try:
        for name, endpoint in ENDPOINTS[sport].items():
            response = await client.get(endpoint, params={"date_str": target_date})
            response.raise_for_status()
            data[name] = response.json()
    except Exception as exc:
        # Exception text can include source URLs and secrets; never publish it.
        failure = type(exc).__name__
    finally:
        warnings = end_collection(token)
    schedule_source = {"mlb": "get_todays_games", "nhl": "get_todays_nhl_games"}.get(sport, "build_football_snapshot")
    if failure or any(w.startswith(schedule_source + ":") for w in warnings):
        return {**previous, "status": "stale" if previous.get("data") else "unavailable",
                "warnings": ["The latest update failed. Showing the last successful results, if available.", *warnings]}
    if sport in ("nfl", "cfb"):
        warnings = list(dict.fromkeys([*warnings, *data.get("weekly", {}).get("warnings", [])]))
    return {
        "status": "partial" if warnings else "ok",
        "generated_at": datetime.now(timezone.utc).isoformat(), "date": target_date,
        "warnings": warnings,
        "odds": ODDS_STATUS.get(ODDS_SPORTS[sport], {}),
        "data": data,
    }

async def export(output, previous_path=None):
    previous = read_previous(previous_path or output)
    target_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://snapshot", timeout=900) as client:
        try:
            response = await client.get("/api/performance")
            response.raise_for_status()
            performance = response.json()
        except Exception:
            performance = previous.get("performance")
        try:
            await asyncio.to_thread(train_if_ready)
        except Exception:
            pass  # Insufficient history must not stop baseline predictions.
        sports = await asyncio.gather(*(
            export_sport(client, sport, target_date, previous.get(sport, {})) for sport in ENDPOINTS
        ))
    bundle = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "date": target_date,
        "timezone": "America/New_York", "refresh_minutes": 60, "odds_cache_hours": float(os.getenv("ODDS_CACHE_HOURS", "24")),
        "performance": performance, **dict(zip(ENDPOINTS, sports)),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(bundle, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(output)
    for sport in ENDPOINTS:
        result = bundle[sport]
        print(f"{sport.upper()}: {result['status']}; {len(result.get('warnings', []))} source warnings")
    return bundle

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args()
    asyncio.run(export(args.output, args.previous))
