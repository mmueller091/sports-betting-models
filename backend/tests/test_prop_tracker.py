import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from data import prop_tracker


class PropTrackerTests(unittest.TestCase):
    def test_normalized_name_and_consensus_no_vig_probability(self):
        lines = [
            {"player": "Ronald Acuna Jr.", "side": "Over", "odds": -180, "line": 0.5, "book": "a", "matchup": "A @ B"},
            {"player": "Ronald Acuna Jr.", "side": "Under", "odds": 140, "line": 0.5, "book": "a", "matchup": "A @ B"},
            {"player": "Ronald Acuna Jr.", "side": "Over", "odds": -170, "line": 0.5, "book": "b", "matchup": "A @ B"},
            {"player": "Ronald Acuna Jr.", "side": "Under", "odds": 135, "line": 0.5, "book": "b", "matchup": "A @ B"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "props.json"
            with patch.object(prop_tracker, "PROPS_FILE", store):
                prop_tracker.record_prop_snapshot(lines, date(2026, 8, 14))
                lookup = prop_tracker.build_player_prop_lookup(date(2026, 8, 14))
        key = prop_tracker.player_lookup_key("Ronald Acuna Jr.")
        self.assertIn(key, lookup)
        self.assertEqual(lookup[key]["books_count"], 2)
        self.assertGreater(lookup[key]["market_probability"], 0.60)
        self.assertLess(lookup[key]["market_probability"], 0.70)

    def test_accented_names_share_a_lookup_key(self):
        self.assertEqual(
            prop_tracker.player_lookup_key("Ronald Acuna Jr."),
            prop_tracker.player_lookup_key("Ronald Acu\u00f1a Jr."),
        )


if __name__ == "__main__":
    unittest.main()
