"""
NHL game bets model.
Scores moneylines, puck lines, and totals, then returns the highest-EV bets.

Inputs:
- MoneyPuck / NST xG
- Goalie quality including GSAx
- Recent form and schedule density
- Injury impact
- Quoted bookmaker prices
"""

from functools import lru_cache
from math import exp, isfinite
from datetime import datetime, timezone

from data.odds import american_to_implied_prob
from data.source_health import warn

MODEL_VERSION = "nhl-2026-11-12.correctness.1"
LEAGUE_AVG_GOALS = 6.10
HOME_WIN_ADVANTAGE = 0.055


def _number(value, default):
    try:
        value = float(value)
        return value if isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _recent_win_pct(recent_games: list[dict]) -> float:
    if not recent_games:
        return 0.500
    wins = sum(1 for game in recent_games if game.get("win"))
    return round(wins / len(recent_games), 3)


def _recent_goal_diff(recent_games: list[dict]) -> float:
    if not recent_games:
        return 0.0
    diff = sum(game.get("goals_for", 0) - game.get("goals_against", 0) for game in recent_games)
    return round(diff / len(recent_games), 2)


def _goalie_score(team_stats: dict) -> float:
    """
    Lower is better. Positive values mean the goalie setup should allow more goals.
    """
    goalie = team_stats.get("starting_goalie", {})
    if not goalie:
        return 0.0

    save_pct = _number(goalie.get("save_pct"), 0.906)
    gaa = _number(goalie.get("gaa"), 3.00)
    xg_stats = goalie.get("xg_stats", {})
    gsax = _number(xg_stats.get("gsax"), 0.0)
    games_played = max(_number(xg_stats.get("games_played", goalie.get("games_started")), 1), 1)
    hd_sv_pct = _number(xg_stats.get("hd_sv_pct"), 0.82)
    quality_starts = _number(xg_stats.get("quality_starts_pct"), 0.5)

    save_adj = (0.906 - save_pct) * 20
    gaa_adj = (gaa - 3.00) * 0.15
    gsax_adj = -(gsax / games_played) * 1.1
    hd_adj = (0.82 - hd_sv_pct) * 7.0
    qs_adj = -(quality_starts - 0.5) * 0.8

    return round(save_adj + gaa_adj + gsax_adj + hd_adj + qs_adj, 3)


def estimate_nhl_total(game: dict) -> float:
    """Estimate the fair total for the game."""
    base = LEAGUE_AVG_GOALS
    away_standings = game.get("away_standings", {})
    home_standings = game.get("home_standings", {})
    away_stats = game.get("away_team_stats", {})
    home_stats = game.get("home_team_stats", {})

    away_gf = _number(away_standings.get("goals_for_per_game"), base / 2)
    home_gf = _number(home_standings.get("goals_for_per_game"), base / 2)
    away_ga = _number(away_standings.get("goals_against_per_game"), base / 2)
    home_ga = _number(home_standings.get("goals_against_per_game"), base / 2)

    away_xgf = _number(away_stats.get("xg", {}).get("xgf_per_game"), away_gf)
    home_xgf = _number(home_stats.get("xg", {}).get("xgf_per_game"), home_gf)
    away_xga = _number(away_stats.get("xg", {}).get("xga_per_game"), away_ga)
    home_xga = _number(home_stats.get("xg", {}).get("xga_per_game"), home_ga)

    expected = ((away_xgf + home_xga) / 2 + (home_xgf + away_xga) / 2)
    our_total = round(base * 0.25 + expected * 0.75, 2)

    away_goalie_adj = _goalie_score(away_stats)
    home_goalie_adj = _goalie_score(home_stats)
    our_total += (away_goalie_adj + home_goalie_adj) * 0.18

    away_schedule = game.get("away_schedule_spot", {})
    home_schedule = game.get("home_schedule_spot", {})
    our_total -= _schedule_total_penalty(away_schedule)
    our_total -= _schedule_total_penalty(home_schedule)

    away_inj = game.get("away_injury_impact", {}).get("total_xg_lost", 0.0)
    home_inj = game.get("home_injury_impact", {}).get("total_xg_lost", 0.0)
    our_total -= (away_inj + home_inj) * 0.10

    return round(max(our_total, 3.5), 2)


