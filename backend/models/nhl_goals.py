"""
NHL goal scorer props model.
Scores skaters for probability of scoring 1+ goal.

Inputs:
- Season goal/shooting profile
- MoneyPuck ixG and goals-above-expected
- Power-play role
- Opposing goalie quality including GSAx
- Schedule fatigue
"""

import math
from datetime import datetime, timezone
from models.nhl_bets import MODEL_VERSION


def prob_score_in_game(goals_per_game: float, shots_per_game: float, goalie_save_pct: float) -> float:
    goalie_factor = 1 - goalie_save_pct
    xg_per_game = shots_per_game * goalie_factor
    lambda_val = xg_per_game * 0.60 + goals_per_game * 0.40
    return round(min(1 - math.exp(-lambda_val), 0.95), 4)


def score_goal_scorer(
    player: dict,
    opposing_goalie: dict,
    is_back_to_back: bool,
    schedule_spot: dict,
    team_pp_pct: float,
) -> dict:
    name = player.get("name", "Unknown")
    pos = player.get("position", "F")
    gp = max(player.get("games_played", 1), 1)
    goals = player.get("goals", 0)
    shots = player.get("shots", 0)
    pp_goals = player.get("pp_goals", 0)

    goals_per_game = player.get("goals_per_game", goals / gp)
    shots_per_game = player.get("shots_per_game", shots / gp)
    pp_pct = round(pp_goals / max(goals, 1), 2) if goals > 0 else 0.0
    shooting_pct = round(goals / max(shots, 1), 3) if shots > 0 else 0.0

    measured_goalie_sv = opposing_goalie.get("save_pct")
    goalie_sv = measured_goalie_sv if measured_goalie_sv is not None else 0.906
    goalie_name = opposing_goalie.get("name", "Unknown")
    goalie_xg = opposing_goalie.get("xg_stats", {})
    goalie_gsax = goalie_xg.get("gsax", 0.0)

    factors = [
        f"Goals/G: {goals_per_game:.3f}",
        f"Shots/G: {shots_per_game:.2f}",
        f"Shooting%: {shooting_pct:.1%}",
        (f"vs {goalie_name} (.{int(goalie_sv*1000)} SV%)" if measured_goalie_sv is not None
         else f"vs {goalie_name}: SV% unavailable; league-average assumption"),
    ]
    if goalie_gsax:
        factors.append(f"Goalie GSAx: {goalie_gsax:+.1f}")
    if pp_goals > 0:
        factors.append(f"PP goals: {pp_goals} ({pp_pct:.0%} of goals on PP)")

    xg_stats = player.get("xg_stats", {})
    ixg_per_60 = xg_stats.get("ixg_per_60", 0)
    goals_above_expected = xg_stats.get("goals_above_expected", 0.0)
    pp_toi_pct = xg_stats.get("pp_toi_pct", 0.0)

    if ixg_per_60 > 0:
        toi_per_game = xg_stats.get("toi_per_game", 18 * 60) / 60
        ixg_per_game = ixg_per_60 * toi_per_game / 60
        shot_based_prob = prob_score_in_game(goals_per_game, shots_per_game, goalie_sv)
        ixg_prob = 1 - math.exp(-ixg_per_game)
        goal_prob = ixg_prob * 0.60 + shot_based_prob * 0.40
        factors.append(f"ixG/game: {ixg_per_game:.3f}")
    else:
        goal_prob = prob_score_in_game(goals_per_game, shots_per_game, goalie_sv)

    if goals_above_expected:
        goal_prob += max(-0.03, min(goals_above_expected * 0.006, 0.035))
        factors.append(f"Goals above expected: {goals_above_expected:+.1f}")

    if pp_toi_pct:
        goal_prob += min(pp_toi_pct * 0.08, 0.035)
        factors.append(f"PP TOI share: {pp_toi_pct:.0%}")

    if team_pp_pct:
        goal_prob += max((team_pp_pct - 20.0) / 100 * 0.08, -0.02)
        factors.append(f"Team PP: {team_pp_pct:.1f}%")

    if goalie_gsax < -5:
        goal_prob += 0.03
    elif goalie_gsax > 10:
        goal_prob -= 0.02

    if is_back_to_back:
        goal_prob *= 0.90
        factors.append("Back-to-back (-10%)")
    if schedule_spot.get("games_last_6", 0) >= 4:
        goal_prob *= 0.94
        factors.append("4-in-6 fatigue")

    if pos == "D":
        goal_prob *= 0.45

    confidence = min(
        100,
        40
        + (20 if goals_per_game > 0.30 else 0)
        + (15 if shots_per_game > 3.0 else 0)
        + (10 if pp_goals > 3 else 0)
        + (10 if ixg_per_60 > 0.75 else 0)
        + (15 if gp >= 40 else 5),
    )

    return {
        "player_id": player.get("id"),
        "name": name,
        "position": pos,
        "goals": goals,
        "assists": player.get("assists", 0),
        "points": player.get("points", 0),
        "goals_per_game": round(goals_per_game, 3),
        "shots_per_game": round(shots_per_game, 2),
        "shooting_pct": shooting_pct,
        "pp_goals": pp_goals,
        "goal_probability": round(min(max(goal_prob, 0.02), 0.95), 4),
        "confidence": confidence,
        "opposing_goalie": goalie_name,
        "opposing_goalie_sv": measured_goalie_sv,
        "model_version": MODEL_VERSION,
        "factors": factors,
    }


