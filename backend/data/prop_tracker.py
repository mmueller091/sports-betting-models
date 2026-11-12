"""
Hit prop line tracker.
Fetches batter hit prop lines (over/under 0.5 hits) from The Odds API.
Polls multiple times before first pitch and detects line movement.

Credit cost: ~10 credits per event via The Odds API player props endpoint.
To save credits we only fetch props for the top N games by odds interest.

Movement detection:
- Opening line snapshot stored at first fetch of the day
- Each subsequent fetch compared to opening
- Significant move (>8 cents) flagged as sharp action
"""

import os
import json
import asyncio
import re
import statistics
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

ODDS_BASE = "https://api.the-odds-api.com/v4"
from data.storage import STORE_DIR
PROPS_FILE = STORE_DIR / "prop_lines.json"

# Max games to fetch props for (to conserve Odds API credits)
MAX_PROP_GAMES = 6
# Minimum line move (in American odds points) to flag as sharp
SHARP_MOVE_THRESHOLD = 8


def _api_key() -> str:
    key = os.getenv("ODDS_API_KEY", "")
    if not key:
        raise ValueError("ODDS_API_KEY not set")
    return key


def _load_store() -> dict:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    if PROPS_FILE.exists():
        try:
            return json.loads(PROPS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_store(data: dict) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    PROPS_FILE.write_text(json.dumps(data, indent=2))


async def fetch_hit_prop_lines(event_ids: list[str]) -> list[dict]:
    """
    Fetch batter_hits player props for the given Odds API event IDs.
    Returns list of {player, team, matchup, over_odds, under_odds, line, book, fetched_at}.
    Costs ~10 credits per event on The Odds API free tier.
    """
    results = []
    now = datetime.now(timezone.utc).isoformat()

    async with httpx.AsyncClient(timeout=20) as client:
        tasks = [_fetch_event_props(client, eid, now) for eid in event_ids[:MAX_PROP_GAMES]]
        batches = await asyncio.gather(*tasks, return_exceptions=True)

    for batch in batches:
        if isinstance(batch, Exception):
            continue
        results.extend(batch)

    return results


async def _fetch_event_props(client: httpx.AsyncClient, event_id: str, now: str) -> list[dict]:
    url = f"{ODDS_BASE}/sports/baseball_mlb/events/{event_id}/odds"
    params = {
        "apiKey": _api_key(),
        "regions": "us",
        "markets": "batter_hits",
        "oddsFormat": "american",
    }
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    away = data.get("away_team", "")
    home = data.get("home_team", "")
    matchup = f"{away} @ {home}"
    lines = []

    for bookmaker in data.get("bookmakers", []):
        book = bookmaker["key"]
        for market in bookmaker.get("markets", []):
            if market.get("key") != "batter_hits":
                continue
            for outcome in market.get("outcomes", []):
                name = outcome.get("name")           # "Over" or "Under"
                player = outcome.get("description")  # Player name
                price = outcome.get("price")         # American odds
                point = outcome.get("point", 0.5)    # Line (usually 0.5)

                if not player or price is None:
                    continue

                lines.append({
                    "player": player,
                    "team": None,
                    "matchup": matchup,
                    "side": name,       # "Over" or "Under"
                    "odds": int(price),
                    "line": point,
                    "book": book,
                    "fetched_at": now,
                })

    return lines


def record_prop_snapshot(lines: list[dict], game_date: Optional[date] = None) -> None:
    """Store a prop line snapshot. First call of the day sets the opening line."""
    d = (game_date or date.today()).isoformat()
    store = _load_store()
    day_data = store.get(d, {"opening": None, "snapshots": []})

    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lines": lines,
    }

    # First snapshot of the day = opening lines
    if day_data["opening"] is None and lines:
        day_data["opening"] = snapshot

    day_data["snapshots"].append(snapshot)

    # Keep at most 20 snapshots per day
    if len(day_data["snapshots"]) > 20:
        day_data["snapshots"] = day_data["snapshots"][-20:]

    store[d] = day_data

    # Prune old days (keep 3 days)
    all_days = sorted(store.keys())
    for old_day in all_days[:-3]:
        del store[old_day]

    _save_store(store)


def get_latest_prop_lines(game_date: Optional[date] = None) -> list[dict]:
    """Return the most recent prop lines for the day."""
    d = (game_date or date.today()).isoformat()
    store = _load_store()
    day_data = store.get(d, {})
    snapshots = day_data.get("snapshots", [])
    if not snapshots:
        return []
    return snapshots[-1].get("lines", [])


def prop_lines_stale(
    game_date: Optional[date] = None,
    minimum_age_minutes: int = 30,
) -> bool:
    """Return True when a new paid prop request is warranted."""
    d = (game_date or date.today()).isoformat()
    day_data = _load_store().get(d, {})
    snapshots = day_data.get("snapshots", [])
    if not snapshots:
        return True
    timestamp = snapshots[-1].get("timestamp")
    try:
        fetched_at = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return True
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - fetched_at).total_seconds()
    return age_seconds >= minimum_age_minutes * 60