def estimate_nhl_win_prob(game: dict) -> tuple[float, float]:
    """Estimate fair win probability for away and home teams."""
    away_standings = game.get("away_standings", {})
    home_standings = game.get("home_standings", {})
    away_stats = game.get("away_team_stats", {})
    home_stats = game.get("home_team_stats", {})

    away_xgf = away_stats.get("xg", {}).get("xgf_pct")
    home_xgf = home_stats.get("xg", {}).get("xgf_pct")
    away_nst = away_stats.get("nst", {}).get("xgf_pct")
    home_nst = home_stats.get("nst", {}).get("xgf_pct")

    if away_xgf and home_xgf:
        away_win_pct = away_xgf / 100
        home_win_pct = home_xgf / 100
    elif away_nst and home_nst:
        away_win_pct = away_nst / 100
        home_win_pct = home_nst / 100
    else:
        away_win_pct = away_standings.get("win_pct", 0.500)
        home_win_pct = home_standings.get("win_pct", 0.500)

    a, b = away_win_pct, home_win_pct
    if a + b - 2 * a * b == 0:
        log5_away = 0.5
    else:
        log5_away = (a - a * b) / (a + b - 2 * a * b)

    home_prob = (1 - log5_away) + HOME_WIN_ADVANTAGE
    away_prob = 1 - home_prob

    away_l10 = _recent_win_pct(game.get("away_recent_form", []))
    home_l10 = _recent_win_pct(game.get("home_recent_form", []))
    goal_diff_adj = (_recent_goal_diff(game.get("home_recent_form", [])) - _recent_goal_diff(game.get("away_recent_form", []))) * 0.015
    form_diff = (home_l10 - away_l10) * 0.10
    home_prob += form_diff + goal_diff_adj
    away_prob -= form_diff + goal_diff_adj

    away_goalie_adj = _goalie_score(away_stats)
    home_goalie_adj = _goalie_score(home_stats)
    net_goalie = (home_goalie_adj - away_goalie_adj) * 0.035
    home_prob -= net_goalie
    away_prob += net_goalie

    away_schedule = game.get("away_schedule_spot", {})
    home_schedule = game.get("home_schedule_spot", {})
    schedule_diff = _schedule_win_penalty(home_schedule) - _schedule_win_penalty(away_schedule)
    home_prob -= schedule_diff
    away_prob += schedule_diff

    away_inj = game.get("away_injury_impact", {}).get("total_xg_lost", 0.0)
    home_inj = game.get("home_injury_impact", {}).get("total_xg_lost", 0.0)
    injury_diff = (home_inj - away_inj) * 0.025
    home_prob -= injury_diff
    away_prob += injury_diff

    away_pp = _number(away_standings.get("pp_pct"), 20.0)
    home_pp = _number(home_standings.get("pp_pct"), 20.0)
    pp_diff = (home_pp - away_pp) / 100 * 0.15
    home_prob += pp_diff
    away_prob -= pp_diff

    pdo_diff = (_number(home_stats.get("xg", {}).get("pdo"), 1.0) - _number(away_stats.get("xg", {}).get("pdo"), 1.0))
    home_prob -= pdo_diff * 0.04
    away_prob += pdo_diff * 0.04

    home_prob = max(0.24, min(0.76, home_prob))
    away_prob = 1 - home_prob
    return round(away_prob, 3), round(home_prob, 3)


def estimate_puckline_cover_probability(
    away_prob: float,
    home_prob: float,
    total_goals: float,
    side: str,
    line: float,
) -> float:
    distribution = nhl_score_distribution(home_prob, total_goals)
    return round(market_probabilities(distribution, "spread", side, line)["win"], 6)


def calculate_ev(our_prob: float, american_odds: int, push_prob: float = 0.0) -> float:
    if american_odds is None:
        return -999.0
    profit_if_win = american_odds if american_odds > 0 else 10000 / abs(american_odds)
    return round((our_prob * profit_if_win) - ((1 - our_prob - push_prob) * 100), 2)