def find_nhl_goal_scorers(snapshot: dict, top_n: int = 5, now: datetime | None = None) -> list[dict]:
    candidates = []
    now = now or datetime.now(timezone.utc)

    for game in snapshot.get("games", []):
        try:
            start = datetime.fromisoformat(game["game_time"].replace("Z", "+00:00"))
            if start.tzinfo is None or start <= now or game.get("status") in ("LIVE", "CRIT", "FINAL", "OFF"):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        away = game["away_team"]
        home = game["home_team"]
        game_time = game.get("game_time", "")
        venue = game.get("venue", "")

        away_goalie = game.get("away_team_stats", {}).get("starting_goalie", {})
        home_goalie = game.get("home_team_stats", {}).get("starting_goalie", {})
        away_b2b = game.get("away_back_to_back", False)
        home_b2b = game.get("home_back_to_back", False)
        away_schedule = game.get("away_schedule_spot", {})
        home_schedule = game.get("home_schedule_spot", {})
        away_pp = game.get("away_standings", {}).get("pp_pct", 20.0)
        home_pp = game.get("home_standings", {}).get("pp_pct", 20.0)

        away_scorers = game.get("away_scorers") or game.get("away_team_stats", {}).get("top_scorers", [])
        home_scorers = game.get("home_scorers") or game.get("home_team_stats", {}).get("top_scorers", [])

        for player in away_scorers:
            scored = score_goal_scorer(
                player,
                home_goalie,
                away_b2b,
                away_schedule,
                away_pp,
            )
            candidates.append({
                **scored,
                "team": away,
                "matchup": f"{away} @ {home}",
                "venue": venue,
                "game_time": game_time,
                "prop_line": 0.5,
            })

        for player in home_scorers:
            scored = score_goal_scorer(
                player,
                away_goalie,
                home_b2b,
                home_schedule,
                home_pp,
            )
            candidates.append({
                **scored,
                "team": home,
                "matchup": f"{away} @ {home}",
                "venue": venue,
                "game_time": game_time,
                "prop_line": 0.5,
            })

    candidates.sort(key=lambda x: x["goal_probability"], reverse=True)

    seen = set()
    unique = []
    for candidate in candidates:
        player_id = candidate.get("player_id")
        if player_id in seen:
            continue
        seen.add(player_id)
        unique.append(candidate)
        if len(unique) >= top_n:
            break

    return unique
