import unittest

from models.bts_model import calibrate_probability, prepare_calibration


class CalibrationTests(unittest.TestCase):
    def test_small_history_does_not_change_probability(self):
        rows = [{"raw_probability": 0.75, "result": "win"}] * 100
        probability, applied = calibrate_probability(0.75, rows)
        self.assertEqual(probability, 0.75)
        self.assertFalse(applied)

    def test_large_history_builds_reusable_monotonic_calibration(self):
        rows = []
        for index in range(1300):
            predicted = 0.65 if index < 650 else 0.80
            cycle = index % 20
            actual_rate = 0.70 if predicted == 0.65 else 0.85
            rows.append({
                "raw_probability": predicted,
                "result": "win" if cycle < int(actual_rate * 20) else "loss",
            })
        prepared = prepare_calibration(rows)
        low, low_applied = calibrate_probability(0.65, prepared)
        high, high_applied = calibrate_probability(0.80, prepared)
        self.assertTrue(low_applied)
        self.assertTrue(high_applied)
        self.assertGreater(high, low)
        self.assertGreater(low, 0.65)


if __name__ == "__main__":
    unittest.main()