def _poisson_probabilities(mean: float) -> tuple[float, ...]:
    probabilities = [exp(-mean)]
    total = probabilities[0]
    while total < 1 - 1e-13:
        probabilities.append(probabilities[-1] * mean / len(probabilities))
        total += probabilities[-1]
        if len(probabilities) > 250:
            raise ValueError("NHL expected goal rate is outside the supported range")
    return tuple(probabilities)


def _regulation_rates(total: float, target_home_win: float):
    """Find a Poisson goal split with 50/50 resolution of regulation ties."""
    lower, upper = 0.0, total
    for _ in range(38):
        home_rate = (lower + upper) / 2
        home = _poisson_probabilities(home_rate)
        away = _poisson_probabilities(total - home_rate)
        away_below = 0.0
        home_win = tie = 0.0
        for goals, home_p in enumerate(home):
            away_equal = away[goals] if goals < len(away) else 0.0
            home_win += home_p * away_below
            tie += home_p * away_equal
            away_below += away_equal
        if home_win + tie / 2 < target_home_win:
            lower = home_rate
        else:
            upper = home_rate
    return home, away, tie


@lru_cache(maxsize=256)
def nhl_score_distribution(home_win_probability: float, expected_total: float) -> tuple:
    """Return (home final goals, away final goals, probability) outcomes.

    This is a coherent provisional distribution, not a fitted NHL model.
    Independent Poisson regulation goals are numerically matched to the existing
    win forecast and final-score total. Every regulation tie resolves 50/50 with
    one deciding scoreboard goal, matching full-game OT/shootout settlement.
    Empty-net dynamics and a learned overtime advantage are not inferred here.
    """
    if not (isfinite(home_win_probability) and 0 < home_win_probability < 1):
        raise ValueError("Home win probability must be between zero and one")
    if not (isfinite(expected_total) and expected_total > 1):
        raise ValueError("Expected NHL final total must exceed one goal")
    lower, upper = expected_total - 1, expected_total
    for _ in range(30):
        regulation_total = (lower + upper) / 2
        home, away, tie = _regulation_rates(regulation_total, home_win_probability)
        if regulation_total + tie < expected_total:
            lower = regulation_total
        else:
            upper = regulation_total
    outcomes = {}
    for h, hp in enumerate(home):
        for a, ap in enumerate(away):
            probability = hp * ap
            if h == a:
                for final in ((h + 1, a), (h, a + 1)):
                    outcomes[final] = outcomes.get(final, 0.0) + probability / 2
            else:
                outcomes[(h, a)] = outcomes.get((h, a), 0.0) + probability
    mass = sum(outcomes.values())
    distribution = tuple((h, a, p / mass) for (h, a), p in sorted(outcomes.items()))
    realized_home_win = sum(p for h, a, p in distribution if h > a)
    realized_total = sum((h + a) * p for h, a, p in distribution)
    if abs(realized_home_win - home_win_probability) > 1e-8 or abs(realized_total - expected_total) > 1e-7:
        raise ValueError("Requested NHL win probability and total are incompatible with this score distribution")
    return distribution


def market_probabilities(distribution: tuple, market: str, side: str, line=None) -> dict:
    """Settle each final-score outcome under the same win/push/loss rules."""
    probabilities = {"win": 0.0, "push": 0.0, "loss": 0.0}
    if market not in ("moneyline", "spread", "total"):
        raise ValueError("Unsupported NHL market")
    if side not in (("over", "under") if market == "total" else ("home", "away")):
        raise ValueError("Unsupported NHL market side")
    for home, away, probability in distribution:
        if market == "total":
            difference = (home + away - line) * (1 if side == "over" else -1)
        else:
            difference = (home - away) * (1 if side == "home" else -1)
            if market == "spread":
                difference += line
        result = "push" if abs(difference) < 1e-12 else "win" if difference > 0 else "loss"
        probabilities[result] += probability
    return probabilities


