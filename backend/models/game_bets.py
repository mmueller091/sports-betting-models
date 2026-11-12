"""
Game bets model.
Scores MLB moneylines, run lines, and totals, then returns the highest-EV bets.

The upgraded model blends:
- Starting pitcher quality
- Confirmed lineup quality and platoon matchup
- Recent team form and schedule density
- Bullpen fatigue
- Park, weather, umpire, and injuries
- Market structure (best price vs consensus and line movement)
"""

from math import erf, sqrt

from data.odds import american_to_implied_prob, no_vig_prob

# How much we trust the model vs the no-vig market consensus
MODEL_WEIGHT = 0.35


def score_pitcher(pitcher_data: dict) -> float:
    """
    Convert pitcher data into a run-prevention score.
    Lower is better. Rough scale: -2.0 elite, +2.0 poor.
    """
    s = pitcher_data.get("season_stats", {})
    x = pitcher_data.get("xstats", {})

    if not s and not x:
        return 0.0

    score = 0.0
    era = x.get("xera") or s.get("era", 4.20)
    whip = s.get("whip", 1.25)
    k_pct = x.get("k_pct") or (s.get("k_per_9", 8.5) / 27 * 100)
    bb_pct = x.get("bb_pct") or (s.get("bb_per_9", 3.0) / 27 * 100)
    hh = x.get("hard_hit_pct_allowed", 37.0)
    barrel = x.get("barrel_pct_allowed", 7.5)
    hr_per_9 = s.get("hr_per_9", 1.1)

    score += (era - 4.20) * 0.30
    score += (whip - 1.25) * 0.50
    score -= (k_pct - 22) * 0.03
    score += (bb_pct - 8) * 0.05
    score += (hh - 37) * 0.02
    score += (barrel - 7.5) * 0.04
    score += (hr_per_9 - 1.1) * 0.12
    return round(score, 3)


def score_lineup_strength(lineup: list[dict], opposing_pitcher_data: dict) -> float:
    """
    Score an offense vs today's opposing starter.
    Positive means above-average run creation environment.
    """
    if not lineup:
        return 0.0

    weights = {
        1: 1.08, 2: 1.06, 3: 1.06, 4: 1.04, 5: 1.00,
        6: 0.96, 7: 0.93, 8: 0.90, 9: 0.88,
    }
    total = 0.0
    weight_total = 0.0
    for batter in lineup:
        order = batter.get("batting_order", 5)
        weight = weights.get(order, 0.92)
        total += _score_batter_matchup(batter, opposing_pitcher_data) * weight
        weight_total += weight

    return round(total / max(weight_total, 1), 3)


def estimate_game_total(game: dict) -> float:
    """
    Estimate our fair total (runs) for the game.
    """
    base = 8.8
    away_pd = game.get("away_pitcher_data", {})
    home_pd = game.get("home_pitcher_data", {})

    away_pitcher_score = score_pitcher(away_pd)
    home_pitcher_score = score_pitcher(home_pd)
    pitcher_adjustment = (away_pitcher_score + home_pitcher_score) * 0.50

    away_lineup = score_lineup_strength(game.get("away_lineup", []), home_pd)
    home_lineup = score_lineup_strength(game.get("home_lineup", []), away_pd)
    lineup_adjustment = (away_lineup + home_lineup) * 0.75

    away_bullpen = _bullpen_fatigue_score(game, "away")
    home_bullpen = _bullpen_fatigue_score(game, "home")
    bullpen_adjustment = (away_bullpen + home_bullpen) * 0.22

    # Bullpen quality — starters only cover ~5 innings, the pen decides the rest
    away_bp_era = _bullpen_quality_era(game, "away")
    home_bp_era = _bullpen_quality_era(game, "home")
    bullpen_quality_adjustment = ((away_bp_era - 4.10) + (home_bp_era - 4.10)) * 0.18

    park_factor = game.get("park_factor", 100)
    park_adjustment = (park_factor - 100) / 100 * 1.5

    weather = game.get("weather", {})
    weather_adj = weather.get("weather_factor", {}).get("total_adjustment", 0.0)

    umpire = game.get("umpire", {})
    ump_adj = umpire.get("run_adj", 0.0)

    away_inj = game.get("away_injury_impact", {}).get("run_impact", 0.0)
    home_inj = game.get("home_injury_impact", {}).get("run_impact", 0.0)
    injury_adj = away_inj + home_inj

    estimated = (
        base
        + pitcher_adjustment
        + lineup_adjustment
        + bullpen_adjustment
        + bullpen_quality_adjustment
        + park_adjustment
        + weather_adj
        + ump_adj
        + injury_adj
    )
    return round(max(estimated, 4.0), 2)


