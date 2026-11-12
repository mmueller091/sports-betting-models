"""Beat the Streak model for the probability of at least one hit."""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from models.bts_model import MODEL_VERSION, predict_probability, prepare_calibration


LEAGUE_AVG = 0.248
LEAGUE_AB_PER_PA = 0.88
MARKET_WEIGHT = max(0.0, min(float(os.getenv("BTS_MARKET_WEIGHT", "0.12")), 0.30))


def _pa_per_lineup_spot(order: int) -> float:
    means = {
        1: 4.62,
        2: 4.48,
        3: 4.34,
        4: 4.22,
        5: 4.10,
        6: 3.98,
        7: 3.85,
        8: 3.72,
        9: 3.58,
    }
    return means.get(order, 3.95)


def plate_appearance_distribution(mean_pa: float) -> dict[int, float]:
    """Approximate the batter's full-game PA distribution around its mean."""
    mean_pa = _clip(mean_pa, 2.8, 5.2)
    sigma = 0.78
    weights = {
        pa: math.exp(-0.5 * ((pa - mean_pa) / sigma) ** 2)
        for pa in range(2, 8)
    }
    total = sum(weights.values())
    return {pa: weight / total for pa, weight in weights.items()}


def prob_hit_from_opportunities(
    hit_per_ab: float,
    ab_per_pa: float,
    pa_distribution: dict[int, float],
) -> float:
    """Integrate P(1+ H) over uncertain plate appearances and at-bats."""
    per_pa_hit = _clip(hit_per_ab, 0.01, 0.60) * _clip(ab_per_pa, 0.65, 0.98)
    no_hit = sum(
        probability * ((1.0 - per_pa_hit) ** pa)
        for pa, probability in pa_distribution.items()
    )
    return _clip(1.0 - no_hit, 0.02, 0.98)


def prob_hit_in_n_pa(avg: float, n_pa: float) -> float:
    """Compatibility helper using an opportunity distribution, not fractional AB."""
    return round(
        prob_hit_from_opportunities(avg, LEAGUE_AB_PER_PA, plate_appearance_distribution(n_pa)),
        4,
    )