def find_nhl_best_bets(snapshot: dict, top_n: int = 5, now: datetime | None = None) -> list[dict]:
    candidates = []
    now = now or datetime.now(timezone.utc)
    for game in snapshot.get("games", []):
        try:
            start = datetime.fromisoformat(game["game_time"].replace("Z", "+00:00"))
            if start.tzinfo is None or start <= now or game.get("status") in ("LIVE", "CRIT", "FINAL", "OFF"):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        odds = game.get("odds", {})
        if not odds:
            continue
        away, home = game["away_team"], game["home_team"]
        matchup = f"{away} @ {home}"
        away_prob, home_prob = estimate_nhl_win_prob(game)
        our_total = estimate_nhl_total(game)
        distribution = nhl_score_distribution(home_prob, our_total)
        common = {
            "game_id": game.get("game_id"), "matchup": matchup,
            "venue": game.get("venue", ""), "game_time": game.get("game_time", ""),
            "away_goalie": game.get("away_team_stats", {}).get("starting_goalie", {}).get("name", "TBD"),
            "home_goalie": game.get("home_team_stats", {}).get("starting_goalie", {}).get("name", "TBD"),
            "away_back_to_back": game.get("away_back_to_back", False),
            "home_back_to_back": game.get("home_back_to_back", False),
            "model_version": MODEL_VERSION,
            "odds_event_id": odds.get("game_id_ext"),
            "model_source": "provisional_score_distribution",
            "probability_method": "Poisson regulation scores; ties resolve equally with one deciding goal. Not historically calibrated.",
        }

        def add_candidate(market, side, line, side_odds, book):
            side_odds = _number(side_odds, None)
            if side_odds is None or abs(side_odds) < 100:
                return
            if side_odds.is_integer():
                side_odds = int(side_odds)
            if market != "moneyline":
                line = _number(line, None)
                if line is None or abs(line * 2 - round(line * 2)) > 1e-8:
                    return
                if market == "total" and line <= 0:
                    return
            probabilities = market_probabilities(distribution, market, side, line)
            # The published probabilities also drive published EV, avoiding a
            # hidden market-movement or best-price probability bonus.
            our_prob = round(probabilities["win"], 6)
            push_prob = round(probabilities["push"], 6)
            loss_prob = round(1 - our_prob - push_prob, 6)
            implied = american_to_implied_prob(side_odds)
            break_even = implied * (1 - push_prob)
            edge = our_prob - break_even
            ev = calculate_ev(our_prob, side_odds, push_prob)
            if ev <= 0 or edge <= (0.0 if market == "moneyline" else 0.02):
                return
            team = away if side == "away" else home
            market_key = {"moneyline": "h2h", "spread": "spreads", "total": "totals"}[market]
            quote_at = odds.get("market_updated_at", {}).get(market_key, {}).get(book)
            if not quote_at:
                warn("NHL bookmaker quote timestamps unavailable; cached prices require confirmation")
            candidate = {
                **common,
                "_market_key": f"{market}:{game.get('game_id', matchup)}:{side}",
                "bet_type": {"moneyline": "Moneyline", "spread": "Spread", "total": "Total"}[market],
                "market": market, "side": side, "line": line,
                "team": team, "odds": side_odds, "book": book,
                "odds_updated_at": quote_at,
                "our_prob": our_prob, "push_probability": push_prob, "loss_probability": loss_prob,
                "market_prob": round(break_even, 6),
                "quoted_implied_probability": round(implied, 6),
                "edge": round(edge, 6), "ev_per_100": ev,
            }
            if market == "moneyline":
                candidate.update({
                    "pick": f"{team} ML", "books_available": len(odds.get("moneylines", {})),
                    "reasoning": _ml_reasoning(game, team, side, our_prob, break_even),
                })
            elif market == "total":
                candidate.update({
                    "pick": f"{side.title()} {line} ({book})", "team": f"{away} vs {home}",
                    "our_estimated_total": our_total, "market_line": line,
                    "reasoning": _total_reasoning(game, our_total, line),
                })
            else:
                margin = sum((h - a) * probability for h, a, probability in distribution)
                if side == "away":
                    margin *= -1
                candidate.update({
                    "pick": f"{team} {line:+.1f} ({book})",
                    "reasoning": f"Projected goal margin for {team}: {margin:+.2f} vs line {line:+.1f}",
                })
            candidates.append(candidate)

        for book, pair in odds.get("moneylines", {}).items():
            for side in ("away", "home"):
                add_candidate("moneyline", side, None, pair.get(side), book)
        for book, pair in odds.get("totals", {}).items():
            for side in ("over", "under"):
                quote = pair.get(side, {})
                if quote.get("line") is not None:
                    add_candidate("total", side, quote["line"], quote.get("odds"), book)
        for book, pair in odds.get("run_lines", {}).items():
            for side in ("away", "home"):
                quote = pair.get(side, {})
                if quote.get("line") is not None:
                    add_candidate("spread", side, quote["line"], quote.get("odds"), book)

    candidates.sort(key=lambda candidate: candidate["ev_per_100"], reverse=True)
    return _top_unique_candidates(candidates, top_n)


