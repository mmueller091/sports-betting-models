"""Conservative weekly football estimates with strictly historical validation.

These are research estimates, not a calibrated betting system. Bookmaker prices
are used only to evaluate a quoted wager, never as training targets/features.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from math import erf, exp, floor, isfinite, sqrt
from statistics import mean
import re
from zoneinfo import ZoneInfo

import numpy as np
from sklearn.linear_model import Ridge

EASTERN = ZoneInfo("America/New_York")
MODEL_VERSION = "football-research-1.0"
CONFIG = {
    "nfl": {"home_prior": 1.5, "alpha": 6.0, "margin_cap": 35.0,
            "margin_sigma": 14.0, "total_sigma": 14.0, "mean_score": 22.5,
            "minimum_history": 40, "minimum_holdout": 30, "minimum_team": 6,
            "minimum_edge": 0.04, "minimum_ev": 7.0},
    "cfb": {"home_prior": 2.5, "alpha": 7.0, "margin_cap": 49.0,
            "margin_sigma": 18.0, "total_sigma": 18.0, "mean_score": 27.0,
            "minimum_history": 120, "minimum_holdout": 80, "minimum_team": 6,
            "minimum_edge": 0.06, "minimum_ev": 10.0},
}
RATE_FIELDS = {
    "epa_per_play": ("plays", 0.0),
    "defensive_epa_per_play": ("defensive_plays", 0.0),
    "pass_epa": ("dropbacks", 0.0),
    "defensive_pass_epa": ("defensive_dropbacks", 0.0),
    "success_rate": ("plays", 0.45),
    "defensive_success_rate": ("defensive_plays", 0.45),
    "explosive_rate": ("plays", 0.10),
    "defensive_explosive_rate": ("defensive_plays", 0.10),
    "sack_rate": ("dropbacks", 0.07),
    "pressure_sack_rate": ("defensive_dropbacks", 0.07),
}


def _number(value):
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _time(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (ValueError, TypeError):
        return None


def _week_start(timestamp):
    day = timestamp.astimezone(EASTERN).date()
    # Tuesday through Monday keeps Monday Night Football with its opening slate.
    return day - timedelta(days=(day.weekday() - 1) % 7)


def _history(snapshot, now):
    raw_cutoff = snapshot.get("week_start") or snapshot.get("date")
    try:
        cutoff = datetime.combine(date.fromisoformat(raw_cutoff), datetime.min.time(), EASTERN)
    except (ValueError, TypeError):
        cutoff = now
    cutoff = min(cutoff, now)
    rows, seen = [], set()
    for game in snapshot.get("historical_games", []):
        kickoff = _time(game.get("game_time"))
        home_score, away_score = _number(game.get("home_score")), _number(game.get("away_score"))
        home, away = str(game.get("home_team_id", "")), str(game.get("away_team_id", ""))
        key = str(game.get("game_id") or f"{kickoff}:{home}:{away}")
        if not kickoff or kickoff >= cutoff or not home or not away or home == away:
            continue
        if home_score is None or away_score is None or min(home_score, away_score) < 0 or key in seen:
            continue
        seen.add(key)
        rows.append({**game, "home_team_id": home, "away_team_id": away,
                     "home_score": home_score, "away_score": away_score, "_time": kickoff})
    return sorted(rows, key=lambda row: row["_time"])


def _empty_rates():
    return {"games": 0, "numerators": defaultdict(float), "denominators": defaultdict(float)}


def _add_metrics(state, team, metrics):
    if not isinstance(metrics, dict) or _number(metrics.get("epa_per_play")) is None:
        return
    entry = state.setdefault(team, _empty_rates())
    entry["games"] += 1
    for field, (weight_field, _) in RATE_FIELDS.items():
        value = _number(metrics.get(field))
        weight = _number(metrics.get(weight_field))
        if weight is None and weight_field == "defensive_plays":
            weight = _number(metrics.get("plays"))
        if weight is None and weight_field == "defensive_dropbacks":
            weight = _number(metrics.get("dropbacks"))
        if value is not None and weight is not None and weight > 0:
            entry["numerators"][field] += value * weight
            entry["denominators"][field] += weight


def _rates(state, team):
    entry = state.get(team, _empty_rates())
    values = {"games": entry["games"], "available_fields": [key for key, value in entry["denominators"].items() if value > 0]}
    for field, (weight_field, prior) in RATE_FIELDS.items():
        # Four games of neutral prior stabilize sparse early-season samples.
        prior_plays = 140.0 if "dropback" in weight_field else 240.0
        denominator = entry["denominators"].get(field, 0)
        values[field] = (entry["numerators"].get(field, 0) + prior * prior_plays) / (denominator + prior_plays)
    return values


def _advanced_features(home, away):
    def net(values, offense, defense):
        return values[offense] - values[defense]
    return np.array([
        (net(home, "epa_per_play", "defensive_epa_per_play") - net(away, "epa_per_play", "defensive_epa_per_play")) / 0.10,
        (net(home, "pass_epa", "defensive_pass_epa") - net(away, "pass_epa", "defensive_pass_epa")) / 0.10,
        (net(home, "success_rate", "defensive_success_rate") - net(away, "success_rate", "defensive_success_rate")) / 0.05,
        (net(home, "explosive_rate", "defensive_explosive_rate") - net(away, "explosive_rate", "defensive_explosive_rate")) / 0.05,
        ((away["sack_rate"] - away["pressure_sack_rate"]) - (home["sack_rate"] - home["pressure_sack_rate"])) / 0.03,
    ])


def _score_features(offense, defense):
    return np.array([
        (offense["epa_per_play"] + defense["defensive_epa_per_play"]) / 0.10,
        (offense["pass_epa"] + defense["defensive_pass_epa"]) / 0.10,
        (offense["success_rate"] + defense["defensive_success_rate"] - 0.90) / 0.05,
        (offense["explosive_rate"] + defense["defensive_explosive_rate"] - 0.20) / 0.05,
        (offense["sack_rate"] + defense["pressure_sack_rate"] - 0.14) / 0.03,
    ])


class FootballRatings:
    def __init__(self, games, league):
        self.config = CONFIG[league]
        self.games = games
        self.index = {team: i for i, team in enumerate(sorted({str(g[k]) for g in games for k in ("home_team_id", "away_team_id")}))}
        self.counts = Counter(str(g[k]) for g in games for k in ("home_team_id", "away_team_id"))
        self.state = {}
        self.advanced_games = sum(_number(g.get("home_metrics", {}).get("epa_per_play")) is not None and
                                  _number(g.get("away_metrics", {}).get("epa_per_play")) is not None for g in games)
        self.advanced = self.advanced_games >= max(24, len(games) * 0.5)
        self.mean_score = mean((g["home_score"] + g["away_score"]) / 2 for g in games) if games else self.config["mean_score"]
        self.margin_model = self.score_model = None
        if games:
            self._fit()

    def _margin_x(self, home, away, neutral, home_rates, away_rates):
        n = len(self.index)
        row = np.zeros(n + 1 + (5 if self.advanced else 0))
        if home in self.index:
            row[self.index[home]] = 1
        if away in self.index:
            row[self.index[away]] = -1
        row[n] = 0 if neutral else 1
        if self.advanced:
            row[n + 1:] = _advanced_features(home_rates, away_rates)
        return row

    def _score_x(self, team, opponent, own_rates, opponent_rates):
        n = len(self.index)
        row = np.zeros(2 * n + (5 if self.advanced else 0))
        if team in self.index:
            row[self.index[team]] = 1
        if opponent in self.index:
            row[n + self.index[opponent]] = 1
        if self.advanced:
            row[2 * n:] = _score_features(own_rates, opponent_rates)
        return row

    def _fit(self):
        margin_x, margin_y, score_x, score_y, weights = [], [], [], [], []
        grouped = defaultdict(list)
        for game in self.games:
            grouped[_week_start(game["_time"])].append(game)
        last = self.games[-1]["_time"]
        for _, group in sorted(grouped.items()):
            for game in group:
                home, away = game["home_team_id"], game["away_team_id"]
                hr, ar = _rates(self.state, home), _rates(self.state, away)
                home_advantage = 0 if game.get("neutral_site") else self.config["home_prior"]
                margin_x.append(self._margin_x(home, away, game.get("neutral_site", False), hr, ar))
                margin = game["home_score"] - game["away_score"]
                margin_y.append(max(-self.config["margin_cap"], min(self.config["margin_cap"], margin)) - home_advantage)
                score_x.extend([self._score_x(home, away, hr, ar), self._score_x(away, home, ar, hr)])
                score_y.extend([game["home_score"] - self.mean_score - home_advantage / 2,
                                game["away_score"] - self.mean_score + home_advantage / 2])
                weights.append(exp(-max(0, (last - game["_time"]).days) / 90))
            # A week's own play-by-play never enters its pregame feature vectors.
            for game in group:
                _add_metrics(self.state, game["home_team_id"], game.get("home_metrics", {}))
                _add_metrics(self.state, game["away_team_id"], game.get("away_metrics", {}))
        self.margin_model = Ridge(alpha=self.config["alpha"], fit_intercept=False).fit(np.asarray(margin_x), np.asarray(margin_y), sample_weight=np.asarray(weights))
        self.score_model = Ridge(alpha=self.config["alpha"], fit_intercept=False).fit(np.asarray(score_x), np.asarray(score_y), sample_weight=np.repeat(weights, 2))

    def predict(self, game):
        home, away = str(game["home_team_id"]), str(game["away_team_id"])
        hr, ar = _rates(self.state, home), _rates(self.state, away)
        home_advantage = 0 if game.get("neutral_site") else self.config["home_prior"]
        margin = home_advantage
        total = 2 * self.mean_score
        if self.margin_model is not None:
            margin += float(self.margin_model.predict([self._margin_x(home, away, game.get("neutral_site", False), hr, ar)])[0])
            total += sum(self.score_model.predict([self._score_x(home, away, hr, ar), self._score_x(away, home, ar, hr)]))
        margin = max(-self.config["margin_cap"], min(self.config["margin_cap"], margin))
        total = max(24.0, min(90.0, float(total)))
        return {"margin": margin, "total": total, "home_score": max(0, (total + margin) / 2),
                "away_score": max(0, (total - margin) / 2), "home_rates": hr, "away_rates": ar}


def validate_history(games, league):
    """Expanding, weekly holdout: every prediction uses only earlier weeks."""
    config = CONFIG[league]
    groups = defaultdict(list)
    for game in games:
        groups[_week_start(game["_time"])].append(game)
    prior, margin_errors, total_errors, baseline_margin, baseline_total = [], [], [], [], []
    market_margin_errors, market_total_errors, same_margin_errors, same_total_errors = [], [], [], []
    folds = []
    for week, group in sorted(groups.items()):
        if len(prior) >= config["minimum_history"]:
            ratings = FootballRatings(prior, league)
            evaluated = 0
            for game in group:
                if min(ratings.counts[game["home_team_id"]], ratings.counts[game["away_team_id"]]) < 3:
                    continue
                prediction = ratings.predict(game)
                actual_margin = game["home_score"] - game["away_score"]
                actual_total = game["home_score"] + game["away_score"]
                margin_errors.append(actual_margin - prediction["margin"])
                total_errors.append(actual_total - prediction["total"])
                baseline_margin.append(actual_margin - (0 if game.get("neutral_site") else config["home_prior"]))
                baseline_total.append(actual_total - 2 * ratings.mean_score)
                closing_margin = _number(game.get("closing_home_margin"))
                closing_total = _number(game.get("closing_total"))
                if closing_margin is not None:
                    market_margin_errors.append(actual_margin - closing_margin)
                    same_margin_errors.append(actual_margin - prediction["margin"])
                if closing_total is not None:
                    market_total_errors.append(actual_total - closing_total)
                    same_total_errors.append(actual_total - prediction["total"])
                evaluated += 1
            folds.append({"week_start": week.isoformat(), "training_games": len(prior), "holdout_games": evaluated})
        prior.extend(group)
    def mae(values):
        return round(mean(abs(v) for v in values), 2) if values else None
    def rmse(values):
        return round(sqrt(mean(v * v for v in values)), 2) if values else None
    ready = len(margin_errors) >= config["minimum_holdout"]
    return {
        "status": "provisional" if ready else "insufficient_history",
        "label": "Chronological score validation; betting returns untested",
        "training_games": len(games), "holdout_games": len(margin_errors),
        "margin_mae": mae(margin_errors), "total_mae": mae(total_errors),
        "margin_rmse": rmse(margin_errors), "total_rmse": rmse(total_errors),
        "baseline_margin_mae": mae(baseline_margin), "baseline_total_mae": mae(baseline_total),
        "closing_margin_mae": mae(market_margin_errors), "closing_total_mae": mae(market_total_errors),
        "model_margin_mae_with_market": mae(same_margin_errors), "model_total_mae_with_market": mae(same_total_errors),
        "closing_margin_games": len(market_margin_errors), "closing_total_games": len(market_total_errors),
        "folds": folds,
        "notes": "Each held-out week is predicted using earlier weeks only. Error is measured in points. Available NFL closing spreads/totals provide a forecast-error benchmark, never a training input. Historical bet prices are unavailable, so these errors do not establish betting profitability or probability calibration.",
    }


def _cdf(value, mean_value, sigma):
    return 0.5 * (1 + erf((value - mean_value) / (max(sigma, 1) * sqrt(2))))


def settlement_probabilities(mean_value, sigma, threshold, side="over"):
    """Discretized normal distribution on integer scores; preserve push mass."""
    if abs(threshold - round(threshold)) < 1e-9:
        integer = round(threshold)
        lower = _cdf(integer - 0.5, mean_value, sigma)
        upper = _cdf(integer + 0.5, mean_value, sigma)
        above, below, push = 1 - upper, lower, upper - lower
    else:
        boundary = floor(threshold) + 0.5
        below = _cdf(boundary, mean_value, sigma)
        above, push = 1 - below, 0.0
    win, loss = (above, below) if side == "over" else (below, above)
    return win, push, loss


def implied_probability(odds):
    odds = float(odds)
    return 100 / (100 + odds) if odds > 0 else -odds / (100 - odds)


def expected_value(win, loss, odds):
    profit = float(odds) if odds > 0 else 10000 / abs(float(odds))
    return win * profit - loss * 100


def margin_settlement(mean_value, sigma, threshold, allow_ties=True):
    win, push, loss = settlement_probabilities(mean_value, sigma, threshold)
    if allow_ties:
        return win, push, loss
    # College overtime eliminates a final score of zero margin. Remove that
    # discrete mass instead of inventing a moneyline/PK push for an impossible tie.
    zero = _cdf(0.5, mean_value, sigma) - _cdf(-0.5, mean_value, sigma)
    if threshold < 0:
        win -= zero
    elif threshold > 0:
        loss -= zero
    else:
        push -= zero
    denominator = max(1 - zero, 1e-8)
    return tuple(max(0.0, value) / denominator for value in (win, push, loss))


def _quote_time(game, snapshot, market, book):
    odds = game.get("odds", {})
    # A cache retrieval time does not establish when this book quoted this market.
    return odds.get("market_updated_at", {}).get(market, {}).get(book)


def _qb_alerts(team):
    alerts = []
    recent_passer = team.get("recent_primary_passer")
    def player_key(name):
        tokens = re.findall(r"[a-z]+", str(name).lower().replace("'", ""))
        while tokens and tokens[-1] in ("jr", "sr", "ii", "iii", "iv"):
            tokens.pop()
        return (tokens[0][0], tokens[-1]) if len(tokens) >= 2 else None
    recent_key = player_key(recent_passer) if recent_passer else None
    for injury in team.get("injuries", []):
        if not isinstance(injury, dict):
            continue
        athlete = injury.get("athlete", {}) if isinstance(injury.get("athlete"), dict) else {}
        position = injury.get("position") or athlete.get("position", {})
        if isinstance(position, dict):
            position = position.get("abbreviation") or position.get("name")
        status = str(injury.get("status") or injury.get("description") or "").lower()
        if str(position).upper() in ("QB", "QUARTERBACK") and any(word in status for word in ("out", "question", "doubt", "injured", "ir", "suspend")):
            name = injury.get("name") or injury.get("player") or athlete.get("displayName") or "Quarterback"
            detail = str(injury.get("detail", "")).lower()
            if "coach's decision" in detail or "emergency no. 3" in detail or "emergency third" in detail:
                continue
            injury_key = player_key(name)
            if recent_key and injury_key and injury_key != recent_key:
                continue
            role = "recent primary passer" if recent_key else "starting role must be checked"
            alerts.append(f"{name}: {injury.get('status') or 'availability uncertain'}; {role}")
    return alerts


def _candidates(game, snapshot, prediction, margin_sigma, total_sigma, now):
    odds, candidates = game.get("odds", {}), []
    matchup = f"{game['away_team']} @ {game['home_team']}"
    if game.get("neutral_site"):
        matchup = f"{game['away_team']} vs {game['home_team']}"

    def add(market, side, book, quote, line=None):
        price = _number(quote)
        if price is None or abs(price) < 100:
            return
        if market == "total":
            win, push, loss = settlement_probabilities(prediction["total"], total_sigma, line, side)
            conservative_mean = prediction["total"] - 3 if side == "over" else prediction["total"] + 3
            cw, _, cl = settlement_probabilities(conservative_mean, total_sigma, line, side)
            label = f"{side.title()} {line:g}"
            api_market = "totals"
        else:
            side_mean = prediction["margin"] * (1 if side == "home" else -1)
            threshold = -line if market == "spread" else 0
            allow_ties = snapshot.get("league") != "cfb"
            win, push, loss = margin_settlement(side_mean, margin_sigma, threshold, allow_ties)
            cw, _, cl = margin_settlement(side_mean - 3, margin_sigma, threshold, allow_ties)
            team_name = game[f"{side}_team"]
            label = f"{team_name} {line:+g}" if market == "spread" else f"{team_name} moneyline"
            api_market = "spreads" if market == "spread" else "h2h"
        conditional = win / max(1 - push, 1e-8)
        break_even = implied_probability(price)
        updated_at = _quote_time(game, snapshot, api_market, book)
        quote_dt = _time(updated_at)
        age = (now - quote_dt).total_seconds() / 3600 if quote_dt else None
        candidates.append({
            "game_id": str(game["game_id"]), "matchup": matchup, "game_time": game["game_time"],
            "pick": label, "market": market, "side": side, "line": line, "odds": int(price),
            "book": odds.get("book_titles", {}).get(book, book), "book_key": book,
            "model_probability": round(win, 4), "push_probability": round(push, 4),
            "conditional_probability": round(conditional, 4), "market_probability": round(break_even, 4),
            "edge": round(conditional - break_even, 4), "expected_value": round(expected_value(win, loss, price), 2),
            "conservative_ev": round(expected_value(cw, cl, price), 2),
            "odds_updated_at": updated_at, "odds_age_hours": round(age, 2) if age is not None else None,
            "odds_retrieved_at": odds.get("updated_at") or snapshot.get("odds_status", {}).get("updated_at"),
            "projected_home_score": round(prediction["home_score"], 1), "projected_away_score": round(prediction["away_score"], 1),
            "fair_home_spread": round(-prediction["margin"], 1), "fair_total": round(prediction["total"], 1),
            "confidence": "provisional",
        })

    for book, pair in odds.get("moneylines", {}).items():
        for side in ("home", "away"):
            if side in pair:
                add("moneyline", side, book, pair[side])
    for book, pair in odds.get("run_lines", {}).items():
        for side in ("home", "away"):
            quote = pair.get(side, {})
            line = _number(quote.get("line"))
            if line is not None:
                add("spread", side, book, quote.get("odds"), line)
    for book, pair in odds.get("totals", {}).items():
        for side in ("over", "under"):
            quote = pair.get(side, {})
            line = _number(quote.get("line"))
            if line is not None:
                add("total", side, book, quote.get("odds"), line)
    return candidates


def _display_injuries(team):
    injuries = []
    for injury in team.get("injuries", []):
        if isinstance(injury, str):
            injuries.append(injury)
        elif isinstance(injury, dict):
            athlete = injury.get("athlete", {})
            name = injury.get("name") or injury.get("player")
            if not isinstance(name, str):
                name = athlete.get("displayName", "Player") if isinstance(athlete, dict) else str(athlete)
            position = injury.get("position") or (athlete.get("position") if isinstance(athlete, dict) else None)
            if isinstance(position, dict):
                position = position.get("abbreviation") or position.get("name")
            injuries.append({"name": name, "position": position, "status": injury.get("status") or injury.get("description") or "Status unavailable"})
    return injuries


def _matchup_metrics(home_team, away_team, prediction, advanced):
    rows = []
    hr, ar = prediction["home_rates"], prediction["away_rates"]
    for label, key, unit, multiplier in [
        ("Rolling offensive EPA/play", "epa_per_play", "EPA/play", 1),
        ("Rolling defensive EPA allowed/play", "defensive_epa_per_play", "EPA/play", 1),
        ("Rolling passing EPA/dropback", "pass_epa", "EPA/play", 1),
        ("Rolling defensive passing EPA allowed", "defensive_pass_epa", "EPA/play", 1),
        ("Rolling offensive success rate", "success_rate", "%", 100),
        ("Rolling defensive success allowed", "defensive_success_rate", "%", 100),
        ("Rolling explosive-play rate", "explosive_rate", "%", 100),
        ("Rolling sack rate allowed", "sack_rate", "%", 100),
        ("Rolling defensive sack rate", "pressure_sack_rate", "%", 100),
    ]:
        home_known = advanced and key in hr["available_fields"]
        away_known = advanced and key in ar["available_fields"]
        rows.append({"name": label, "home": round(hr[key] * multiplier, 3) if home_known else None,
                     "away": round(ar[key] * multiplier, 3) if away_known else None,
                     "unit": unit, "status": "used" if home_known or away_known else "unavailable"})
    home_metrics, away_metrics = home_team.get("metrics", {}), away_team.get("metrics", {})
    for label, key, unit in [
        ("Season yards per play", "yards_per_play", "yards/play"),
        ("Season yards per play allowed", "yards_per_play_allowed", "yards/play"),
        ("Season passing yards per attempt", "pass_yards_per_attempt", "yards/attempt"),
        ("Season passing yards per attempt allowed", "pass_yards_per_attempt_allowed", "yards/attempt"),
        ("Season rushing yards per attempt", "rush_yards_per_attempt", "yards/attempt"),
        ("Season plays per game", "plays_per_game", "plays/game"),
        ("Season turnover margin", "turnover_margin_per_game", "per game"),
    ]:
        home, away = _number(home_metrics.get(key)), _number(away_metrics.get(key))
        rows.append({"name": label, "home": home, "away": away, "unit": unit,
                     "status": "context" if home is not None or away is not None else "unavailable"})
    return rows


def _methodology(league, advanced):
    variables = [
        {"name": "Opponent-adjusted team strength", "status": "used", "importance": "Core", "description": "Regularized ratings separate a team's scoring margin from opponent strength. Recent games receive gradually greater weight; blowout margins are capped during fitting.", "source_url": "https://www.espn.com/"},
        {"name": "Offensive and defensive scoring", "status": "used", "importance": "Core", "description": "Separate offense and defense ratings project combined scoring against this opponent, with shrinkage toward league average.", "source_url": "https://www.espn.com/"},
        {"name": "Home field and neutral sites", "status": "used", "importance": "Supporting", "description": "A weak home-field prior is adjusted by the historical margin fit. Neutral-site games receive no home advantage.", "source_url": "https://www.espn.com/"},
        {"name": "Net EPA per play and passing EPA", "status": "used" if advanced else "unavailable", "importance": "Core when available", "description": "NFL play-by-play measures the value of each play. Strictly earlier-week rolling offensive and defensive EPA and passing EPA enter the regularized fit. No EPA is inferred from ordinary yardage.", "source_url": "https://nflfastr.com/articles/beginners_guide.html"},
        {"name": "Success and explosive-play rates", "status": "used" if advanced else "unavailable", "importance": "Supporting", "description": "Rolling offensive and defensive consistency and big-play rates enter the NFL fit when historical play-by-play coverage is sufficient.", "source_url": "https://nflfastr.com/articles/beginners_guide.html"},
        {"name": "Pass protection and pass rush", "status": "used" if advanced else "unavailable", "importance": "Supporting", "description": "Observed sack rates enter the NFL model as a limited pressure proxy. True pressure rate, offensive-line availability, and pass-block win rate are not measured here.", "source_url": "https://nflfastr.com/"},
        {"name": "QB availability and injuries", "status": "context", "importance": "Critical check", "description": "Fresh injury reports are checked against the latest game's primary passer when known; uncertainty for that passer blocks a best-bet designation. A recent passer is not a confirmed starter. Missing reports do not mean healthy, and no unsupported injury point value is invented.", "source_url": "https://www.nfl.com/injuries/"},
        {"name": "Yards per play, pace, turnovers, third downs", "status": "context", "importance": "Supporting context", "description": "Measured season rates help inspect each matchup. They are not added to fitted predictions without matching historical as-of inputs, and volatile turnover/red-zone outcomes are not over-weighted.", "source_url": "https://www.espn.com/"},
        {"name": "Weather, rest, travel and lineup changes", "status": "context", "importance": "Final checks", "description": "Rest and venue context are shown where available. Forecast wind/rain, travel effects and confirmed starters are not assigned numerical adjustments in this version.", "source_url": "https://www.weather.gov/"},
        {"name": "Bookmaker line, price and freshness", "status": "used", "importance": "Required", "description": "Evaluate the actual line and price at one named bookmaker, including push outcomes. Market prices never train the team model; a pick needs positive estimated value after a conservative three-point stress test.", "source_url": "https://the-odds-api.com/liveapi/guides/v4/"},
    ]
    config = CONFIG[league]
    return {
        "summary": "Opponent-adjusted, regularized football ratings with expanding weekly validation" + (" and historical NFL EPA/success/sack features." if advanced else ". Advanced per-play features are unavailable for this slate; this is a scoring-based baseline."),
        "variables": variables,
        "selection_rules": [f"At least {config['minimum_team']} prior games per team and {config['minimum_holdout']} chronological holdout games.",
                            f"At least {config['minimum_edge'] * 100:g} percentage points of estimated edge and ${config['minimum_ev']:g} EV per $100, plus positive EV after a three-point adverse model shift.",
                            "Bookmaker quote no older than 26 hours, with a valid timestamp and no failed-refresh flag. Started games are excluded.",
                            "The corresponding score model must beat a simple league-average/home-field baseline on chronological holdouts.",
                            "At most one qualifying wager per game. No pick is forced when the data or estimated edge is weak."],
        "limitations": ["Probability and EV estimates are provisional, not demonstrated profitable betting returns.",
                        "Score errors use a discretized normal approximation. Pushes are included, but football's exact key-number frequencies and correlated scoring are not fully calibrated.",
                        "QB changes, missing injury reports and weather can overwhelm a statistical edge. CFB injury coverage is especially incomplete.",
                        "Season-to-date estimates can be unstable after coaching/roster changes. Compare the listed price with the price actually offered before considering a wager."],
        "sources": [{"name": "ESPN schedules and statistics", "url": "https://www.espn.com/"},
                    {"name": "nflverse / nflfastR play-by-play", "url": "https://nflfastr.com/articles/beginners_guide.html"},
                    {"name": "The Odds API", "url": "https://the-odds-api.com/liveapi/guides/v4/"}],
    }


def find_football_bets(snapshot, top_n=5, now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    league = snapshot.get("league", "nfl")
    if league not in CONFIG:
        raise ValueError("league must be nfl or cfb")
    config = CONFIG[league]
    history = _history(snapshot, now)
    ratings = FootballRatings(history, league)
    validation = validate_history(history, league)
    picks, watchlist, projections = [], [], []
    teams = snapshot.get("teams", {})
    for game in snapshot.get("games", []):
        kickoff = _time(game.get("game_time"))
        if not kickoff or kickoff <= now or str(game.get("status", "")).lower() in ("live", "in", "post", "final", "completed", "canceled", "cancelled", "postponed"):
            continue
        home, away = str(game["home_team_id"]), str(game["away_team_id"])
        home_team, away_team = teams.get(home, {}), teams.get(away, {})
        home_n, away_n = ratings.counts[home], ratings.counts[away]
        prediction = ratings.predict(game)
        mean_uncertainty = sqrt(1 / (home_n + 4) + 1 / (away_n + 4))
        margin_sigma = max(config["margin_sigma"], validation["margin_rmse"] or 0) * sqrt(1 + mean_uncertainty ** 2)
        total_sigma = max(config["total_sigma"], validation["total_rmse"] or 0) * sqrt(1 + mean_uncertainty ** 2)
        injury_alerts = _qb_alerts(home_team) + _qb_alerts(away_team)
        reasons = []
        if snapshot.get("history_complete") is False:
            reasons.append("Season history is incomplete because a source fetch failed")
        if min(home_n, away_n) < config["minimum_team"]:
            reasons.append(f"Too few prior games: away {away_n}, home {home_n}; need {config['minimum_team']} each")
        if validation["holdout_games"] < config["minimum_holdout"]:
            reasons.append("Not enough chronological validation games")
        reasons.extend(injury_alerts)
        advanced_n = min(prediction["home_rates"]["games"], prediction["away_rates"]["games"])
        coverage = min(60, round(min(home_n, away_n) / config["minimum_team"] * 60))
        coverage += 20 if home_team.get("metrics") and away_team.get("metrics") else 0
        coverage += 10 if game.get("odds") else 0
        coverage += 10 if ratings.advanced and advanced_n >= 4 else 0
        cautions = ["Provisional model; historical betting returns and probability calibration have not been established."]
        if league == "cfb":
            cautions.append("Reliable current college injury and starter coverage is incomplete.")
        if not ratings.advanced:
            cautions.append("No historical play-by-play efficiency features are available; the prediction uses scoring ratings.")
        factors = [f"Opponent-adjusted projection: {game['away_team']} {prediction['away_score']:.1f}, {game['home_team']} {prediction['home_score']:.1f}.",
                   f"Historical sample: {away_n} away-team games, {home_n} home-team games.",
                   "Neutral site: no home-field advantage." if game.get("neutral_site") else "Home-field advantage is included in the margin fit."]
        if ratings.advanced:
            hr, ar = prediction["home_rates"], prediction["away_rates"]
            factors.append(f"Prior-game, shrunk net EPA/play: away {ar['epa_per_play'] - ar['defensive_epa_per_play']:+.3f}; home {hr['epa_per_play'] - hr['defensive_epa_per_play']:+.3f}.")
        if home_team.get("recent_primary_passer") or away_team.get("recent_primary_passer"):
            factors.append(f"Most recent primary passers: away {away_team.get('recent_primary_passer') or 'unavailable'}; home {home_team.get('recent_primary_passer') or 'unavailable'}. These are not confirmed starters.")
        candidates = _candidates(game, snapshot, prediction, margin_sigma, total_sigma, now)
        eligible, rejected = [], []
        for candidate in candidates:
            candidate_reasons = list(reasons)
            age = candidate["odds_age_hours"]
            if age is None:
                candidate_reasons.append("Bookmaker quote timestamp is unavailable")
            elif age < -0.08:
                candidate_reasons.append("Bookmaker quote has an invalid future timestamp")
            elif age > 26:
                candidate_reasons.append(f"Bookmaker quote is {age:g} hours old; limit is 26 hours")
            if game.get("odds", {}).get("stale") or snapshot.get("odds_status", {}).get("stale"):
                candidate_reasons.append("The latest odds refresh failed; cached prices need confirmation")
            score_type = "total" if candidate["market"] == "total" else "margin"
            model_mae = validation[f"{score_type}_mae"]
            baseline_mae = validation[f"baseline_{score_type}_mae"]
            if model_mae is not None and baseline_mae is not None and model_mae >= baseline_mae:
                candidate_reasons.append("This model has not beaten the simple baseline on held-out score errors")
            if candidate["edge"] < config["minimum_edge"] or candidate["expected_value"] < config["minimum_ev"]:
                candidate_reasons.append("Estimated price advantage is below the selection threshold")
            if candidate["conservative_ev"] <= 0:
                candidate_reasons.append("Estimated value does not survive a three-point adverse model shift")
            candidate.update({"coverage": coverage, "factors": factors, "cautions": cautions, "reasons": candidate_reasons})
            (rejected if candidate_reasons else eligible).append(candidate)
        eligible.sort(key=lambda c: c["conservative_ev"], reverse=True)
        rejected.sort(key=lambda c: c["expected_value"], reverse=True)
        if eligible:
            picks.append(eligible[0])
        elif rejected:
            watchlist.append(rejected[0])
        else:
            reasons.append("No matched bookmaker lines and prices are available")
        home_win, _, _ = margin_settlement(prediction["margin"], margin_sigma, 0, league != "cfb")
        separator = "vs" if game.get("neutral_site") else "@"
        projections.append({"game_id": str(game["game_id"]), "matchup": f"{game['away_team']} {separator} {game['home_team']}",
                            "game_time": game["game_time"], "home_team": game["home_team"], "away_team": game["away_team"],
                            "projected_home_score": round(prediction["home_score"], 1), "projected_away_score": round(prediction["away_score"], 1),
                            "fair_home_spread": round(-prediction["margin"], 1), "fair_total": round(prediction["total"], 1),
                            "home_win_probability": round(home_win, 4), "coverage": coverage,
                            "history_games": {"home": home_n, "away": away_n},
                            "metrics": {"home": home_team.get("metrics", {}), "away": away_team.get("metrics", {})},
                            "injuries": {"home": _display_injuries(home_team), "away": _display_injuries(away_team)},
                            "recent_primary_passers": {"home": home_team.get("recent_primary_passer"), "away": away_team.get("recent_primary_passer")},
                            "matchup_metrics": _matchup_metrics(home_team, away_team, prediction, ratings.advanced),
                            "rest": game.get("rest", {}), "venue": game.get("venue", {}), "neutral_site": game.get("neutral_site", False),
                            "status": "qualified" if eligible else "watch" if candidates else "no_odds", "reasons": [] if eligible else reasons or (rejected[0]["reasons"] if rejected else [])})
    picks.sort(key=lambda c: c["conservative_ev"], reverse=True)
    watchlist.sort(key=lambda c: c["expected_value"], reverse=True)
    selected = picks[:max(0, min(20, top_n))]
    return {
        "league": league, "title": "NFL Weekly Best Bets" if league == "nfl" else "College Football Weekly Best Bets",
        "model_version": MODEL_VERSION, "date": snapshot.get("date"), "generated_at": now.isoformat(),
        "week_label": snapshot.get("week_label"), "week_start": snapshot.get("week_start"), "week_end": snapshot.get("week_end"),
        "count": len(selected), "picks": selected, "watchlist": watchlist[:12], "games": sorted(projections, key=lambda g: g["game_time"]),
        "methodology": _methodology(league, ratings.advanced), "validation": validation,
        "coverage": {"games_total": len(projections), "games_with_odds": sum(g["status"] != "no_odds" for g in projections),
                     "games_with_history": sum(min(g["history_games"].values()) >= config["minimum_team"] for g in projections),
                     "advanced_history_games": ratings.advanced_games},
        "no_bet_reason": None if selected else "No upcoming game currently clears the model's data, price, and uncertainty checks. Watchlist entries are not recommendations.",
        "source_health": snapshot.get("source_health", []), "warnings": snapshot.get("warnings", []),
        "odds_status": snapshot.get("odds_status", {}),
    }
