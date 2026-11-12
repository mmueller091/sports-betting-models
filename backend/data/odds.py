"""
The Odds API fetcher.
Free tier at https://the-odds-api.com — 500 requests/month.
Set ODDS_API_KEY in your .env file.
"""

import os
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import httpx
from typing import Optional
from data.source_health import warn

ODDS_BASE = "https://api.the-odds-api.com/v4"
SPORT = "baseball_mlb"
from data.storage import STORE_DIR
CACHE_DIR = STORE_DIR / "odds_cache"
ODDS_STATUS = {}


def _api_key() -> str:
    key = os.getenv("ODDS_API_KEY", "")
    if not key:
        raise ValueError("ODDS_API_KEY not set in environment. Add it to your .env file.")
    return key


async def get_game_odds(regions: str = "us", markets: str = "h2h,spreads,totals", sport: str = SPORT) -> list[dict]:
    """
    Fetch moneyline (h2h), run line (spreads), and total (totals) odds.
    Returns a list of games with their odds across books.
    sport: 'baseball_mlb' or 'icehockey_nhl'
    """
    cache_hours = float(os.getenv("ODDS_CACHE_HOURS", "0"))
    cache_key = hashlib.sha256(f"{sport}:{regions}:{markets}".encode()).hexdigest()[:16]
    cache_path = CACHE_DIR / f"{cache_key}.json"
    cached = None
    if cache_hours > 0:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if time.time() - cached["timestamp"] < cache_hours * 3600:
                ODDS_STATUS[sport] = {"updated_at": cached["updated_at"], "cached": True}
                return cached["games"]
        except (OSError, ValueError, KeyError, TypeError):
            cached = None

    url = f"{ODDS_BASE}/sports/{sport}/odds"
    params = {
        "apiKey": _api_key(),
        "regions": regions,
        "markets": markets,
        "oddsFormat": "american",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        games = [_parse_odds_game(g) for g in resp.json()]
    except Exception:
        if cached:
            warn("Market odds refresh failed; older cached prices are shown")
            ODDS_STATUS[sport] = {"updated_at": cached["updated_at"], "cached": True, "stale": True}
            return cached["games"]
        raise
    updated_at = datetime.now(timezone.utc).isoformat()
    ODDS_STATUS[sport] = {"updated_at": updated_at, "cached": False}
    if cache_hours > 0:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"timestamp": time.time(), "updated_at": updated_at, "games": games}), encoding="utf-8")
    return games


def _parse_odds_game(g: dict) -> dict:
    """Parse a single game's odds into a clean structure."""
    game = {
        "game_id_ext": g["id"],
        "sport": g["sport_key"],
        "commence_time": g["commence_time"],
        "away_team": g["away_team"],
        "home_team": g["home_team"],
        "moneylines": {},   # book -> {away: int, home: int}
        "run_lines": {},    # book -> {away: {line, odds}, home: {line, odds}}
        "totals": {},       # book -> {over: {line, odds}, under: {line, odds}}
        "best_moneyline_away": None,
        "best_moneyline_home": None,
        "consensus_total": None,
        "line_movement": None,
        "market_updated_at": {},
        "book_titles": {},
    }

    for bookmaker in g.get("bookmakers", []):
        book = bookmaker["key"]
        game["book_titles"][book] = bookmaker.get("title", book)
        for market in bookmaker.get("markets", []):
            game["market_updated_at"].setdefault(market["key"], {})[book] = market.get("last_update") or bookmaker.get("last_update")
            if market["key"] == "h2h":
                ml = {}
                for outcome in market["outcomes"]:
                    if outcome["name"] == g["away_team"]:
                        ml["away"] = outcome["price"]
                    elif outcome["name"] == g["home_team"]:
                        ml["home"] = outcome["price"]
                game["moneylines"][book] = ml

            elif market["key"] == "spreads":
                rl = {}
                for outcome in market["outcomes"]:
                    entry = {"line": outcome["point"], "odds": outcome["price"]}
                    if outcome["name"] == g["away_team"]:
                        rl["away"] = entry
                    elif outcome["name"] == g["home_team"]:
                        rl["home"] = entry
                game["run_lines"][book] = rl

            elif market["key"] == "totals":
                tot = {}
                for outcome in market["outcomes"]:
                    entry = {"line": outcome["point"], "odds": outcome["price"]}
                    if outcome["name"] == "Over":
                        tot["over"] = entry
                    elif outcome["name"] == "Under":
                        tot["under"] = entry
                game["totals"][book] = tot

    # Best available moneyline (highest payout for each side)
    away_lines = [v["away"] for v in game["moneylines"].values() if "away" in v]
    home_lines = [v["home"] for v in game["moneylines"].values() if "home" in v]
    if away_lines:
        game["best_moneyline_away"] = max(away_lines)
    if home_lines:
        game["best_moneyline_home"] = max(home_lines)

    # Consensus total (most common line across books)
    all_totals = [v["over"]["line"] for v in game["totals"].values() if "over" in v]
    if all_totals:
        game["consensus_total"] = round(sum(all_totals) / len(all_totals), 1)

    game["market_metrics"] = _build_market_metrics(game)

    return game


