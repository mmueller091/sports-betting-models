"""
Results tracker.
Records every recommended pick, updates a closing-line proxy on each refresh,
grades picks against final scores, and reports ROI / hit rate / calibration / CLV.

Store: backend/data_store/picks_history.json
"""

import asyncio
import json
import os
from datetime import date, timedelta

from data.mlb_stats import get_final_scores_for_date, get_boxscore_hits
from data.odds import american_to_implied_prob

from data.storage import STORE_DIR
_STORE_PATH = str(STORE_DIR / "picks_history.json")
FLAT_STAKE = 100.0
VOID_AFTER_DAYS = 5


def _load() -> dict:
    try:
        with open(_STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(store: dict) -> None:
    os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
    with open(_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=1)


def _bet_key(bet: dict) -> str:
    return f"{bet.get('bet_type')}:{bet.get('matchup')}:{bet.get('pick')}"


def record_picks(target_date: date, bets: list[dict] = None, props: list[dict] = None) -> None:
    """
    Persist today's recommended picks. First-seen odds are kept as the
    "bet placed" price; re-records only update the closing-line proxy.
    """
    store = _load()
    day = store.setdefault(target_date.isoformat(), {"bets": {}, "props": {}})

    for bet in bets or []:
        key = _bet_key(bet)
        if key not in day["bets"]:
            day["bets"][key] = {
                "bet_type": bet.get("bet_type"),
                "pick": bet.get("pick"),
                "team": bet.get("team"),
                "matchup": bet.get("matchup"),
                "odds": bet.get("odds"),
                "our_prob": bet.get("our_prob"),
                "market_prob": bet.get("market_prob"),
                "edge": bet.get("edge"),
                "market_line": bet.get("market_line"),
                "closing_odds": bet.get("odds"),
                "result": None,
            }

    for prop in props or []:
        pid = str(prop.get("batter_id"))
        if pid and pid != "None" and pid not in day["props"]:
            day["props"][pid] = {
                "name": prop.get("name"),
                "team": prop.get("team"),
                "matchup": prop.get("matchup"),
                "hit_probability": prop.get("hit_probability"),
                "prop_over_odds": prop.get("prop_over_odds"),
                "closing_over_odds": prop.get("prop_over_odds"),
                "result": None,
            }

    _save(store)


def update_closing_lines(odds_list: list[dict], target_date: date) -> None:
    """
    Refresh the closing-line proxy for every recorded bet using the latest
    odds snapshot. The last update before first pitch ≈ the closing line.
    """
    store = _load()
    day = store.get(target_date.isoformat())
    if not day:
        return

    index = {}
    for g in odds_list:
        index[(g["away_team"], g["home_team"])] = g
        index[(g["away_team"].split()[-1], g["home_team"].split()[-1])] = g

    for key, bet in day.get("bets", {}).items():
        if bet.get("result"):
            continue
        matchup = bet.get("matchup", "")
        if " @ " not in matchup:
            continue
        away, home = matchup.split(" @ ", 1)
        game = index.get((away, home)) or index.get((away.split()[-1], home.split()[-1]))
        if not game:
            continue

        bet_type = bet.get("bet_type")
        pick = bet.get("pick", "")
        if bet_type == "Moneyline":
            closing = game.get("best_moneyline_away") if bet.get("team") == away else game.get("best_moneyline_home")
            if closing is not None:
                bet["closing_odds"] = closing
        elif bet_type == "Total":
            side = "over" if pick.startswith("Over") else "under"
            line = bet.get("market_line")
            for book_totals in game.get("totals", {}).values():
                entry = book_totals.get(side)
                if entry and entry.get("line") == line:
                    bet["closing_odds"] = entry.get("odds", bet.get("closing_odds"))
                    break
        elif bet_type == "Spread":
            side = "away" if bet.get("team") == away else "home"
            for book_lines in game.get("run_lines", {}).values():
                entry = book_lines.get(side)
                if entry:
                    bet["closing_odds"] = entry.get("odds", bet.get("closing_odds"))
                    break

    _save(store)


def update_prop_closing(prop_lookup: dict, target_date: date) -> None:
    """Refresh closing odds for recorded hit props from the latest prop lines."""
    store = _load()
    day = store.get(target_date.isoformat())
    if not day:
        return
    by_name = {p.get("name"): p for p in day.get("props", {}).values()}
    for player_name, prop in (prop_lookup or {}).items():
        entry = by_name.get(player_name)
        if entry and not entry.get("result") and prop.get("over_odds") is not None:
            entry["closing_over_odds"] = prop["over_odds"]
    _save(store)


async def grade_pending() -> int:
    """
    Grade all ungraded picks for past dates against final scores.
    Returns the number of picks graded in this pass.
    """
    store = _load()
    today = date.today()
    graded_count = 0

    for date_str, day in store.items():
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            continue
        if d >= today:
            continue  # Games may still be in progress

        pending_bets = [b for b in day.get("bets", {}).values() if not b.get("result")]
        pending_props = [p for p in day.get("props", {}).values() if not p.get("result")]
        if not pending_bets and not pending_props:
            continue

        try:
            results = await get_final_scores_for_date(d)
        except Exception:
            continue
        by_teams = {}
        for (away, home), entry in results.items():
            by_teams[(away, home)] = entry
            by_teams[(away.split()[-1], home.split()[-1])] = entry

        stale = (today - d).days > VOID_AFTER_DAYS

        for bet in pending_bets:
            result = _grade_bet(bet, by_teams)
            if result:
                bet["result"] = result
                bet["profit"] = _profit(result, bet.get("odds"))
                graded_count += 1
            elif stale:
                bet["result"] = "void"
                bet["profit"] = 0.0

        if pending_props:
            graded_count += await _grade_props(day, by_teams, stale)

    _save(store)
    return graded_count


def _grade_bet(bet: dict, by_teams: dict) -> str | None:
    matchup = bet.get("matchup", "")
    if " @ " not in matchup:
        return None
    away, home = matchup.split(" @ ", 1)
    game = by_teams.get((away, home)) or by_teams.get((away.split()[-1], home.split()[-1]))
    if not game:
        return None

    away_score = game["away_score"]
    home_score = game["home_score"]
    bet_type = bet.get("bet_type")
    pick = bet.get("pick", "")
    team = bet.get("team", "")

    if bet_type == "Moneyline":
        team_is_away = team == game["away_team"] or team.split()[-1] == game["away_team"].split()[-1]
        team_score, opp_score = (away_score, home_score) if team_is_away else (home_score, away_score)
        return "win" if team_score > opp_score else "loss"

    if bet_type == "Total":
        line = bet.get("market_line")
        if line is None:
            return None
        total = away_score + home_score
        if total == line:
            return "push"
        is_over = pick.startswith("Over")
        return "win" if (total > line) == is_over else "loss"

    if bet_type == "Spread":
        try:
            # pick format: "Team Name -1.5 (book)"
            line = float(pick.rsplit("(", 1)[0].strip().split()[-1])
        except (ValueError, IndexError):
            return None
        team_is_away = team == game["away_team"] or team.split()[-1] == game["away_team"].split()[-1]
        margin = (away_score - home_score) if team_is_away else (home_score - away_score)
        adjusted = margin + line
        if adjusted == 0:
            return "push"
        return "win" if adjusted > 0 else "loss"

    return None


async def _grade_props(day: dict, by_teams: dict, stale: bool) -> int:
    graded = 0
    pending = [p for p in day.get("props", {}).values() if not p.get("result")]

    game_ids = {}
    for prop in pending:
        matchup = prop.get("matchup", "")
        if " @ " not in matchup:
            continue
        away, home = matchup.split(" @ ", 1)
        game = by_teams.get((away, home)) or by_teams.get((away.split()[-1], home.split()[-1]))
        if game:
            game_ids[matchup] = game["game_id"]

    boxscores = {}
    tasks = {m: get_boxscore_hits(gid) for m, gid in game_ids.items()}
    if tasks:
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for matchup, result in zip(tasks.keys(), results):
            if not isinstance(result, Exception):
                boxscores[matchup] = result

    for pid, prop in day.get("props", {}).items():
        if prop.get("result"):
            continue
        hits_by_batter = boxscores.get(prop.get("matchup"))
        if hits_by_batter is None:
            if stale:
                prop["result"] = "void"
                prop["profit"] = 0.0
            continue
        batter_hits = hits_by_batter.get(int(pid))
        if batter_hits is None:
            # Batter didn't play (scratched) — void
            prop["result"] = "void"
            prop["profit"] = 0.0
            continue
        prop["hits"] = batter_hits
        prop["result"] = "win" if batter_hits >= 1 else "loss"
        prop["profit"] = _profit(prop["result"], prop.get("prop_over_odds"))
        graded += 1

    return graded


def _profit(result: str, odds) -> float | None:
    """Flat-stake profit. None when no odds were available (track hit rate only)."""
    if result == "push" or result == "void":
        return 0.0
    if odds is None:
        return None
    if result == "win":
        payout = odds if odds > 0 else 10000 / abs(odds)
        return round(payout, 2)
    return -FLAT_STAKE


def _clv_pct(pick_odds, closing_odds) -> float | None:
    """Positive = we beat the closing line (got a better price than close)."""
    if pick_odds is None or closing_odds is None or pick_odds == closing_odds:
        return 0.0 if pick_odds == closing_odds else None
    return round(
        (american_to_implied_prob(closing_odds) - american_to_implied_prob(pick_odds)) * 100, 2
    )


def performance_summary() -> dict:
    store = _load()

    bet_rows = []
    prop_rows = []
    for date_str in sorted(store.keys()):
        day = store[date_str]
        for bet in day.get("bets", {}).values():
            bet_rows.append({**bet, "date": date_str})
        for pid, prop in day.get("props", {}).items():
            prop_rows.append({**prop, "batter_id": pid, "date": date_str})

    graded_bets = [b for b in bet_rows if b.get("result") in ("win", "loss", "push")]
    graded_props = [p for p in prop_rows if p.get("result") in ("win", "loss")]

    bets_summary = _summarize_bets(graded_bets)
    bets_summary["by_type"] = {
        t: _summarize_bets([b for b in graded_bets if b.get("bet_type") == t])
        for t in sorted({b.get("bet_type") for b in graded_bets if b.get("bet_type")})
    }

    props_wins = sum(1 for p in graded_props if p["result"] == "win")
    props_profit = [p["profit"] for p in graded_props if p.get("profit") is not None]
    props_summary = {
        "count": len(graded_props),
        "wins": props_wins,
        "losses": len(graded_props) - props_wins,
        "hit_rate": round(props_wins / len(graded_props), 3) if graded_props else None,
        "avg_predicted_prob": round(
            sum(p.get("hit_probability", 0) for p in graded_props) / len(graded_props), 3
        ) if graded_props else None,
        "profit": round(sum(props_profit), 2) if props_profit else None,
        "roi_pct": round(sum(props_profit) / (len(props_profit) * FLAT_STAKE) * 100, 2) if props_profit else None,
        "calibration": _calibration(graded_props, "hit_probability"),
    }

    recent = sorted(
        [b for b in bet_rows if b.get("result")] + [
            {**p, "pick": f"{p.get('name')} 1+ hit", "bet_type": "Hit Prop", "odds": p.get("prop_over_odds")}
            for p in prop_rows if p.get("result")
        ],
        key=lambda r: r["date"],
        reverse=True,
    )[:30]

    pending = sum(1 for b in bet_rows if not b.get("result")) + sum(1 for p in prop_rows if not p.get("result"))

    return {
        "bets": bets_summary,
        "props": props_summary,
        "recent": recent,
        "pending_count": pending,
        "days_tracked": len(store),
    }


def _summarize_bets(bets: list[dict]) -> dict:
    wins = sum(1 for b in bets if b["result"] == "win")
    losses = sum(1 for b in bets if b["result"] == "loss")
    pushes = sum(1 for b in bets if b["result"] == "push")
    profits = [b["profit"] for b in bets if b.get("profit") is not None]
    clvs = [c for c in (_clv_pct(b.get("odds"), b.get("closing_odds")) for b in bets) if c is not None]
    settled = wins + losses
    return {
        "count": len(bets),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_rate": round(wins / settled, 3) if settled else None,
        "profit": round(sum(profits), 2) if profits else 0.0,
        "roi_pct": round(sum(profits) / (len(profits) * FLAT_STAKE) * 100, 2) if profits else None,
        "avg_clv_pct": round(sum(clvs) / len(clvs), 2) if clvs else None,
        "calibration": _calibration(bets, "our_prob"),
    }


def _calibration(rows: list[dict], prob_field: str) -> list[dict]:
    """Bucket predicted probability vs actual win rate."""
    buckets = [(0.0, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 1.01)]
    out = []
    for low, high in buckets:
        in_bucket = [
            r for r in rows
            if r.get(prob_field) is not None and low <= r[prob_field] < high
            and r.get("result") in ("win", "loss")
        ]
        if not in_bucket:
            continue
        wins = sum(1 for r in in_bucket if r["result"] == "win")
        out.append({
            "range": f"{int(low*100)}–{int(min(high,1.0)*100)}%",
            "count": len(in_bucket),
            "predicted": round(sum(r[prob_field] for r in in_bucket) / len(in_bucket), 3),
            "actual": round(wins / len(in_bucket), 3),
        })
    return out