def estimate_win_probability(game: dict) -> tuple[float, float]:
    """
    Estimate fair win probability for away and home team.
    """
    away_pd = game.get("away_pitcher_data", {})
    home_pd = game.get("home_pitcher_data", {})

    away_pitcher_score = score_pitcher(away_pd)
    home_pitcher_score = score_pitcher(home_pd)
    pitcher_diff = away_pitcher_score - home_pitcher_score

    away_lineup = score_lineup_strength(game.get("away_lineup", []), home_pd)
    home_lineup = score_lineup_strength(game.get("home_lineup", []), away_pd)
    lineup_diff = home_lineup - away_lineup

    away_form = _team_form_score(game, "away")
    home_form = _team_form_score(game, "home")
    form_diff = home_form - away_form

    away_bullpen = _bullpen_fatigue_score(game, "away")
    home_bullpen = _bullpen_fatigue_score(game, "home")
    bullpen_diff = away_bullpen - home_bullpen

    away_injury = game.get("away_injury_impact", {}).get("run_impact", 0.0)
    home_injury = game.get("home_injury_impact", {}).get("run_impact", 0.0)
    injury_diff = home_injury - away_injury

    away_bp_era = _bullpen_quality_era(game, "away")
    home_bp_era = _bullpen_quality_era(game, "home")
    bullpen_quality_diff = away_bp_era - home_bp_era  # positive = home pen better

    home_prob = 0.540
    home_prob -= pitcher_diff * 0.08
    home_prob += lineup_diff * 0.06
    home_prob += form_diff * 0.04
    home_prob += bullpen_diff * 0.025
    home_prob += bullpen_quality_diff * 0.020
    home_prob += injury_diff * 0.08

    home_prob = max(0.28, min(0.76, home_prob))
    away_prob = 1 - home_prob
    return round(away_prob, 3), round(home_prob, 3)


def estimate_runline_cover_probability(
    away_prob: float,
    home_prob: float,
    total_runs: float,
    side: str,
    line: float,
) -> float:
    """
    Approximate run-line cover probability with a normal margin model.
    """
    home_margin_mean = (home_prob - away_prob) * max(total_runs * 1.1, 6.0)
    margin_sd = max(2.35, total_runs * 0.34)
    mean = home_margin_mean if side == "home" else -home_margin_mean
    threshold = -line
    cover_prob = 1 - _normal_cdf(threshold, mean, margin_sd)
    return round(max(0.08, min(0.92, cover_prob)), 3)


def calculate_ev(our_prob: float, american_odds: int) -> float:
    """Calculate expected value per $100 wagered."""
    if american_odds is None:
        return -999.0
    profit_if_win = american_odds if american_odds > 0 else 10000 / abs(american_odds)
    return round((our_prob * profit_if_win) - ((1 - our_prob) * 100), 2)


