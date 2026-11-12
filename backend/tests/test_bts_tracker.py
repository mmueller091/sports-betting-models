import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

from data.bts_tracker import (
    bts_performance_summary,
    grade_bts_predictions,
    load_calibration_rows,
    record_bts_predictions,
)


class BTSTrackerTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_and_grades_exact_player_games(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "history.sqlite3"
            candidates = [
                self.candidate(1, 101, 0.78, 1),
                self.candidate(2, 101, 0.72, 2),
            ]
            recorded = record_bts_predictions(
                date(2026, 8, 13), candidates, db_path=db_path
            )
            self.assertEqual(recorded, 2)

            finals = {("Away", "Home"): {"game_id": 101}}
            boxscore = {
                1: {"hits": 1, "at_bats": 4, "plate_appearances": 4},
                2: {"hits": 0, "at_bats": 4, "plate_appearances": 4},
            }
            with patch(
                "data.bts_tracker.get_final_scores_for_date",
                AsyncMock(return_value=finals),
            ), patch(
                "data.bts_tracker.get_boxscore_batting",
                AsyncMock(return_value=boxscore),
            ):
                graded = await grade_bts_predictions(
                    today=date(2026, 8, 14), db_path=db_path
                )

            self.assertEqual(graded, 2)
            summary = bts_performance_summary(db_path)
            self.assertEqual(summary["top_pick"]["wins"], 1)
            self.assertEqual(summary["top_two"]["losses"], 1)
            self.assertEqual(summary["daily_pair"]["both_hit"], 0)
            calibration = load_calibration_rows(date(2026, 8, 14), db_path=db_path)
            self.assertEqual(len(calibration), 2)

    @staticmethod
    def candidate(player_id, game_id, probability, rank):
        return {
            "game_id": game_id,
            "batter_id": player_id,
            "name": f"Player {player_id}",
            "team": "Away",
            "matchup": "Away @ Home",
            "game_time": "2026-08-13T23:00:00Z",
            "batting_order": rank,
            "lineup_confirmed": True,
            "raw_probability": probability,
            "hit_probability": probability,
            "data_quality": 90,
            "slate_rank": rank,
            "model_version": "bts-2.0",
            "model_source": "baseline",
            "features": {"raw_probability": probability},
        }


if __name__ == "__main__":
    unittest.main()
