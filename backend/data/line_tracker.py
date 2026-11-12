"""
Opening line tracker.
Stores the first-seen odds for each game each day.
Compares current odds to opening odds to detect sharp money movement.

Sharp signal: line moves OPPOSITE to public betting direction.
E.g. 65% of bets on Team A, but line moves from -110 to -105 (toward Team A losing value)
= sharp money on Team B.

This module uses a local JSON file as a lightweight persistent store.
"""

import json
import os
from datetime import date, datetime
from typing import Optional

from data.storage import STORE_DIR
LINES_FILE = str(STORE_DIR / "opening_lines.json")


def _ensure_store():
    os.makedirs(os.path.dirname(LINES_FILE), exist_ok=True)
    if not os.path.exists(LINES_FILE):
        with open(LINES_FILE, "w") as f:
            json.dump({}, f)


def _load() -> dict:
    _ensure_store()
    try:
        with open(LINES_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    _ensure_store()
    with open(LINES_FILE, "w") as f:
        json.dump(data, f, indent=2)


def record_opening_lines(odds_list: list[dict], game_date: Optional[date] = None):
    """
    Call this ONCE per day (first fetch of the morning).
    Stores opening lines for each game — never overwrites existing entries.
    """
    d = (game_date or date.today()).isoformat()
    store = _load()

    if d not in store:
        store[d] = {}

    changed = False
    for game in odds_list:
        game_key = f"{game.get('away_team')}@{game.get('home_team')}"
        if game_key not in store[d]:
            store[d][game_key] = {
                "recorded_at": datetime.utcnow().isoformat(),
                "moneyline_away": game.get("best_moneyline_away"),
                "moneyline_home": game.get("best_moneyline_home"),
                "consensus_total": game.get("consensus_total"),
                "totals_by_book": {
                    book: {"over": v.get("over", {}).get("line"), "under": v.get("under", {}).get("line")}
                    for book, v in game.get("totals", {}).items()
                    if "over" in v
                },
            }
            changed = True

    # Prune entries older than 7 days
    cutoff = (date.today().toordinal() - 7)
    to_remove = [k for k in store if date.fromisoformat(k).toordinal() < cutoff]
    for k in to_remove:
        del store[k]
        changed = True

    if changed:
        _save(store)


def get_line_movement(odds_list: list[dict], game_date: Optional[date] = None) -> dict[str, dict]:
    """
    Compare current odds to opening odds.
    Returns {game_key: {ml_away_move, ml_home_move, total_move, sharp_signal}}

    ml_move: positive = line moved toward this team (more expensive to bet them now)
    sharp_signal: 'away', 'home', 'over', 'under', or None
    """
    d = (game_date or date.today()).isoformat()
    store = _load()
    day_store = store.get(d, {})

    movements = {}
    for game in odds_list:
        game_key = f"{game.get('away_team')}@{game.get('home_team')}"
        opening = day_store.get(game_key)
        if not opening:
            continue

        curr_ml_away = game.get("best_moneyline_away")
        curr_ml_home = game.get("best_moneyline_home")
        curr_total = game.get("consensus_total")
        open_ml_away = opening.get("moneyline_away")
        open_ml_home = opening.get("moneyline_home")
        open_total = opening.get("consensus_total")

        ml_away_move = _american_move(open_ml_away, curr_ml_away)
        ml_home_move = _american_move(open_ml_home, curr_ml_home)
        total_move = round((curr_total or 0) - (open_total or 0), 1) if curr_total and open_total else 0.0

        sharp_signal = _detect_sharp(ml_away_move, ml_home_move, total_move)

        movements[game_key] = {
            "open_ml_away": open_ml_away,
            "open_ml_home": open_ml_home,
            "open_total": open_total,
            "curr_ml_away": curr_ml_away,
            "curr_ml_home": curr_ml_home,
            "curr_total": curr_total,
            "ml_away_move": ml_away_move,
            "ml_home_move": ml_home_move,
            "total_move": total_move,
            "sharp_signal": sharp_signal,
            "sharp_signal_desc": _describe_sharp(sharp_signal, game, total_move),
        }

    return movements


def _american_move(open_odds: Optional[int], curr_odds: Optional[int]) -> float:
    """
    Compute how much the line moved in implied probability terms.
    Positive = line moved TOWARD this side (they're now more expensive = market moved their way).
    """
    if open_odds is None or curr_odds is None:
        return 0.0
    open_prob = _to_prob(open_odds)
    curr_prob = _to_prob(curr_odds)
    return round(curr_prob - open_prob, 4)


def _to_prob(american: int) -> float:
    if american > 0:
        return 100 / (american + 100)
    return abs(american) / (abs(american) + 100)


def _detect_sharp(ml_away_move: float, ml_home_move: float, total_move: float) -> Optional[str]:
    """
    Sharp signal when line movement is significant (≥1.5% probability shift).
    The side the line moves TOWARD has sharp action.
    """
    threshold = 0.015  # 1.5% probability shift = meaningful movement

    signals = []
    if ml_away_move >= threshold:
        signals.append(("away", abs(ml_away_move)))
    if ml_home_move >= threshold:
        signals.append(("home", abs(ml_home_move)))
    if total_move >= 0.5:
        signals.append(("over", total_move))
    elif total_move <= -0.5:
        signals.append(("under", abs(total_move)))

    if not signals:
        return None
    # Return the signal with largest movement
    signals.sort(key=lambda x: x[1], reverse=True)
    return signals[0][0]


def _describe_sharp(signal: Optional[str], game: dict, total_move: float) -> str:
    if not signal:
        return "No significant line movement"
    away = game.get("away_team", "Away")
    home = game.get("home_team", "Home")
    if signal == "away":
        return f"Sharp money on {away} (line moved their way)"
    elif signal == "home":
        return f"Sharp money on {home} (line moved their way)"
    elif signal == "over":
        return f"Total moved up {total_move:+.1f} — sharp action on Over"
    elif signal == "under":
        return f"Total moved down {total_move:+.1f} — sharp action on Under"
    return "Line movement detected"


def apply_line_movement_to_games(games: list[dict], movements: dict[str, dict]) -> list[dict]:
    """Attach line movement data to each enriched game."""
    for game in games:
        away = game.get("away_team", "")
        home = game.get("home_team", "")
        game_key = f"{away}@{home}"
        movement = movements.get(game_key)
        if movement:
            game["line_movement"] = movement
    return games
