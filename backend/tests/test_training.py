import pickle
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from data.bts_tracker import record_bts_predictions
from models.bts_model import FEATURE_NAMES
from train_bts_model import train


class TrainingPipelineTests(unittest.TestCase):
    def test_time_split_training_writes_usable_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "history.sqlite3"
            artifact_path = root / "model.pkl"
            start = date(2026, 4, 1)

            for day_index in range(40):
                game_date = start + timedelta(days=day_index)
                candidates = []
                for rank in range(1, 11):
                    probability = 0.55 + (10 - rank) * 0.03
                    features = {
                        name: probability if "probability" in name else float(rank)
                        for name in FEATURE_NAMES
                    }
                    candidates.append({
                        "game_id": 1000 + day_index,
                        "batter_id": rank,
                        "name": f"Player {rank}",
                        "team": "Away",
                        "matchup": "Away @ Home",
                        "game_time": f"{game_date.isoformat()}T23:00:00Z",
                        "batting_order": rank if rank <= 9 else 1,
                        "lineup_confirmed": True,
                        "raw_probability": probability,
                        "hit_probability": probability,
                        "data_quality": 90,
                        "slate_rank": rank,
                        "model_version": "bts-2.0",
                        "model_source": "baseline",
                        "features": features,
                    })
                record_bts_predictions(game_date, candidates, db_path=db_path)

            connection = sqlite3.connect(db_path)
            try:
                rows = connection.execute(
                    "SELECT game_date, game_id, player_id, raw_probability FROM predictions"
                ).fetchall()
                for game_date, game_id, player_id, probability in rows:
                    outcome_index = (game_id + player_id) % 10
                    result = "win" if outcome_index < round(probability * 10) else "loss"
                    connection.execute(
                        """
                        UPDATE predictions SET result = ?
                        WHERE game_date = ? AND game_id = ? AND player_id = ?
                        """,
                        (result, game_date, game_id, player_id),
                    )
                connection.commit()
            finally:
                connection.close()

            report = train(
                min_rows=300,
                db_path=db_path,
                artifact_path=artifact_path,
            )
            self.assertTrue(artifact_path.exists())
            self.assertEqual(report["training_rows"], 400)
            with artifact_path.open("rb") as handle:
                artifact = pickle.load(handle)
            sample = [[0.7] * len(FEATURE_NAMES)]
            probability = artifact["model"].predict_proba(sample)[0][1]
            self.assertGreater(probability, 0)
            self.assertLess(probability, 1)


if __name__ == "__main__":
    unittest.main()
