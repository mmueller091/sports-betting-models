"""Train the Beat the Streak classifier from settled SQLite prediction rows."""

from __future__ import annotations

import argparse
import json
import math
import pickle
from datetime import datetime, timezone
from pathlib import Path

from data.bts_tracker import MIN_TRAINING_ROWS, load_training_rows
from models.bts_model import ARTIFACT_PATH, FEATURE_NAMES, MODEL_VERSION


def train_if_ready(
    min_rows: int = MIN_TRAINING_ROWS,
    retrain_every: int = 250,
    db_path: Path | None = None,
    artifact_path: Path = ARTIFACT_PATH,
) -> dict:
    """Train only when enough new settled rows have accumulated."""
    rows = load_training_rows(db_path)
    if len(rows) < min_rows:
        return {"status": "waiting", "rows": len(rows), "minimum": min_rows}

    artifact_path = Path(artifact_path)
    report_path = artifact_path.with_suffix(".json")
    previous_rows = 0
    try:
        previous_rows = int(json.loads(report_path.read_text(encoding="utf-8")).get("training_rows", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    if artifact_path.exists() and len(rows) < previous_rows + retrain_every:
        return {"status": "current", "rows": len(rows), "trained_rows": previous_rows}

    return {"status": "trained", **train(
        min_rows=min_rows,
        db_path=db_path,
        artifact_path=artifact_path,
    )}


def train(
    min_rows: int = MIN_TRAINING_ROWS,
    db_path: Path | None = None,
    artifact_path: Path = ARTIFACT_PATH,
) -> dict:
    try:
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.frozen import FrozenEstimator
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise RuntimeError(
            "scikit-learn is required. Install backend/requirements.txt first."
        ) from exc

    rows = load_training_rows(db_path)
    if len(rows) < min_rows:
        raise RuntimeError(
            f"Need at least {min_rows} settled batter-games; found {len(rows)}."
        )

    dates = sorted({row["game_date"] for row in rows})
    if len(dates) < 30:
        raise RuntimeError(f"Need at least 30 distinct slates; found {len(dates)}.")

    train_cut = dates[max(1, int(len(dates) * 0.70))]
    test_cut = dates[max(2, int(len(dates) * 0.85))]
    train_rows = [row for row in rows if row["game_date"] < train_cut]
    calibration_rows = [
        row for row in rows if train_cut <= row["game_date"] < test_cut
    ]
    test_rows = [row for row in rows if row["game_date"] >= test_cut]

    train_x, train_y = _matrix(train_rows)
    calibration_x, calibration_y = _matrix(calibration_rows)
    test_x, test_y = _matrix(test_rows)

    factories = {
        "logistic": lambda: make_pipeline(
            SimpleImputer(strategy="median"),
            StandardScaler(),
            LogisticRegression(C=0.5, max_iter=2000),
        ),
        "hist_gradient_boosting": lambda: make_pipeline(
            SimpleImputer(strategy="median"),
            HistGradientBoostingClassifier(
                learning_rate=0.04,
                max_iter=250,
                max_leaf_nodes=15,
                min_samples_leaf=35,
                l2_regularization=2.0,
                random_state=42,
            ),
        ),
    }

    selection_metrics = {}
    fitted = {}
    for name, factory in factories.items():
        model = factory()
        model.fit(train_x, train_y)
        probabilities = model.predict_proba(calibration_x)[:, 1]
        selection_metrics[name] = _metrics(
            calibration_y, probabilities, brier_score_loss, log_loss, roc_auc_score
        )
        fitted[name] = model

    selected_name = min(
        selection_metrics,
        key=lambda name: (
            selection_metrics[name]["log_loss"],
            selection_metrics[name]["brier_score"],
        ),
    )
    evaluation_model = CalibratedClassifierCV(
        FrozenEstimator(fitted[selected_name]), method="sigmoid"
    )
    evaluation_model.fit(calibration_x, calibration_y)
    test_probabilities = evaluation_model.predict_proba(test_x)[:, 1]
    holdout_metrics = _metrics(
        test_y, test_probabilities, brier_score_loss, log_loss, roc_auc_score
    )

    # Refit on all data while preserving the latest block exclusively for
    # calibration. The metrics above remain from the untouched holdout block.
    fit_rows = train_rows + calibration_rows
    fit_x, fit_y = _matrix(fit_rows)
    final_base = factories[selected_name]()
    final_base.fit(fit_x, fit_y)
    final_model = CalibratedClassifierCV(
        FrozenEstimator(final_base), method="sigmoid"
    )
    final_model.fit(test_x, test_y)

    artifact = {
        "model": final_model,
        "model_name": selected_name,
        "model_version": MODEL_VERSION,
        "feature_names": FEATURE_NAMES,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_rows": len(rows),
        "training_dates": len(dates),
        "metrics": {
            "selection": selection_metrics,
            "holdout": holdout_metrics,
            "holdout_rows": len(test_rows),
        },
    }
    artifact_path = Path(artifact_path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    with artifact_path.open("wb") as handle:
        pickle.dump(artifact, handle)

    report_path = artifact_path.with_suffix(".json")
    report_path.write_text(
        json.dumps({key: value for key, value in artifact.items() if key != "model"}, indent=2),
        encoding="utf-8",
    )
    return {
        "artifact": str(artifact_path),
        "selected_model": selected_name,
        "training_rows": len(rows),
        "holdout_metrics": holdout_metrics,
    }


def _matrix(rows: list[dict]) -> tuple[list[list[float]], list[int]]:
    features = []
    outcomes = []
    for row in rows:
        values = []
        source = row.get("features", {})
        for name in FEATURE_NAMES:
            value = source.get(name)
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                parsed = math.nan
            values.append(parsed if math.isfinite(parsed) else math.nan)
        features.append(values)
        outcomes.append(1 if row["result"] == "win" else 0)
    return features, outcomes


def _metrics(y_true, probabilities, brier_fn, log_loss_fn, auc_fn) -> dict:
    metrics = {
        "brier_score": round(float(brier_fn(y_true, probabilities)), 6),
        "log_loss": round(float(log_loss_fn(y_true, probabilities)), 6),
    }
    try:
        metrics["roc_auc"] = round(float(auc_fn(y_true, probabilities)), 6)
    except ValueError:
        metrics["roc_auc"] = None
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-rows", type=int, default=MIN_TRAINING_ROWS)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--artifact-path", type=Path, default=ARTIFACT_PATH)
    args = parser.parse_args()
    print(json.dumps(train(
        min_rows=args.min_rows,
        db_path=args.db_path,
        artifact_path=args.artifact_path,
    ), indent=2))
