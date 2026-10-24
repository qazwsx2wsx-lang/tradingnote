import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import tradingnote
from tradingnote_tradejournal import get_trade, list_open_positions


class CliCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"
        self.settings = {}

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, cmd):
        return tradingnote.parse_command(cmd, self.db_path, {}, self.db_path, self.settings)

    def test_add_buy_sell_list_round_trip(self):
        self._run("add 2330 1000 10.0 2026-10-01 9.0")
        positions = list_open_positions(self.db_path)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].shares, 1000)
        self.assertEqual(positions[0].entry_price, 10.0)
        position_id = positions[0].id

        self._run(f"buy {position_id} 500 12.0")
        positions = list_open_positions(self.db_path)
        self.assertEqual(positions[0].shares, 1500)

        self._run(f"sell {position_id} 1500 14.0")
        positions = list_open_positions(self.db_path)
        self.assertEqual(positions, [])  # fully sold -> auto-closed, no longer OPEN

        trade = get_trade(self.db_path, int(position_id))
        self.assertEqual(trade.status, "CLOSED")

    def test_add_requires_stop_loss_arg(self):
        with patch("sys.stdout", new_callable=StringIO) as out:
            self._run("add 2330 1000 10.0 2026-10-01")  # missing stop-loss
        self.assertIn("用法", out.getvalue())
        self.assertEqual(list_open_positions(self.db_path), [])

    def test_edit_updates_stop_loss_and_note_without_touching_shares(self):
        self._run("add 2330 1000 10.0 2026-10-01 9.0")
        position_id = list_open_positions(self.db_path)[0].id

        self._run(f"edit {position_id} 8.5 新停損")
        trade = get_trade(self.db_path, int(position_id))
        self.assertEqual(trade.initial_stop_loss, 8.5)
        self.assertEqual(trade.notes, "新停損")
        self.assertEqual(trade.total_qty, 1000)  # untouched

    def test_remove_is_refused_once_add_has_seeded_an_execution(self):
        # `add` itself always seeds one execution (it represents "I bought these
        # shares just now"), so a position created via `add` is never "empty" --
        # `remove` is refused and the position stays OPEN. The success path
        # (cancelling a genuinely zero-execution trade) is covered directly
        # against the core function in test_tradingnote_tradejournal.py, since
        # there's no CLI command that produces a trade with zero executions.
        self._run("add 2330 1000 10.0 2026-10-01 9.0")
        position_id = list_open_positions(self.db_path)[0].id

        with patch("sys.stdout", new_callable=StringIO) as out:
            self._run(f"remove {position_id}")
        self.assertIn("已經有成交記錄", out.getvalue())
        self.assertEqual(len(list_open_positions(self.db_path)), 1)

    def test_resolve_trade_id_prefix_matching(self):
        self._run("add 2330 1000 10.0 2026-10-01 9.0")
        position_id = list_open_positions(self.db_path)[0].id
        resolved = tradingnote._resolve_trade_id(self.db_path, position_id[:2])
        self.assertEqual(resolved, int(position_id))

    def test_resolve_trade_id_returns_none_for_unknown(self):
        self.assertIsNone(tradingnote._resolve_trade_id(self.db_path, "999"))


class CliMigrationPromptTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.positions_path = self.data_dir / "positions.json"
        self.positions_path.write_text(
            json.dumps([
                {
                    "ticker": "2301", "shares": 5000, "entry_price": 271.0,
                    "entry_date": "2026-10-17", "note": "", "name": "光寶科",
                    "market": "TWSE", "id": "a872a8c6", "updated_at": "2026-10-17",
                }
            ]),
            encoding="utf-8",
        )
        self.db_path = self.data_dir / "history.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_migration_end_to_end_with_stubbed_input(self):
        with patch.object(tradingnote, "POSITIONS_PATH", self.positions_path), \
             patch.object(tradingnote, "HISTORY_DB_PATH", self.db_path), \
             patch("builtins.input", return_value="250"):
            tradingnote._prompt_legacy_migration()

        self.assertFalse(self.positions_path.exists())
        self.assertTrue(self.positions_path.with_name("positions.json.bak").exists())
        positions = list_open_positions(self.db_path)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].ticker, "2301")
        self.assertEqual(positions[0].shares, 5000)

    def test_migration_leaves_file_untouched_on_interrupt(self):
        with patch.object(tradingnote, "POSITIONS_PATH", self.positions_path), \
             patch.object(tradingnote, "HISTORY_DB_PATH", self.db_path), \
             patch("builtins.input", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                tradingnote._prompt_legacy_migration()

        self.assertTrue(self.positions_path.exists())
        self.assertEqual(list_open_positions(self.db_path), [])


if __name__ == "__main__":
    unittest.main()