def find_best_bets(snapshot: dict, top_n: int = 5) -> list[dict]:
    """Score all available MLB bets and return the top N by EV."""
    candidates = []

    for game in snapshot.get("games", []):
        odds = game.get("odds", {})
        if not odds:
            continue

        away = game["away_team"]
        home = game["home_team"]
        matchup = f"{away} @ {home}"
        venue = game.get("venue", "Unknown")
        weather = game.get("weather", {})
        game_time = game.get("game_time", "")
        umpire = game.get("umpire", {})
        line_move = game.get("line_movement", {})
        market = odds.get("market_metrics", {})

        model_away_prob, model_home_prob = estimate_win_probability(game)
        our_total = estimate_game_total(game)

        # Shrink the model toward the no-vig market consensus. A small model
        # beats the market at the margins, not head-on — this kills most
        # false-positive "value" and only keeps edges the model is sure about.
        if market.get("books_h2h", 0) >= 2:
            consensus_away = market.get("consensus_prob_away", 0.5)
            consensus_home = market.get("consensus_prob_home", 0.5)
            away_prob = round(model_away_prob * MODEL_WEIGHT + consensus_away * (1 - MODEL_WEIGHT), 3)
            home_prob = round(model_home_prob * MODEL_WEIGHT + consensus_home * (1 - MODEL_WEIGHT), 3)
        else:
            away_prob, home_prob = model_away_prob, model_home_prob

        sharp_signal = line_move.get("sharp_signal")
        sharp_desc = line_move.get("sharp_signal_desc", "")
        sharp_boost_away = 0.015 if sharp_signal == "away" else 0.0
        sharp_boost_home = 0.015 if sharp_signal == "home" else 0.0
        sharp_boost_over = 0.012 if sharp_signal == "over" else 0.0
        sharp_boost_under = 0.012 if sharp_signal == "under" else 0.0

        ml_away = odds.get("best_moneyline_away")
        ml_home = odds.get("best_moneyline_home")

        if ml_away:
            market_prob = american_to_implied_prob(ml_away)
            market_support = _market_support_boost(market.get("best_vs_consensus_away", 0.0))
            ev = calculate_ev(away_prob + sharp_boost_away + market_support, ml_away)
            edge = away_prob - market_prob
            if edge > 0:
                candidates.append({
                    "_market_key": f"moneyline:{matchup}:away",
                    "bet_type": "Moneyline",
                    "pick": f"{away} ML",
                    "team": away,
                    "odds": ml_away,
                    "our_prob": away_prob,
                    "model_prob": model_away_prob,
                    "market_prob": round(market_prob, 3),
                    "edge": round(edge, 3),
                    "ev_per_100": ev,
                    "matchup": matchup,
                    "venue": venue,
                    "game_time": game_time,
                    "weather_notes": weather.get("weather_factor", {}).get("notes", []),
                    "away_pitcher": (game.get("away_probable_pitcher") or {}).get("name", "TBD"),
                    "home_pitcher": (game.get("home_probable_pitcher") or {}).get("name", "TBD"),
                    "umpire": umpire.get("name"),
                    "umpire_tendency": umpire.get("tendency_label"),
                    "sharp_signal": sharp_desc if sharp_signal == "away" else None,
                    "market_support": round(market.get("best_vs_consensus_away", 0.0), 3),
                    "books_available": market.get("books_h2h", 0),
                    "reasoning": _ml_reasoning(game, away, "away", away_prob, market_prob),
                })

        if ml_home:
            market_prob = american_to_implied_prob(ml_home)
            market_support = _market_support_boost(market.get("best_vs_consensus_home", 0.0))
            ev = calculate_ev(home_prob + sharp_boost_home + market_support, ml_home)
            edge = home_prob - market_prob
            if edge > 0:
                candidates.append({
                    "_market_key": f"moneyline:{matchup}:home",
                    "bet_type": "Moneyline",
                    "pick": f"{home} ML",
                    "team": home,
                    "odds": ml_home,
                    "our_prob": home_prob,
                    "model_prob": model_home_prob,
                    "market_prob": round(market_prob, 3),
                    "edge": round(edge, 3),
                    "ev_per_100": ev,
                    "matchup": matchup,
                    "venue": venue,
                    "game_time": game_time,
                    "weather_notes": weather.get("weather_factor", {}).get("notes", []),
                    "away_pitcher": (game.get("away_probable_pitcher") or {}).get("name", "TBD"),
                    "home_pitcher": (game.get("home_probable_pitcher") or {}).get("name", "TBD"),
                    "umpire": umpire.get("name"),
                    "umpire_tendency": umpire.get("tendency_label"),
                    "sharp_signal": sharp_desc if sharp_signal == "home" else None,
                    "market_support": round(market.get("best_vs_consensus_home", 0.0), 3),
                    "books_available": market.get("books_h2h", 0),
                    "reasoning": _ml_reasoning(game, home, "home", home_prob, market_prob),
                })

        for book, total_data in odds.get("totals", {}).items():
            if "over" not in total_data or "under" not in total_data:
                continue

            line = total_data["over"]["line"]
            over_odds = total_data["over"]["odds"]
            under_odds = total_data["under"]["odds"]
            market_over_prob, market_under_prob = no_vig_prob(over_odds, under_odds)

            diff = our_total - line
            line_value_over = max(0.0, (odds.get("consensus_total", line) - line) * 0.02)
            line_value_under = max(0.0, (line - odds.get("consensus_total", line)) * 0.02)

            our_over_prob = max(0.30, min(0.74, 0.50 + diff * 0.06 + sharp_boost_over + line_value_over))
            our_under_prob = max(0.26, min(0.70, 1 - our_over_prob + sharp_boost_under + line_value_under))

            # Shrink totals toward the no-vig consensus too
            if market.get("books_totals", 0) >= 2:
                consensus_over = market.get("consensus_total_over_prob", 0.5)
                consensus_under = market.get("consensus_total_under_prob", 0.5)
                our_over_prob = our_over_prob * MODEL_WEIGHT + consensus_over * (1 - MODEL_WEIGHT)
                our_under_prob = our_under_prob * MODEL_WEIGHT + consensus_under * (1 - MODEL_WEIGHT)

            for side, our_prob, market_prob, side_odds, label in [
                ("over", our_over_prob, market_over_prob, over_odds, "Over"),
                ("under", our_under_prob, market_under_prob, under_odds, "Under"),
            ]:
                ev = calculate_ev(our_prob, side_odds)
                edge = our_prob - market_prob
                if edge > 0.02:
                    candidates.append({
                        "_market_key": f"total:{matchup}:{side}",
                        "bet_type": "Total",
                        "pick": f"{label} {line} ({book})",
                        "team": f"{away} vs {home}",
                        "odds": side_odds,
                        "our_prob": round(our_prob, 3),
                        "market_prob": round(market_prob, 3),
                        "edge": round(edge, 3),
                        "ev_per_100": ev,
                        "matchup": matchup,
                        "venue": venue,
                        "game_time": game_time,
                        "weather_notes": weather.get("weather_factor", {}).get("notes", []),
                        "away_pitcher": (game.get("away_probable_pitcher") or {}).get("name", "TBD"),
                        "home_pitcher": (game.get("home_probable_pitcher") or {}).get("name", "TBD"),
                        "our_estimated_total": our_total,
                        "market_line": line,
                        "reasoning": _total_reasoning(game, our_total, line, side, weather),
                    })

        for book, spread_data in odds.get("run_lines", {}).items():
            away_rl = spread_data.get("away") or {}
            home_rl = spread_data.get("home") or {}
            rl_no_vig = {}
            if away_rl.get("odds") is not None and home_rl.get("odds") is not None:
                nv_away, nv_home = no_vig_prob(away_rl["odds"], home_rl["odds"])
                rl_no_vig = {"away": nv_away, "home": nv_home}

            for side, team, data in [
                ("away", away, spread_data.get("away")),
                ("home", home, spread_data.get("home")),
            ]:
                if not data:
                    continue
                line = data.get("line")
                side_odds = data.get("odds")
                if line is None or side_odds is None:
                    continue
                our_prob = estimate_runline_cover_probability(away_prob, home_prob, our_total, side, line)
                if side in rl_no_vig:
                    our_prob = round(our_prob * MODEL_WEIGHT + rl_no_vig[side] * (1 - MODEL_WEIGHT), 3)
                market_prob = american_to_implied_prob(side_odds)
                edge = our_prob - market_prob
                ev = calculate_ev(our_prob, side_odds)
                if edge > 0.02:
                    candidates.append({
                        "_market_key": f"spread:{matchup}:{side}",
                        "bet_type": "Spread",
                        "pick": f"{team} {line:+.1f} ({book})",
                        "team": team,
                        "odds": side_odds,
                        "our_prob": round(our_prob, 3),
                        "market_prob": round(market_prob, 3),
                        "edge": round(edge, 3),
                        "ev_per_100": ev,
                        "matchup": matchup,
                        "venue": venue,
                        "game_time": game_time,
                        "weather_notes": weather.get("weather_factor", {}).get("notes", []),
                        "away_pitcher": (game.get("away_probable_pitcher") or {}).get("name", "TBD"),
                        "home_pitcher": (game.get("home_probable_pitcher") or {}).get("name", "TBD"),
                        "reasoning": _spread_reasoning(team, line, away_prob, home_prob, our_total, side),
                    })

    candidates.sort(key=lambda x: x["ev_per_100"], reverse=True)
    return _top_unique_candidates(candidates, top_n)