def _schedule_total_penalty(schedule_spot: dict) -> float:
    penalty = 0.0
    if schedule_spot.get("games_last_4", 0) >= 3:
        penalty += 0.10
    if schedule_spot.get("games_last_6", 0) >= 4:
        penalty += 0.12
    if schedule_spot.get("rest_days", 1) == 0:
        penalty += 0.05
    return penalty


def _schedule_win_penalty(schedule_spot: dict) -> float:
    penalty = 0.0
    if schedule_spot.get("games_last_4", 0) >= 3:
        penalty += 0.02
    if schedule_spot.get("games_last_6", 0) >= 4:
        penalty += 0.03
    if schedule_spot.get("road_trip_len", 0) >= 4:
        penalty += 0.015
    if schedule_spot.get("rest_days", 1) >= 2:
        penalty -= 0.01
    return penalty


def _top_unique_candidates(candidates: list[dict], top_n: int) -> list[dict]:
    unique = []
    seen = set()
    for candidate in candidates:
        key = candidate.pop("_market_key", None) or candidate.get("pick")
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
        if len(unique) >= top_n:
            break
    return unique


def _ml_reasoning(game: dict, team: str, side: str, our_prob: float, market_prob: float) -> str:
    parts = []
    standings = game.get(f"{side}_standings", {})
    stats = game.get(f"{side}_team_stats", {})
    schedule = game.get(f"{side}_schedule_spot", {})
    injury = game.get(f"{side}_injury_impact", {})

    win_pct = standings.get("win_pct")
    if win_pct:
        parts.append(f"Win%: {win_pct:.3f}")

    xgf = stats.get("xg", {}).get("xgf_pct")
    if xgf:
        parts.append(f"xGF%: {xgf:.1f}")

    goalie = stats.get("starting_goalie", {})
    goalie_xg = goalie.get("xg_stats", {})
    if goalie:
        sv = goalie.get("save_pct", 0)
        gaa = goalie.get("gaa", 0)
        gsax = goalie_xg.get("gsax")
        if sv is not None:
            goalie_detail = f"G: {goalie.get('name', '?')} .{int(sv*1000)}"
            if gaa is not None:
                goalie_detail += f" / {gaa:.2f} GAA"
            parts.append(goalie_detail)
        if gsax is not None:
            parts.append(f"GSAx: {gsax:+.1f}")

    if schedule.get("games_last_6", 0) >= 4:
        parts.append(f"4-in-6 spot ({schedule['games_last_6']} in last 6)")
    if injury.get("total_xg_lost", 0) >= 0.3:
        parts.append(f"Injuries: -{injury['total_xg_lost']:.2f} xG")

    edge_pct = round((our_prob - market_prob) * 100, 1)
    parts.append(f"Edge: +{edge_pct}%")
    return " | ".join(parts)


def _total_reasoning(game: dict, our_total: float, line: float) -> str:
    parts = [f"Model total: {our_total} vs line {line}"]
    away_xgf = game.get("away_team_stats", {}).get("xg", {}).get("xgf_per_game")
    home_xgf = game.get("home_team_stats", {}).get("xg", {}).get("xgf_per_game")
    if away_xgf and home_xgf:
        parts.append(f"xGF/G Away/Home: {away_xgf:.2f}/{home_xgf:.2f}")
    away_schedule = game.get("away_schedule_spot", {})
    home_schedule = game.get("home_schedule_spot", {})
    if away_schedule.get("games_last_6", 0) >= 4 or home_schedule.get("games_last_6", 0) >= 4:
        parts.append("Compressed schedule")
    return " | ".join(parts)