def score_batter_hit_prob(
    batter: dict,
    opposing_pitcher_data: dict,
    park_factor: int,
    weather_adj: float,
    lineup_support: float,
    opposing_team_context: Optional[dict] = None,
    lineup_confirmed: bool = True,
    calibration_rows: Optional[Iterable[dict]] = None,
) -> dict:
    """Estimate a batter's probability of recording at least one hit."""
    stats = batter.get("stats", {})
    previous_stats = batter.get("previous_stats", {})
    recent = batter.get("recent", {})
    xstats = batter.get("xstats", {})
    vs_pitcher = batter.get("vs_pitcher", {})
    team_context = opposing_team_context or {}

    order = int(batter.get("batting_order", 5) or 5)
    factors = []

    batter_skill, recent_rate = _estimate_batter_skill(
        stats, previous_stats, recent, xstats
    )
    factors.append(f"Talent: {batter_skill:.3f} hit/AB")
    if _valid_rate(xstats.get("xba")):
        factors.append(f"xBA: {float(xstats['xba']):.3f}")

    pitcher_hand = opposing_pitcher_data.get("hand", "R")
    hand_split = batter.get("vs_hand", {}).get(
        "vs_lhp" if pitcher_hand == "L" else "vs_rhp", {}
    )
    batter_vs_hand = _split_rate(hand_split, batter_skill, prior_strength=220)
    if int(hand_split.get("ab", 0) or 0) >= 40:
        factors.append(
            f"vs {pitcher_hand}HP: {batter_vs_hand:.3f} "
            f"({int(hand_split.get('ab', 0))} AB, shrunk)"
        )

    starter_allowed, starter_split = _estimate_pitcher_allowed(
        opposing_pitcher_data, batter.get("bats", "R")
    )
    starter_matchup = _log5(batter_vs_hand, starter_split, LEAGUE_AVG)

    bullpen_allowed = _estimate_bullpen_allowed(team_context)
    bullpen_matchup = _log5(batter_skill, bullpen_allowed, LEAGUE_AVG)
    expected_starter_innings = _expected_starter_innings(opposing_pitcher_data)
    starter_share = _clip(expected_starter_innings / 9.0, 0.38, 0.72)
    hit_per_ab = starter_matchup * starter_share + bullpen_matchup * (1.0 - starter_share)

    if starter_allowed < LEAGUE_AVG - 0.010:
        factors.append(f"Tough starter: {starter_split:.3f} BAA")
    elif starter_allowed > LEAGUE_AVG + 0.010:
        factors.append(f"Hittable starter: {starter_split:.3f} BAA")
    if abs(bullpen_allowed - LEAGUE_AVG) >= 0.012:
        label = "soft" if bullpen_allowed > LEAGUE_AVG else "strong"
        factors.append(f"{label.title()} bullpen: {bullpen_allowed:.3f} BAA")

    ab_per_pa = _estimate_ab_per_pa(stats, previous_stats)
    vp_pa = int(vs_pitcher.get("pa", 0) or 0)
    vp_hits = int(vs_pitcher.get("hits", 0) or 0)
    bvp_rate = None
    if vp_pa > 0:
        vp_ab = max(vp_hits, int(round(vp_pa * ab_per_pa)))
        bvp_rate = _beta_rate(vp_hits, vp_ab, starter_matchup, 200)
        bvp_weight = min(0.08, vp_pa / (vp_pa + 300.0) * 0.20)
        hit_per_ab += (bvp_rate - starter_matchup) * bvp_weight
        if vp_pa >= 10:
            factors.append(f"BvP: {bvp_rate:.3f} ({vp_pa} PA, heavily shrunk)")

    defense_oaa = float(batter.get("opp_defense_oaa", 0.0) or 0.0)
    defense_adjustment = _clip(-defense_oaa / 30.0, -0.004, 0.004)
    hit_per_ab += defense_adjustment
    if abs(defense_adjustment) >= 0.001:
        factors.append("Opposing defense adjustment")

    park_factor = int(park_factor or 100)
    park_multiplier = 1.0 + _clip((park_factor - 100) * 0.0025, -0.04, 0.05)
    hit_per_ab *= park_multiplier
    if abs(park_factor - 100) >= 4:
        label = "hit-friendly" if park_factor > 100 else "hit-suppressing"
        factors.append(f"Park: {park_factor} ({label})")

    weather_adjustment = _clip(float(weather_adj or 0.0) * 0.0015, -0.004, 0.004)
    hit_per_ab += weather_adjustment
    if abs(weather_adjustment) >= 0.001:
        factors.append("Weather affects contact environment")

    bullpen_fatigue = float(
        team_context.get("bullpen", {}).get("fatigue_index", 0.0) or 0.0
    )
    fatigue_adjustment = _clip(bullpen_fatigue * 0.0008, 0.0, 0.0025)
    hit_per_ab += fatigue_adjustment * (1.0 - starter_share)
    if bullpen_fatigue >= 1.5:
        factors.append("Opposing bullpen taxed")

    hit_per_ab = _clip(hit_per_ab, 0.160, 0.370)
    expected_pa = _estimate_pa(
        order,
        bool(batter.get("is_home", False)),
        lineup_support,
        batter.get("team_context", {}),
    )
    pa_distribution = plate_appearance_distribution(expected_pa)
    baseline_probability = prob_hit_from_opportunities(
        hit_per_ab, ab_per_pa, pa_distribution
    )

    prop = batter.get("prop", {})
    market_probability = _market_probability(prop)
    if market_probability is not None and MARKET_WEIGHT > 0:
        baseline_probability = (
            baseline_probability * (1.0 - MARKET_WEIGHT)
            + market_probability * MARKET_WEIGHT
        )
        factors.append(f"No-vig market check: {market_probability:.1%}")

    data_quality = _data_quality(
        stats=stats,
        previous_stats=previous_stats,
        xstats=xstats,
        pitcher_data=opposing_pitcher_data,
        hand_split=hand_split,
        team_context=team_context,
        lineup_confirmed=lineup_confirmed,
        market_probability=market_probability,
    )

    features = {
        "raw_probability": round(baseline_probability, 6),
        "batter_skill": round(batter_skill, 6),
        "batter_vs_hand": round(batter_vs_hand, 6),
        "starter_allowed_rate": round(starter_allowed, 6),
        "starter_split_rate": round(starter_split, 6),
        "bullpen_allowed_rate": round(bullpen_allowed, 6),
        "recent_rate": round(recent_rate, 6),
        "bvp_rate": round(bvp_rate, 6) if bvp_rate is not None else None,
        "bvp_pa": vp_pa,
        "expected_pa": round(expected_pa, 6),
        "ab_per_pa": round(ab_per_pa, 6),
        "batting_order": order,
        "starter_expected_innings": round(expected_starter_innings, 6),
        "starter_share": round(starter_share, 6),
        "park_hit_factor": park_factor,
        "weather_adjustment": round(weather_adjustment, 6),
        "defense_oaa_per_game": round(defense_oaa, 6),
        "bullpen_fatigue": round(bullpen_fatigue, 6),
        "lineup_support": round(lineup_support, 6),
        "market_probability": market_probability,
        "lineup_confirmed": 1 if lineup_confirmed else 0,
        "data_quality": data_quality,
    }
    hit_probability, model_source = predict_probability(
        features, baseline_probability, calibration_rows
    )

    return {
        "batter_id": batter.get("id"),
        "name": batter.get("name", "Unknown"),
        "batting_order": order,
        "adjusted_avg": round(hit_per_ab, 3),
        "hit_probability": hit_probability,
        "raw_probability": round(baseline_probability, 4),
        "data_quality": data_quality,
        "confidence": data_quality,
        "model_source": model_source,
        "model_version": MODEL_VERSION,
        "n_pa_estimated": round(expected_pa, 2),
        "pa_distribution": {str(k): round(v, 4) for k, v in pa_distribution.items()},
        "ab_per_pa": round(ab_per_pa, 3),
        "factors": factors,
        "features": features,
        "vs_pitcher_pa": vp_pa,
        "vs_pitcher_avg": round(bvp_rate, 3) if bvp_rate is not None else None,
        "vs_pitcher_hits": vp_hits if vp_pa > 0 else None,
    }