async def get_props(event_id: str, markets: str = "batter_hits") -> list[dict]:
    """
    Fetch player prop odds for a specific game event.
    market options: batter_hits, batter_total_bases, pitcher_strikeouts, etc.
    """
    url = f"{ODDS_BASE}/sports/{SPORT}/events/{event_id}/odds"
    params = {
        "apiKey": _api_key(),
        "regions": "us",
        "markets": markets,
        "oddsFormat": "american",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
    raw = resp.json()
    return _parse_props(raw)


def _parse_props(data: dict) -> list[dict]:
    """Parse player props into [{player, line, over_odds, under_odds, book}]"""
    props = []
    for bookmaker in data.get("bookmakers", []):
        book = bookmaker["key"]
        for market in bookmaker.get("markets", []):
            players: dict[str, dict] = {}
            for outcome in market.get("outcomes", []):
                name = outcome["description"]
                if name not in players:
                    players[name] = {"player": name, "book": book, "market": market["key"]}
                if outcome["name"] == "Over":
                    players[name]["line"] = outcome.get("point", 0.5)
                    players[name]["over_odds"] = outcome["price"]
                elif outcome["name"] == "Under":
                    players[name]["under_odds"] = outcome["price"]
            props.extend(players.values())
    return props


def american_to_implied_prob(american: int) -> float:
    """Convert American odds to implied probability (0–1)."""
    if american > 0:
        return 100 / (american + 100)
    else:
        return abs(american) / (abs(american) + 100)


def implied_prob_to_american(prob: float) -> int:
    """Convert implied probability to American odds."""
    if prob >= 0.5:
        return round(-prob / (1 - prob) * 100)
    else:
        return round((1 - prob) / prob * 100)


def no_vig_prob(odds_a: int, odds_b: int) -> tuple[float, float]:
    """Remove the book's vig to get fair probabilities for two-outcome markets."""
    p_a = american_to_implied_prob(odds_a)
    p_b = american_to_implied_prob(odds_b)
    total = p_a + p_b
    return p_a / total, p_b / total


def _build_market_metrics(game: dict) -> dict:
    moneyline_pairs = []
    for book, prices in game.get("moneylines", {}).items():
        away = prices.get("away")
        home = prices.get("home")
        if away is None or home is None:
            continue
        away_prob, home_prob = no_vig_prob(away, home)
        moneyline_pairs.append({
            "book": book,
            "away_prob": away_prob,
            "home_prob": home_prob,
            "hold": round(
                american_to_implied_prob(away) + american_to_implied_prob(home) - 1,
                4,
            ),
        })

    totals_pairs = []
    for book, prices in game.get("totals", {}).items():
        over = prices.get("over", {})
        under = prices.get("under", {})
        if "odds" not in over or "odds" not in under or "line" not in over:
            continue
        over_prob, under_prob = no_vig_prob(over["odds"], under["odds"])
        totals_pairs.append({
            "book": book,
            "line": over["line"],
            "over_prob": over_prob,
            "under_prob": under_prob,
            "hold": round(
                american_to_implied_prob(over["odds"]) + american_to_implied_prob(under["odds"]) - 1,
                4,
            ),
        })

    away_consensus = _avg([p["away_prob"] for p in moneyline_pairs], 0.5)
    home_consensus = _avg([p["home_prob"] for p in moneyline_pairs], 0.5)
    over_consensus = _avg([p["over_prob"] for p in totals_pairs], 0.5)
    under_consensus = _avg([p["under_prob"] for p in totals_pairs], 0.5)

    best_away = game.get("best_moneyline_away")
    best_home = game.get("best_moneyline_home")

    return {
        "books_h2h": len(moneyline_pairs),
        "books_totals": len(totals_pairs),
        "consensus_prob_away": round(away_consensus, 4),
        "consensus_prob_home": round(home_consensus, 4),
        "best_vs_consensus_away": round(
            away_consensus - american_to_implied_prob(best_away),
            4,
        ) if best_away is not None else 0.0,
        "best_vs_consensus_home": round(
            home_consensus - american_to_implied_prob(best_home),
            4,
        ) if best_home is not None else 0.0,
        "away_dispersion": round(_dispersion([p["away_prob"] for p in moneyline_pairs]), 4),
        "home_dispersion": round(_dispersion([p["home_prob"] for p in moneyline_pairs]), 4),
        "avg_moneyline_hold": round(_avg([p["hold"] for p in moneyline_pairs], 0.0), 4),
        "consensus_total_over_prob": round(over_consensus, 4),
        "consensus_total_under_prob": round(under_consensus, 4),
        "total_line_dispersion": round(_dispersion([p["line"] for p in totals_pairs]), 3),
        "avg_totals_hold": round(_avg([p["hold"] for p in totals_pairs], 0.0), 4),
    }


def _avg(values: list[float], default: float = 0.0) -> float:
    if not values:
        return default
    return sum(values) / len(values)


def _dispersion(values: list[float]) -> float:
    if not values:
        return 0.0
    return max(values) - min(values)
