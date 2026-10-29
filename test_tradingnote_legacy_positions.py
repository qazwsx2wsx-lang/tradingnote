import json
import tempfile
import unittest
from pathlib import Path

from tradingnote_legacy_positions import (
    archive_legacy_positions_file,
    load_legacy_positions,
)


class LegacyPositionsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "positions.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_load_missing_file_returns_empty_list(self):
        self.assertEqual(load_legacy_positions(self.path), [])

    def test_load_real_sample_shape(self):
        self.path.write_text(
            json.dumps([
                {
                    "ticker": "2301", "shares": 5000, "entry_price": 271.0,
                    "entry_date": "2026-10-17", "note": "", "name": "光寶科",
                    "market": "TWSE", "id": "a872a8c6", "updated_at": "2026-10-17",
                }
            ]),
            encoding="utf-8",
        )
        positions = load_legacy_positions(self.path)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].ticker, "2301")
        self.assertEqual(positions[0].shares, 5000)
        self.assertEqual(positions[0].entry_price, 271.0)
        self.assertEqual(positions[0].name, "光寶科")

    def test_archive_renames_file(self):
        self.path.write_text("[]", encoding="utf-8")
        archive_legacy_positions_file(self.path)
        self.assertFalse(self.path.exists())
        self.assertTrue(self.path.with_name("positions.json.bak").exists())

    def test_archive_is_noop_when_file_missing(self):
        archive_legacy_positions_file(self.path)  # should not raise
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