def find_hit_candidates(
    snapshot: dict,
    calibration_rows: Optional[Iterable[dict]] = None,
) -> list[dict]:
    """Score the complete eligible batter-game pool for unbiased tracking."""
    candidates = []
    prepared_calibration = prepare_calibration(calibration_rows or [])

    for game in snapshot.get("games", []):
        weather = game.get("weather", {})
        weather_adj = weather.get("weather_factor", {}).get("total_adjustment", 0.0)
        if weather.get("is_indoor", False):
            weather_adj = 0.0

        common = {
            "game_id": game.get("game_id"),
            "matchup": f"{game.get('away_team')} @ {game.get('home_team')}",
            "venue": game.get("venue", "Unknown"),
            "game_time": game.get("game_time", ""),
            "park_factor": game.get("hit_park_factor", game.get("park_factor", 100)),
            "weather_adj": weather_adj,
            "umpire": game.get("umpire", {}),
        }

        candidates.extend(_score_lineup(
            lineup=game.get("away_lineup", []),
            team=game.get("away_team"),
            opposing_pitcher=game.get("home_probable_pitcher"),
            opposing_pitcher_data=game.get("home_pitcher_data", {}),
            own_team_context=game.get("away_team_context", {}),
            opposing_team_context=game.get("home_team_context", {}),
            opposing_defense_oaa=game.get("home_defense_oaa", 0.0),
            projected=game.get("away_lineup_projected", False),
            is_home=False,
            common=common,
            calibration_rows=prepared_calibration,
        ))
        candidates.extend(_score_lineup(
            lineup=game.get("home_lineup", []),
            team=game.get("home_team"),
            opposing_pitcher=game.get("away_probable_pitcher"),
            opposing_pitcher_data=game.get("away_pitcher_data", {}),
            own_team_context=game.get("home_team_context", {}),
            opposing_team_context=game.get("away_team_context", {}),
            opposing_defense_oaa=game.get("away_defense_oaa", 0.0),
            projected=game.get("home_lineup_projected", False),
            is_home=True,
            common=common,
            calibration_rows=prepared_calibration,
        ))

    candidates.sort(
        key=lambda item: (item["hit_probability"], item["data_quality"]),
        reverse=True,
    )
    for rank, candidate in enumerate(candidates, start=1):
        candidate["slate_rank"] = rank
    return candidates


def find_best_hit_props(
    snapshot: dict,
    top_n: int = 5,
    calibration_rows: Optional[Iterable[dict]] = None,
) -> list[dict]:
    candidates = find_hit_candidates(snapshot, calibration_rows=calibration_rows)
    return select_best_hit_props(candidates, top_n)


def select_best_hit_props(candidates: list[dict], top_n: int = 5) -> list[dict]:
    """Select the highest-probability distinct players from a scored slate."""
    unique = []
    seen_ids = set()
    for candidate in candidates:
        batter_id = candidate.get("batter_id")
        if batter_id in seen_ids:
            continue
        seen_ids.add(batter_id)
        unique.append(candidate)
        if len(unique) >= top_n:
            break
    return unique


