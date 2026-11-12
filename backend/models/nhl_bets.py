"""
NHL game bets model.
Scores moneylines, puck lines, and totals, then returns the highest-EV bets.

Inputs:
- MoneyPuck / NST xG
- Goalie quality including GSAx
- Recent form and schedule density
- Injury impact
- Market structure and line movement
"""

from math import erf, sqrt

from data.odds import american_to_implied_prob, no_vig_prob

LEAGUE_AVG_GOALS = 6.10
HOME_WIN_ADVANTAGE = 0.055


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

    save_pct = goalie.get("save_pct", 0.906)
    gaa = goalie.get("gaa", 3.00)
    xg_stats = goalie.get("xg_stats", {})
    gsax = xg_stats.get("gsax", 0.0)
    games_played = max(xg_stats.get("games_played", goalie.get("games_started", 1)), 1)
    hd_sv_pct = xg_stats.get("hd_sv_pct", 0.82)
    quality_starts = xg_stats.get("quality_starts_pct", 0.5)

    save_adj = (0.906 - save_pct) * 20
    gaa_adj = (gaa - 3.00) * 0.15
    gsax_adj = -(gsax / games_played) * 1.1
    hd_adj = (0.82 - hd_sv_pct) * 7.0 if hd_sv_pct else 0.0
    qs_adj = -(quality_starts - 0.5) * 0.8 if quality_starts else 0.0

    return round(save_adj + gaa_adj + gsax_adj + hd_adj + qs_adj, 3)


def estimate_nhl_total(game: dict) -> float:
    """Estimate the fair total for the game."""
    base = LEAGUE_AVG_GOALS
    away_standings = game.get("away_standings", {})
    home_standings = game.get("home_standings", {})
    away_stats = game.get("away_team_stats", {})
    home_stats = game.get("home_team_stats", {})

    away_gf = away_standings.get("goals_for_per_game", base / 2)
    home_gf = home_standings.get("goals_for_per_game", base / 2)
    away_ga = away_standings.get("goals_against_per_game", base / 2)
    home_ga = home_standings.get("goals_against_per_game", base / 2)

    away_xgf = away_stats.get("xg", {}).get("xgf_per_game", away_gf)
    home_xgf = home_stats.get("xg", {}).get("xgf_per_game", home_gf)
    away_xga = away_stats.get("xg", {}).get("xga_per_game", away_ga)
    home_xga = home_stats.get("xg", {}).get("xga_per_game", home_ga)

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
    home_prob += net_goalie
    away_prob -= net_goalie

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

    away_pp = away_standings.get("pp_pct", 20.0)
    home_pp = home_standings.get("pp_pct", 20.0)
    pp_diff = (home_pp - away_pp) / 100 * 0.15
    home_prob += pp_diff
    away_prob -= pp_diff

    pdo_diff = (home_stats.get("xg", {}).get("pdo", 1.0) - away_stats.get("xg", {}).get("pdo", 1.0))
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
    home_margin_mean = (home_prob - away_prob) * max(total_goals * 0.95, 4.0)
    margin_sd = max(1.55, total_goals * 0.24)
    mean = home_margin_mean if side == "home" else -home_margin_mean
    threshold = -line
    cover_prob = 1 - _normal_cdf(threshold, mean, margin_sd)
    return round(max(0.08, min(0.92, cover_prob)), 3)


def calculate_ev(our_prob: float, american_odds: int) -> float:
    if american_odds is None:
        return -999.0
    profit_if_win = american_odds if american_odds > 0 else 10000 / abs(american_odds)
    return round((our_prob * profit_if_win) - ((1 - our_prob) * 100), 2)


