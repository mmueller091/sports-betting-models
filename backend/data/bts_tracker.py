"""SQLite-backed prediction history and evaluation for Beat the Streak."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from data.mlb_stats import (
    get_boxscore_batting,
    get_final_scores_for_date,
)
from models.bts_model import MODEL_VERSION


from data.storage import STORE_DIR
DB_PATH = STORE_DIR / "bts_history.sqlite3"
VOID_AFTER_DAYS = 5
MIN_TRAINING_ROWS = 2000


def record_bts_predictions(
    target_date: date,
    candidates: list[dict],
    selected_count: int = 5,
    db_path: Optional[Path] = None,
) -> int:
    """Upsert the latest pregame prediction for every player-game on a slate."""
    if not candidates:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for candidate in candidates:
        game_id = candidate.get("game_id")
        player_id = candidate.get("batter_id")
        if game_id is None or player_id is None:
            continue
        rank = int(candidate.get("slate_rank", 9999) or 9999)
        rows.append((
            target_date.isoformat(),
            int(game_id),
            int(player_id),
            candidate.get("name"),
            candidate.get("team"),
            candidate.get("matchup"),
            candidate.get("game_time"),
            int(candidate.get("batting_order", 0) or 0),
            1 if candidate.get("lineup_confirmed") else 0,
            _float_or_none(candidate.get("raw_probability")),
            _float_or_none(candidate.get("hit_probability")),
            int(candidate.get("data_quality", 0) or 0),
            rank,
            1 if rank <= selected_count else 0,
            candidate.get("model_version", MODEL_VERSION),
            candidate.get("model_source", "baseline"),
            json.dumps(candidate.get("features", {}), separators=(",", ":")),
            now,
            now,
        ))

    if not rows:
        return 0

    with _connect(db_path) as connection:
        connection.executemany(
            """
            INSERT INTO predictions (
                game_date, game_id, player_id, name, team, matchup, game_time,
                batting_order, lineup_confirmed, raw_probability,
                hit_probability, data_quality, slate_rank, selected,
                model_version, model_source, features_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_date, game_id, player_id) DO UPDATE SET
                name = excluded.name,
                team = excluded.team,
                matchup = excluded.matchup,
                game_time = excluded.game_time,
                batting_order = excluded.batting_order,
                lineup_confirmed = excluded.lineup_confirmed,
                raw_probability = excluded.raw_probability,
                hit_probability = excluded.hit_probability,
                data_quality = excluded.data_quality,
                slate_rank = excluded.slate_rank,
                selected = excluded.selected,
                model_version = excluded.model_version,
                model_source = excluded.model_source,
                features_json = excluded.features_json,
                updated_at = excluded.updated_at
            WHERE predictions.result IS NULL
            """,
            rows,
        )
    return len(rows)


def load_calibration_rows(
    before_date: date,
    model_version: str = MODEL_VERSION,
    db_path: Optional[Path] = None,
) -> list[dict]:
    """Load settled predictions strictly before a slate to prevent leakage."""
    with _connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT raw_probability, result
            FROM predictions
            WHERE game_date < ?
              AND model_version = ?
              AND result IN ('win', 'loss')
              AND raw_probability IS NOT NULL
            ORDER BY game_date, game_id, player_id
            """,
            (before_date.isoformat(), model_version),
        ).fetchall()
    return [dict(row) for row in rows]