def find_best_bts_pair(candidates: list[dict]) -> Optional[dict]:
    """Return the two distinct players with the highest joint hit probability."""
    pool = candidates[:30]
    best = None
    for index, first in enumerate(pool):
        for second in pool[index + 1:]:
            if first.get("batter_id") == second.get("batter_id"):
                continue
            joint = first["hit_probability"] * second["hit_probability"]
            quality = min(first.get("data_quality", 0), second.get("data_quality", 0))
            score = (joint, quality)
            if best is None or score > best[0]:
                best = (score, first, second)
    if best is None:
        return None
    _, first, second = best
    return {
        "players": [_pair_player(first), _pair_player(second)],
        "joint_probability": round(
            first["hit_probability"] * second["hit_probability"], 4
        ),
        "assumption": "independent conditional hit outcomes",
    }


def _score_lineup(
    lineup: list[dict],
    team: str,
    opposing_pitcher: Optional[dict],
    opposing_pitcher_data: dict,
    own_team_context: dict,
    opposing_team_context: dict,
    opposing_defense_oaa: float,
    projected: bool,
    is_home: bool,
    common: dict,
    calibration_rows,
) -> list[dict]:
    support = _lineup_support_score(lineup)
    pitcher_name = (opposing_pitcher or {}).get("name", "TBD")
    is_day_game = _is_day_game(common.get("game_time", ""))
    scored = []

    for batter in lineup:
        stats = batter.get("stats", {})
        previous = batter.get("previous_stats", {})
        if (
            int(stats.get("at_bats", 0) or 0) < 20
            and int(previous.get("at_bats", 0) or 0) < 100
            and not _valid_rate(batter.get("xstats", {}).get("xba"))
        ):
            continue

        enriched = {
            **batter,
            "is_home": is_home,
            "is_day_game": is_day_game,
            "opp_defense_oaa": opposing_defense_oaa,
            "team_context": own_team_context,
        }
        result = score_batter_hit_prob(
            enriched,
            opposing_pitcher_data,
            common["park_factor"],
            common["weather_adj"],
            support,
            opposing_team_context=opposing_team_context,
            lineup_confirmed=not projected,
            calibration_rows=calibration_rows,
        )
        prop = batter.get("prop", {})
        umpire = common.get("umpire", {})
        scored.append({
            **result,
            "game_id": common.get("game_id"),
            "lineup_projected": projected,
            "lineup_confirmed": not projected,
            "team": team,
            "facing_pitcher": pitcher_name,
            "matchup": common.get("matchup"),
            "venue": common.get("venue"),
            "game_time": common.get("game_time"),
            "prop_line": prop.get("line", 0.5),
            "prop_over_odds": prop.get("over_odds"),
            "prop_under_odds": prop.get("under_odds"),
            "market_probability": _market_probability(prop),
            "umpire": umpire.get("name"),
            "umpire_tendency": umpire.get("tendency_label", ""),
        })
    return scored


def _estimate_batter_skill(
    stats: dict,
    previous_stats: dict,
    recent: dict,
    xstats: dict,
) -> tuple[float, float]:
    previous_ab = int(previous_stats.get("at_bats", 0) or 0)
    previous_hits = int(previous_stats.get("hits", 0) or 0)
    previous_rate = _beta_rate(previous_hits, previous_ab, LEAGUE_AVG, 220)

    current_ab = int(stats.get("at_bats", 0) or 0)
    current_hits = int(stats.get("hits", 0) or 0)
    prior_strength = _clip(previous_ab * 0.45, 140, 320) if previous_ab else 260
    season_rate = _beta_rate(current_hits, current_ab, previous_rate, prior_strength)

    xba = _as_rate(xstats.get("xba"))
    if xba is not None:
        xba_rate = _shrink_rate(xba, current_ab * 0.75, previous_rate, 220)
        skill = season_rate * 0.45 + xba_rate * 0.55
    else:
        skill = season_rate

    recent_ab = int(recent.get("at_bats", 0) or 0)
    recent_hits = int(recent.get("hits", 0) or 0)
    recent_rate = _beta_rate(recent_hits, recent_ab, skill, 120)
    skill += _clip((recent_rate - skill) * 0.20, -0.004, 0.004)
    return _clip(skill, 0.185, 0.350), _clip(recent_rate, 0.170, 0.370)