def _score_batter_matchup(batter: dict, opposing_pitcher_data: dict) -> float:
    stats = batter.get("stats", {})
    recent = batter.get("recent", {})
    xstats = batter.get("xstats", {})
    split = _pitcher_split_for_hand(opposing_pitcher_data.get("splits", {}), batter.get("bats", "R"))

    avg = stats.get("avg", 0.245)
    ops = stats.get("ops", 0.720)
    xwoba = xstats.get("xwoba", 0.320)
    xslg = xstats.get("xslg", 0.400)
    hard_hit = xstats.get("hard_hit_pct", 38.0)
    barrel = xstats.get("barrel_pct", 7.0)

    score = 0.0
    score += (avg - 0.245) * 3.5
    score += (ops - 0.720) * 1.8
    score += (xwoba - 0.320) * 5.0
    score += (xslg - 0.400) * 1.2
    score += (hard_hit - 38.0) * 0.012
    score += (barrel - 7.0) * 0.03

    recent_ab = recent.get("at_bats", 0)
    if recent_ab >= 20:
        score += (recent.get("ops", 0.720) - 0.720) * 0.70

    if split.get("ops"):
        score += (split["ops"] - 0.720) * 0.65
    elif split.get("avg"):
        score += (split["avg"] - 0.245) * 2.5

    order = batter.get("batting_order", 5)
    score += max(0, 5 - order) * 0.025
    return score