def load_training_rows(db_path: Optional[Path] = None) -> list[dict]:
    """Return all settled feature rows in chronological order for model training."""
    with _connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT game_date, game_id, player_id, result, features_json
            FROM predictions
            WHERE result IN ('win', 'loss')
            ORDER BY game_date, game_id, player_id
            """
        ).fetchall()
    output = []
    for row in rows:
        try:
            features = json.loads(row["features_json"] or "{}")
        except json.JSONDecodeError:
            continue
        output.append({
            "game_date": row["game_date"],
            "game_id": row["game_id"],
            "player_id": row["player_id"],
            "result": row["result"],
            "features": features,
        })
    return output


async def grade_bts_predictions(
    today: Optional[date] = None,
    db_path: Optional[Path] = None,
) -> int:
    """Grade every pending historical player-game against its exact boxscore."""
    current_date = today or date.today()
    with _connect(db_path) as connection:
        pending_rows = connection.execute(
            """
            SELECT DISTINCT game_date, game_id
            FROM predictions
            WHERE result IS NULL AND game_date < ?
            ORDER BY game_date, game_id
            """,
            (current_date.isoformat(),),
        ).fetchall()

    by_date = defaultdict(list)
    for row in pending_rows:
        by_date[row["game_date"]].append(int(row["game_id"]))

    graded = 0
    for date_string, game_ids in by_date.items():
        game_date = date.fromisoformat(date_string)
        stale = (current_date - game_date).days > VOID_AFTER_DAYS
        try:
            finals = await get_final_scores_for_date(game_date)
        except Exception:
            continue
        final_ids = {int(entry["game_id"]) for entry in finals.values()}
        gradeable = [game_id for game_id in game_ids if game_id in final_ids]

        tasks = [get_boxscore_batting(game_id) for game_id in gradeable]
        results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
        boxscores = {
            game_id: result
            for game_id, result in zip(gradeable, results)
            if not isinstance(result, Exception) and result
        }

        with _connect(db_path) as connection:
            for game_id in game_ids:
                if game_id not in boxscores:
                    if stale:
                        cursor = connection.execute(
                            """
                            UPDATE predictions
                            SET result = 'void', graded_at = ?
                            WHERE game_date = ? AND game_id = ? AND result IS NULL
                            """,
                            (datetime.now(timezone.utc).isoformat(), date_string, game_id),
                        )
                        graded += cursor.rowcount
                    continue

                player_rows = connection.execute(
                    """
                    SELECT player_id FROM predictions
                    WHERE game_date = ? AND game_id = ? AND result IS NULL
                    """,
                    (date_string, game_id),
                ).fetchall()
                boxscore = boxscores[game_id]
                for player_row in player_rows:
                    player_id = int(player_row["player_id"])
                    batting = boxscore.get(player_id)
                    if not batting or int(batting.get("plate_appearances", 0) or 0) <= 0:
                        result = "void"
                        hits = at_bats = plate_appearances = 0
                    else:
                        hits = int(batting.get("hits", 0) or 0)
                        at_bats = int(batting.get("at_bats", 0) or 0)
                        plate_appearances = int(batting.get("plate_appearances", 0) or 0)
                        result = "win" if hits >= 1 else "loss"
                    connection.execute(
                        """
                        UPDATE predictions
                        SET result = ?, hits = ?, at_bats = ?, plate_appearances = ?,
                            graded_at = ?
                        WHERE game_date = ? AND game_id = ? AND player_id = ?
                          AND result IS NULL
                        """,
                        (
                            result,
                            hits,
                            at_bats,
                            plate_appearances,
                            datetime.now(timezone.utc).isoformat(),
                            date_string,
                            game_id,
                            player_id,
                        ),
                    )
                    graded += 1
    return graded


def bts_performance_summary(db_path: Optional[Path] = None) -> dict:
    with _connect(db_path) as connection:
        rows = [
            dict(row) for row in connection.execute(
                """
                SELECT game_date, game_id, player_id, name, hit_probability,
                       raw_probability, data_quality, slate_rank,
                       lineup_confirmed, model_version, result
                FROM predictions
                WHERE result IN ('win', 'loss')
                ORDER BY game_date, slate_rank
                """
            ).fetchall()
        ]
        pending = connection.execute(
            "SELECT COUNT(*) FROM predictions WHERE result IS NULL"
        ).fetchone()[0]
        total = connection.execute(
            "SELECT COUNT(*) FROM predictions"
        ).fetchone()[0]

    top_pick = _daily_top(rows, 1)
    top_two = _daily_top(rows, 2)
    confirmed = [row for row in rows if row["lineup_confirmed"]]
    dates = sorted({row["game_date"] for row in rows})

    return {
        "all_candidates": _summarize(rows),
        "top_pick": _summarize(top_pick),
        "top_two": _summarize(top_two),
        "confirmed_only": _summarize(confirmed),
        "daily_pair": _pair_summary(rows),
        "streak": _streak_summary(top_pick),
        "pending_count": int(pending),
        "predictions_stored": int(total),
        "training_rows": len(rows),
        "minimum_training_rows": MIN_TRAINING_ROWS,
        "days_tracked": len(dates),
    }


def _summarize(rows: list[dict]) -> dict:
    if not rows:
        return {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "hit_rate": None,
            "avg_predicted_prob": None,
            "brier_score": None,
            "log_loss": None,
            "calibration": [],
        }
    wins = sum(row["result"] == "win" for row in rows)
    probabilities = [_bounded_probability(row["hit_probability"]) for row in rows]
    outcomes = [1.0 if row["result"] == "win" else 0.0 for row in rows]
    brier = sum((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes)) / len(rows)
    log_loss = -sum(
        outcome * math.log(probability) + (1.0 - outcome) * math.log(1.0 - probability)
        for probability, outcome in zip(probabilities, outcomes)
    ) / len(rows)
    return {
        "count": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "hit_rate": round(wins / len(rows), 4),
        "avg_predicted_prob": round(sum(probabilities) / len(rows), 4),
        "brier_score": round(brier, 5),
        "log_loss": round(log_loss, 5),
        "calibration": _calibration(rows),
    }


def _calibration(rows: list[dict]) -> list[dict]:
    buckets = (
        (0.00, 0.60),
        (0.60, 0.65),
        (0.65, 0.70),
        (0.70, 0.75),
        (0.75, 0.80),
        (0.80, 0.85),
        (0.85, 1.01),
    )
    output = []
    for low, high in buckets:
        selected = [
            row for row in rows
            if row.get("hit_probability") is not None
            and low <= float(row["hit_probability"]) < high
        ]
        if not selected:
            continue
        wins = sum(row["result"] == "win" for row in selected)
        output.append({
            "range": f"{int(low * 100)}-{int(min(high, 1.0) * 100)}%",
            "count": len(selected),
            "predicted": round(
                sum(float(row["hit_probability"]) for row in selected) / len(selected), 4
            ),
            "actual": round(wins / len(selected), 4),
        })
    return output


def _pair_summary(rows: list[dict]) -> dict:
    top_two_rows = _daily_top(rows, 2)
    by_date = defaultdict(list)
    for row in top_two_rows:
        by_date[row["game_date"]].append(row)
    complete = [pair for pair in by_date.values() if len(pair) == 2]
    if not complete:
        return {
            "days": 0,
            "both_hit": 0,
            "joint_hit_rate": None,
            "avg_joint_predicted": None,
        }
    both_hit = sum(
        pair[0]["result"] == "win" and pair[1]["result"] == "win"
        for pair in complete
    )
    predicted = [
        float(pair[0]["hit_probability"]) * float(pair[1]["hit_probability"])
        for pair in complete
    ]
    return {
        "days": len(complete),
        "both_hit": both_hit,
        "joint_hit_rate": round(both_hit / len(complete), 4),
        "avg_joint_predicted": round(sum(predicted) / len(predicted), 4),
    }


def _streak_summary(top_pick_rows: list[dict]) -> dict:
    current = 0
    longest = 0
    for row in sorted(top_pick_rows, key=lambda item: item["game_date"]):
        if row["result"] == "win":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return {"current": current, "longest": longest}


def _daily_top(rows: list[dict], count: int) -> list[dict]:
    """Reconstruct each day's best final pregame rows across staggered starts."""
    by_date = defaultdict(list)
    for row in rows:
        by_date[row["game_date"]].append(row)
    selected = []
    for game_date in sorted(by_date):
        day_rows = sorted(
            by_date[game_date],
            key=lambda row: (
                float(row.get("hit_probability") or 0.0),
                int(row.get("data_quality") or 0),
            ),
            reverse=True,
        )
        seen_players = set()
        for row in day_rows:
            if row["player_id"] in seen_players:
                continue
            seen_players.add(row["player_id"])
            selected.append(row)
            if len(seen_players) >= count:
                break
    return selected