def _estimate_pitcher_allowed(pitcher_data: dict, batter_hand: str) -> tuple[float, float]:
    stats = pitcher_data.get("season_stats", {})
    previous = pitcher_data.get("previous_stats", {})
    xstats = pitcher_data.get("xstats", {})

    previous_bf = int(previous.get("batters_faced", 0) or 0)
    previous_rate = _shrink_rate(
        _as_rate(previous.get("avg_against")) or LEAGUE_AVG,
        previous_bf * LEAGUE_AB_PER_PA,
        LEAGUE_AVG,
        260,
    )
    current_bf = int(stats.get("batters_faced", 0) or 0)
    observed = _as_rate(stats.get("avg_against"))
    allowed = _shrink_rate(
        observed if observed is not None else previous_rate,
        current_bf * LEAGUE_AB_PER_PA,
        previous_rate,
        _clip(previous_bf * 0.35, 160, 320) if previous_bf else 260,
    )

    xba_against = _as_rate(xstats.get("xba_against"))
    if xba_against is not None:
        expected = _shrink_rate(xba_against, current_bf * 0.70, previous_rate, 220)
        allowed = allowed * 0.45 + expected * 0.55

    split = _pitcher_split_for_hand(pitcher_data.get("splits", {}), batter_hand)
    split_rate = _split_rate(split, allowed, prior_strength=220)
    return _clip(allowed, 0.180, 0.340), _clip(split_rate, 0.175, 0.350)


def _estimate_bullpen_allowed(team_context: dict) -> float:
    quality = team_context.get("bullpen_quality", {})
    observed = _as_rate(quality.get("bullpen_avg_against"))
    innings = float(quality.get("bullpen_ip", 0.0) or 0.0)
    batters_faced = float(quality.get("bullpen_batters_faced", 0.0) or innings * 4.25)
    if observed is None:
        return LEAGUE_AVG
    return _clip(
        _shrink_rate(observed, batters_faced * LEAGUE_AB_PER_PA, LEAGUE_AVG, 300),
        0.205,
        0.305,
    )


def _expected_starter_innings(pitcher_data: dict) -> float:
    stats = pitcher_data.get("season_stats", {})
    previous = pitcher_data.get("previous_stats", {})
    starts = int(stats.get("games_started", 0) or 0)
    innings = float(stats.get("innings_pitched", 0.0) or 0.0)
    previous_starts = int(previous.get("games_started", 0) or 0)
    previous_innings = float(previous.get("innings_pitched", 0.0) or 0.0)

    current_ip_per_start = innings / starts if starts else None
    previous_ip_per_start = previous_innings / previous_starts if previous_starts else 5.2
    if current_ip_per_start is None:
        estimate = previous_ip_per_start
    else:
        reliability = starts / (starts + 8.0)
        estimate = current_ip_per_start * reliability + previous_ip_per_start * (1.0 - reliability)
    return _clip(estimate, 3.5, 6.5)


def _estimate_ab_per_pa(stats: dict, previous_stats: dict) -> float:
    current_ab = int(stats.get("at_bats", 0) or 0)
    current_pa = int(stats.get("plate_appearances", 0) or 0)
    previous_ab = int(previous_stats.get("at_bats", 0) or 0)
    previous_pa = int(previous_stats.get("plate_appearances", 0) or 0)
    prior_rate = _beta_rate(previous_ab, previous_pa, LEAGUE_AB_PER_PA, 180)
    return _clip(_beta_rate(current_ab, current_pa, prior_rate, 180), 0.76, 0.95)


def _estimate_pa(
    order: int,
    is_home: bool,
    lineup_support: float,
    team_context: dict,
) -> float:
    mean = _pa_per_lineup_spot(order)
    if is_home:
        mean -= 0.06
    recent_runs = team_context.get("recent_form", {}).get("runs_for_per_game_last_10")
    if recent_runs is not None:
        mean += _clip((float(recent_runs) - 4.3) * 0.035, -0.10, 0.10)
    mean += _clip(lineup_support * 0.10, -0.08, 0.12)
    return _clip(mean, 3.20, 4.85)


