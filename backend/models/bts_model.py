"""Versioned inference and online calibration for Beat the Streak probabilities."""

from __future__ import annotations

import math
import os
import pickle
from pathlib import Path
from typing import Iterable, Optional


MODEL_VERSION = "bts-2.0"
ARTIFACT_PATH = Path(os.getenv("BTS_ARTIFACT_PATH", str(Path(__file__).parent / "artifacts" / "bts_model.pkl")))

# Keep this list stable. Training artifacts record the exact feature order.
FEATURE_NAMES = (
    "raw_probability",
    "batter_skill",
    "batter_vs_hand",
    "starter_allowed_rate",
    "starter_split_rate",
    "bullpen_allowed_rate",
    "recent_rate",
    "bvp_rate",
    "bvp_pa",
    "expected_pa",
    "ab_per_pa",
    "batting_order",
    "starter_expected_innings",
    "starter_share",
    "park_hit_factor",
    "weather_adjustment",
    "defense_oaa_per_game",
    "bullpen_fatigue",
    "lineup_support",
    "market_probability",
    "lineup_confirmed",
    "data_quality",
)

_artifact_cache: dict = {"mtime": None, "artifact": None}


def predict_probability(
    features: dict,
    fallback_probability: float,
    calibration_rows: Optional[Iterable[dict]] = None,
) -> tuple[float, str]:
    """Return a calibrated probability and the source used to produce it."""
    trained = _predict_trained(features)
    if trained is not None:
        return round(_clip(trained, 0.02, 0.98), 4), "trained"

    calibrated, applied = calibrate_probability(fallback_probability, calibration_rows or [])
    source = "baseline_calibrated" if applied else "baseline"
    return round(_clip(calibrated, 0.02, 0.98), 4), source


def calibrate_probability(probability: float, rows: Iterable[dict]) -> tuple[float, bool]:
    """
    Calibrate from strictly earlier predictions with isotonic regression.

    A gradual blend protects the model while the history is still small. Full
    calibration is reached after 1,250 settled batter-games.
    """
    prepared = prepare_calibration(rows)
    if not prepared:
        return probability, False

    calibrated = _interpolate(prepared["points"], probability)
    weight = prepared["weight"]
    blended = probability * (1.0 - weight) + calibrated * weight
    return _clip(blended, 0.02, 0.98), weight > 0


def prepare_calibration(rows: Iterable[dict]) -> Optional[dict]:
    """Prepare reusable calibration points for a full daily slate."""
    if isinstance(rows, dict) and "points" in rows:
        return rows

    clean = []
    for row in rows:
        predicted = _as_float(row.get("raw_probability"))
        result = row.get("result")
        if predicted is None or result not in ("win", "loss"):
            continue
        clean.append((predicted, 1.0 if result == "win" else 0.0))

    if len(clean) < 250:
        return None

    points = _isotonic_points(clean)
    if not points:
        return None

    weight = min(1.0, max(0.0, (len(clean) - 250) / 1000.0))
    return {"points": points, "weight": weight, "sample_size": len(clean)}


def model_status() -> dict:
    artifact = _load_artifact()
    if not artifact:
        return {
            "version": MODEL_VERSION,
            "source": "baseline",
            "trained_model_available": False,
        }
    return {
        "version": artifact.get("model_version", MODEL_VERSION),
        "source": "trained",
        "trained_model_available": True,
        "trained_at": artifact.get("trained_at"),
        "training_rows": artifact.get("training_rows"),
        "metrics": artifact.get("metrics", {}),
    }


def _predict_trained(features: dict) -> Optional[float]:
    artifact = _load_artifact()
    if not artifact:
        return None
    if tuple(artifact.get("feature_names", ())) != FEATURE_NAMES:
        return None

    model = artifact.get("model")
    if model is None:
        return None

    row = [[_feature_value(features.get(name)) for name in FEATURE_NAMES]]
    try:
        return float(model.predict_proba(row)[0][1])
    except Exception:
        return None


def _load_artifact() -> Optional[dict]:
    try:
        mtime = os.path.getmtime(ARTIFACT_PATH)
    except OSError:
        _artifact_cache.update({"mtime": None, "artifact": None})
        return None

    if _artifact_cache["mtime"] == mtime:
        return _artifact_cache["artifact"]

    try:
        with ARTIFACT_PATH.open("rb") as handle:
            artifact = pickle.load(handle)
    except Exception:
        artifact = None

    _artifact_cache.update({"mtime": mtime, "artifact": artifact})
    return artifact


def _isotonic_points(rows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    rows.sort(key=lambda item: item[0])
    # Start with modest quantile buckets so individual outcomes cannot create
    # a staircase of zeroes and ones at the top of the range.
    bucket_size = max(25, len(rows) // 30)
    blocks = []
    for start in range(0, len(rows), bucket_size):
        bucket = rows[start:start + bucket_size]
        blocks.append({
            "x_sum": sum(item[0] for item in bucket),
            "y_sum": sum(item[1] for item in bucket),
            "weight": len(bucket),
        })

    index = 0
    while index < len(blocks) - 1:
        left = blocks[index]
        right = blocks[index + 1]
        if left["y_sum"] / left["weight"] <= right["y_sum"] / right["weight"]:
            index += 1
            continue
        left["x_sum"] += right["x_sum"]
        left["y_sum"] += right["y_sum"]
        left["weight"] += right["weight"]
        del blocks[index + 1]
        index = max(index - 1, 0)

    return [
        (block["x_sum"] / block["weight"], block["y_sum"] / block["weight"])
        for block in blocks
    ]


def _interpolate(points: list[tuple[float, float]], probability: float) -> float:
    if probability <= points[0][0]:
        return points[0][1]
    if probability >= points[-1][0]:
        return points[-1][1]
    for left, right in zip(points, points[1:]):
        if left[0] <= probability <= right[0]:
            width = right[0] - left[0]
            if width <= 0:
                return (left[1] + right[1]) / 2
            ratio = (probability - left[0]) / width
            return left[1] + ratio * (right[1] - left[1])
    return probability


def _feature_value(value) -> float:
    parsed = _as_float(value)
    return math.nan if parsed is None else parsed


def _as_float(value) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(float(value), high))