@contextmanager
def _connect(db_path: Optional[Path] = None):
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=20)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        _ensure_schema(connection)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS predictions (
            game_date TEXT NOT NULL,
            game_id INTEGER NOT NULL,
            player_id INTEGER NOT NULL,
            name TEXT,
            team TEXT,
            matchup TEXT,
            game_time TEXT,
            batting_order INTEGER,
            lineup_confirmed INTEGER NOT NULL DEFAULT 0,
            raw_probability REAL,
            hit_probability REAL,
            data_quality INTEGER,
            slate_rank INTEGER,
            selected INTEGER NOT NULL DEFAULT 0,
            model_version TEXT NOT NULL,
            model_source TEXT,
            features_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            result TEXT,
            hits INTEGER,
            at_bats INTEGER,
            plate_appearances INTEGER,
            graded_at TEXT,
            PRIMARY KEY (game_date, game_id, player_id)
        );
        CREATE INDEX IF NOT EXISTS idx_predictions_result
            ON predictions(result, game_date);
        CREATE INDEX IF NOT EXISTS idx_predictions_model
            ON predictions(model_version, game_date);
        """
    )


def _bounded_probability(value) -> float:
    parsed = _float_or_none(value)
    if parsed is None:
        return 0.5
    return max(1e-6, min(parsed, 1.0 - 1e-6))


def _float_or_none(value) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