def _data_quality(
    stats: dict,
    previous_stats: dict,
    xstats: dict,
    pitcher_data: dict,
    hand_split: dict,
    team_context: dict,
    lineup_confirmed: bool,
    market_probability: Optional[float],
) -> int:
    score = 0.0
    score += min(int(stats.get("at_bats", 0) or 0) / 250.0, 1.0) * 22
    score += min(int(previous_stats.get("at_bats", 0) or 0) / 350.0, 1.0) * 10
    score += 15 if _valid_rate(xstats.get("xba")) else 0
    pitcher_stats = pitcher_data.get("season_stats", {})
    pitcher_sample = int(pitcher_stats.get("batters_faced", 0) or 0)
    score += min(pitcher_sample / 250.0, 1.0) * 15
    score += min(int(hand_split.get("ab", 0) or 0) / 180.0, 1.0) * 10
    score += 8 if team_context.get("bullpen_quality") else 0
    score += 15 if lineup_confirmed else 5
    score += 5 if market_probability is not None else 0
    return int(round(_clip(score, 0, 100)))


def _lineup_support_score(lineup: list[dict]) -> float:
    if not lineup:
        return 0.0
    values = []
    for batter in lineup:
        stats = batter.get("stats", {})
        previous = batter.get("previous_stats", {})
        current_ops = float(stats.get("ops", 0.0) or 0.0)
        previous_ops = float(previous.get("ops", 0.0) or 0.0)
        if current_ops > 0:
            values.append(current_ops - 0.720)
        elif previous_ops > 0:
            values.append(previous_ops - 0.720)
    return round(sum(values) / len(values), 3) if values else 0.0


def _market_probability(prop: dict) -> Optional[float]:
    value = _as_rate(prop.get("market_probability"))
    if value is None or not 0.30 <= value <= 0.95:
        return None
    return round(value, 6)


def _split_rate(split: dict, prior_rate: float, prior_strength: float) -> float:
    observed = _as_rate(split.get("avg"))
    sample = int(split.get("ab", 0) or 0)
    if observed is None or sample <= 0:
        return prior_rate
    return _shrink_rate(observed, sample, prior_rate, prior_strength)


def _pitcher_split_for_hand(splits: dict, batter_hand: str) -> dict:
    if batter_hand == "L":
        return splits.get("vs_lhb", {})
    if batter_hand == "R":
        return splits.get("vs_rhb", {})
    left = splits.get("vs_lhb", {})
    right = splits.get("vs_rhb", {})
    left_ab = int(left.get("ab", 0) or 0)
    right_ab = int(right.get("ab", 0) or 0)
    total = left_ab + right_ab
    if total <= 0:
        return {}
    return {
        "avg": (
            float(left.get("avg", LEAGUE_AVG)) * left_ab
            + float(right.get("avg", LEAGUE_AVG)) * right_ab
        ) / total,
        "ab": total,
    }


def _pair_player(candidate: dict) -> dict:
    return {
        "batter_id": candidate.get("batter_id"),
        "name": candidate.get("name"),
        "team": candidate.get("team"),
        "matchup": candidate.get("matchup"),
        "hit_probability": candidate.get("hit_probability"),
        "lineup_confirmed": candidate.get("lineup_confirmed"),
    }


def _is_day_game(game_time: str) -> bool:
    try:
        if not game_time:
            return False
        dt = datetime.fromisoformat(game_time.replace("Z", "+00:00"))
        eastern = dt.astimezone(timezone(timedelta(hours=-4)))
        return eastern.hour < 17
    except (TypeError, ValueError):
        return False


def _log5(hitter_rate: float, allowed_rate: float, league_rate: float) -> float:
    value = _logit(hitter_rate) + _logit(allowed_rate) - _logit(league_rate)
    return _sigmoid(value)


def _logit(value: float) -> float:
    value = _clip(value, 0.001, 0.999)
    return math.log(value / (1.0 - value))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-value))


def _beta_rate(successes: float, trials: float, prior_rate: float, prior_strength: float) -> float:
    trials = max(float(trials or 0.0), 0.0)
    successes = _clip(float(successes or 0.0), 0.0, trials) if trials else 0.0
    return (successes + prior_rate * prior_strength) / (trials + prior_strength)


def _shrink_rate(rate: float, sample: float, prior_rate: float, prior_strength: float) -> float:
    sample = max(float(sample or 0.0), 0.0)
    return (rate * sample + prior_rate * prior_strength) / (sample + prior_strength)


def _valid_rate(value) -> bool:
    parsed = _as_rate(value)
    return parsed is not None and parsed > 0


def _as_rate(value) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or not 0 < parsed < 1:
        return None
    return parsed


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(float(value), high))