def find_nhl_best_bets(snapshot: dict, top_n: int = 5) -> list[dict]:
    candidates = []

    for game in snapshot.get("games", []):
        odds = game.get("odds", {})
        if not odds:
            continue

        away = game["away_team"]
        home = game["home_team"]
        matchup = f"{away} @ {home}"
        venue = game.get("venue", "")
        game_time = game.get("game_time", "")
        market = odds.get("market_metrics", {})
        line_move = game.get("line_movement", {})

        away_prob, home_prob = estimate_nhl_win_prob(game)
        our_total = estimate_nhl_total(game)

        away_goalie = game.get("away_team_stats", {}).get("starting_goalie", {})
        home_goalie = game.get("home_team_stats", {}).get("starting_goalie", {})
        away_goalie_name = away_goalie.get("name", "TBD")
        home_goalie_name = home_goalie.get("name", "TBD")

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
                    "market_prob": round(market_prob, 3),
                    "edge": round(edge, 3),
                    "ev_per_100": ev,
                    "matchup": matchup,
                    "venue": venue,
                    "game_time": game_time,
                    "away_goalie": away_goalie_name,
                    "home_goalie": home_goalie_name,
                    "away_back_to_back": game.get("away_back_to_back", False),
                    "home_back_to_back": game.get("home_back_to_back", False),
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
                    "market_prob": round(market_prob, 3),
                    "edge": round(edge, 3),
                    "ev_per_100": ev,
                    "matchup": matchup,
                    "venue": venue,
                    "game_time": game_time,
                    "away_goalie": away_goalie_name,
                    "home_goalie": home_goalie_name,
                    "away_back_to_back": game.get("away_back_to_back", False),
                    "home_back_to_back": game.get("home_back_to_back", False),
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
            market_over, market_under = no_vig_prob(over_odds, under_odds)

            diff = our_total - line
            line_value_over = max(0.0, (odds.get("consensus_total", line) - line) * 0.03)
            line_value_under = max(0.0, (line - odds.get("consensus_total", line)) * 0.03)
            our_over = max(0.30, min(0.74, 0.50 + diff * 0.08 + sharp_boost_over + line_value_over))
            our_under = max(0.26, min(0.70, 1 - our_over + sharp_boost_under + line_value_under))

            for side, our_prob, market_prob, side_odds, label in [
                ("over", our_over, market_over, over_odds, "Over"),
                ("under", our_under, market_under, under_odds, "Under"),
            ]:
                edge = our_prob - market_prob
                ev = calculate_ev(our_prob, side_odds)
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
                        "away_goalie": away_goalie_name,
                        "home_goalie": home_goalie_name,
                        "our_estimated_total": our_total,
                        "market_line": line,
                        "away_back_to_back": game.get("away_back_to_back", False),
                        "home_back_to_back": game.get("home_back_to_back", False),
                        "reasoning": _total_reasoning(game, our_total, line),
                    })

        for book, spread_data in odds.get("run_lines", {}).items():
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
                our_prob = estimate_puckline_cover_probability(away_prob, home_prob, our_total, side, line)
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
                        "away_goalie": away_goalie_name,
                        "home_goalie": home_goalie_name,
                        "reasoning": _spread_reasoning(team, line, away_prob, home_prob, our_total, side),
                    })

    candidates.sort(key=lambda x: x["ev_per_100"], reverse=True)
    selected = _top_unique_candidates(candidates, top_n)
    for pick in selected:
        game = next((g for g in snapshot.get('games', []) if f"{g['away_team']} @ {g['home_team']}" == pick['matchup']), {})
        pick['game_id'] = game.get('game_id')
        odds = game.get('odds', {})
        side = 'away' if pick.get('team') == game.get('away_team') else 'home'
        market = {'Moneyline': 'h2h', 'Spread': 'spreads', 'Total': 'totals'}[pick['bet_type']]
        if pick['bet_type'] == 'Moneyline':
            book = next((b for b, pair in odds.get('moneylines', {}).items() if pair.get(side) == pick['odds']), None)
        else:
            book = pick['pick'].rsplit('(', 1)[-1].rstrip(')') if '(' in pick['pick'] else None
        pick['book'] = book
        pick['odds_updated_at'] = odds.get('market_updated_at', {}).get(market, {}).get(book)
        if pick['bet_type'] == 'Spread':
            pick['line'] = odds.get('run_lines', {}).get(book, {}).get(side, {}).get('line')
    return selected


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
        if sv:
            parts.append(f"G: {goalie.get('name', '?')} .{int(sv*1000)} / {gaa:.2f} GAA")
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


def _spread_reasoning(team: str, line: float, away_prob: float, home_prob: float, total: float, side: str) -> str:
    margin_mean = (home_prob - away_prob) * max(total * 0.95, 4.0)
    if side == "away":
        margin_mean *= -1
    return f"Projected goal margin for {team}: {margin_mean:+.2f} vs line {line:+.1f}"