def _pitcher_split_for_hand(splits: dict, batter_hand: str) -> dict:
    if batter_hand == "L":
        return splits.get("vs_lhb", {})
    if batter_hand == "R":
        return splits.get("vs_rhb", {})
    vs_l = splits.get("vs_lhb", {})
    vs_r = splits.get("vs_rhb", {})
    if not vs_l and not vs_r:
        return {}
    return {
        "avg": round((vs_l.get("avg", 0.245) + vs_r.get("avg", 0.245)) / 2, 3),
        "ops": round((vs_l.get("ops", 0.720) + vs_r.get("ops", 0.720)) / 2, 3),
    }


def _bullpen_fatigue_score(game: dict, side: str) -> float:
    context = game.get(f"{side}_team_context", {})
    return context.get("bullpen", {}).get("fatigue_index", 0.0)


def _bullpen_quality_era(game: dict, side: str) -> float:
    """Team bullpen ERA, defaulting to league average when unavailable."""
    context = game.get(f"{side}_team_context", {})
    era = context.get("bullpen_quality", {}).get("bullpen_era", 0.0)
    return era if era and 1.0 < era < 9.0 else 4.10


def _team_form_score(game: dict, side: str) -> float:
    recent = game.get(f"{side}_team_context", {}).get("recent_form", {})
    win_pct = recent.get("win_pct_last_10", 0.5)
    run_diff = recent.get("run_diff_per_game_last_10", 0.0)
    rest_days = recent.get("rest_days", 1)
    games_last_5 = recent.get("games_last_5", 0)
    road_trip_len = recent.get("road_trip_len", 0)

    score = 0.0
    score += (win_pct - 0.5) * 1.0
    score += run_diff * 0.12
    score += min(rest_days, 2) * 0.03
    score -= max(games_last_5 - 4, 0) * 0.05
    score -= max(road_trip_len - 5, 0) * 0.04
    return round(score, 3)


def _market_support_boost(best_vs_consensus: float) -> float:
    return max(0.0, min(best_vs_consensus * 0.35, 0.01))


def _normal_cdf(x: float, mean: float, std_dev: float) -> float:
    if std_dev <= 0:
        return 0.5
    z = (x - mean) / (std_dev * sqrt(2))
    return 0.5 * (1 + erf(z))


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
    pitcher = (game.get(f"{side}_probable_pitcher") or {}).get("name", "Unknown")
    pitcher_data = game.get(f"{side}_pitcher_data", {})
    season_stats = pitcher_data.get("season_stats", {})
    xstats = pitcher_data.get("xstats", {})
    lineup_score = score_lineup_strength(
        game.get(f"{side}_lineup", []),
        game.get("home_pitcher_data" if side == "away" else "away_pitcher_data", {}),
    )
    bullpen = game.get(f"{side}_team_context", {}).get("bullpen", {})

    era = xstats.get("xera") or season_stats.get("era")
    if era:
        parts.append(f"{pitcher}: xERA {era:.2f}")
    if lineup_score:
        parts.append(f"Lineup score {lineup_score:+.2f}")
    if bullpen.get("fatigue_index", 0) >= 1.0:
        parts.append(f"Bullpen fatigue {bullpen['fatigue_index']:.2f}")

    edge_pct = round((our_prob - market_prob) * 100, 1)
    parts.append(f"Our edge: +{edge_pct}%")
    return " | ".join(parts) if parts else f"Model favors {team}"


def _total_reasoning(game: dict, our_total: float, line: float, side: str, weather: dict) -> str:
    parts = [f"Model total: {our_total} runs vs line {line}"]
    park_factor = game.get("park_factor", 100)
    if park_factor != 100:
        parts.append(f"Park factor: {park_factor}")

    away_bp = _bullpen_fatigue_score(game, "away")
    home_bp = _bullpen_fatigue_score(game, "home")
    if away_bp >= 1.0 or home_bp >= 1.0:
        parts.append(f"Bullpen fatigue A/H: {away_bp:.2f}/{home_bp:.2f}")

    w_notes = weather.get("weather_factor", {}).get("notes", [])
    if w_notes:
        parts.extend(w_notes)
    return " | ".join(parts)


def _spread_reasoning(team: str, line: float, away_prob: float, home_prob: float, total: float, side: str) -> str:
    margin_mean = (home_prob - away_prob) * max(total * 1.1, 6.0)
    if side == "away":
        margin_mean *= -1
    return f"Projected margin for {team}: {margin_mean:+.2f} vs line {line:+.1f}"