def get_prop_movement(game_date: Optional[date] = None) -> dict:
    """
    Compare latest prop snapshot to opening lines.
    Returns dict of player -> {over_move, under_move, sharp_signal}.
    """
    d = (game_date or date.today()).isoformat()
    store = _load_store()
    day_data = store.get(d, {})
    opening_lines = day_data.get("opening", {}).get("lines", [])
    snapshots = day_data.get("snapshots", [])
    if not opening_lines or not snapshots:
        return {}

    latest_lines = snapshots[-1].get("lines", [])

    # Index: player+side+book -> odds
    def _index(lines: list) -> dict:
        idx = {}
        for ln in lines:
            key = (ln["player"], ln["side"], ln["book"])
            idx[key] = ln["odds"]
        return idx

    opening_idx = _index(opening_lines)
    latest_idx = _index(latest_lines)

    movements = {}
    for (player, side, book), current_odds in latest_idx.items():
        opening_odds = opening_idx.get((player, side, book))
        if opening_odds is None:
            continue
        move = current_odds - opening_odds
        if abs(move) < SHARP_MOVE_THRESHOLD:
            continue

        if player not in movements:
            movements[player] = {"over_move": 0, "under_move": 0, "sharp_signal": None, "books": []}

        if side == "Over":
            movements[player]["over_move"] = move
        else:
            movements[player]["under_move"] = move
        movements[player]["books"].append(book)

    # Determine sharp signal
    for player, data in movements.items():
        over_move = data["over_move"]
        under_move = data["under_move"]
        # Odds moving more negative on Over = public/sharp taking Over
        if over_move < -SHARP_MOVE_THRESHOLD:
            data["sharp_signal"] = "over"
            data["sharp_desc"] = f"Line moved {over_move:+d} on Over (sharp action)"
        elif under_move < -SHARP_MOVE_THRESHOLD:
            data["sharp_signal"] = "under"
            data["sharp_desc"] = f"Line moved {under_move:+d} on Under"

    return movements


def build_player_prop_lookup(game_date: Optional[date] = None) -> dict:
    """
    Build a player_name -> {over_odds, under_odds, line, movement} dict
    from the latest snapshot + movement data. Used by the hit props model.
    """
    lines = get_latest_prop_lines(game_date)
    movements = get_prop_movement(game_date)

    grouped: dict[str, dict] = {}
    for ln in lines:
        player = ln["player"]
        key = player_lookup_key(player)
        entry = grouped.setdefault(key, {
            "player": player,
            "line": ln.get("line", 0.5),
            "matchup": ln.get("matchup"),
            "movement": movements.get(player, {}),
            "books": {},
        })
        book = entry["books"].setdefault(ln.get("book", "unknown"), {})
        book[ln["side"].lower()] = ln["odds"]

    lookup: dict[str, dict] = {}
    for key, grouped_entry in grouped.items():
        over_prices = []
        under_prices = []
        no_vig_probabilities = []
        for book in grouped_entry["books"].values():
            over = book.get("over")
            under = book.get("under")
            if over is not None:
                over_prices.append(int(over))
            if under is not None:
                under_prices.append(int(under))
            if over is not None and under is not None:
                over_prob = _american_to_probability(over)
                under_prob = _american_to_probability(under)
                if over_prob + under_prob > 0:
                    no_vig_probabilities.append(over_prob / (over_prob + under_prob))

        value = {
            "line": grouped_entry["line"],
            "over_odds": _median_price(over_prices),
            "under_odds": _median_price(under_prices),
            "market_probability": round(statistics.median(no_vig_probabilities), 4)
            if no_vig_probabilities else None,
            "books_count": len(grouped_entry["books"]),
            "matchup": grouped_entry["matchup"],
            "movement": grouped_entry["movement"],
        }
        lookup[grouped_entry["player"]] = value
        lookup[key] = value
    return lookup


def player_lookup_key(player_name: str) -> str:
    """Normalize sportsbook and MLB player names to a stable lookup key."""
    normalized = unicodedata.normalize("NFKD", player_name or "")
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    ascii_name = re.sub(r"[^a-z0-9 ]+", " ", ascii_name)
    return " ".join(ascii_name.split())


def _american_to_probability(odds: int) -> float:
    odds = int(odds)
    if odds < 0:
        return abs(odds) / (abs(odds) + 100.0)
    return 100.0 / (odds + 100.0)


def _median_price(prices: list[int]) -> Optional[int]:
    if not prices:
        return None
    return int(round(statistics.median(prices)))


def get_event_ids_for_today(odds_list: list[dict]) -> list[str]:
    """Extract Odds API event IDs from already-fetched odds (no extra credits)."""
    ids = []
    for g in odds_list:
        eid = g.get("game_id_ext")
        if eid:
            ids.append(eid)
    return ids
