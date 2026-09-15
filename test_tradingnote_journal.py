import sqlite3
import tempfile
import unittest
from pathlib import Path

from tradingnote_core import Position
from tradingnote_journal import (
    initialize_journal,
    load_week,
    save_journal_entry,
    save_portfolio_snapshot,
)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """CREATE TABLE daily_prices (
                    date TEXT NOT NULL, ticker TEXT NOT NULL, market TEXT, name TEXT,
                    close REAL, change_pct REAL, volume INTEGER, trading_value REAL,
                    PRIMARY KEY (date, ticker))"""
            )
            conn.executemany(
                """INSERT INTO daily_prices
                   (date, ticker, market, name, close, change_pct, volume, trading_value)
                   VALUES (?, ?, 'TWSE', ?, ?, 0, 1000, 100000)""",
                [
                    ("2026-09-04", "1111", "甲", 10.0),
                    ("2026-09-07", "1111", "甲", 11.0),
                    ("2026-09-04", "2222", "乙", 20.0),
                    ("2026-09-07", "2222", "乙", 18.0),
                    ("2026-09-08", "1111", "甲", 12.0),
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def positions():
        return [
            Position("1111", 1000, 9, "2026-01-01", name="甲", id="a"),
            Position("2222", 200, 21, "2026-09-07", name="乙", id="b"),
        ]

    def test_schema_is_idempotent_and_old_days_are_estimated(self):
        self.assertEqual(
            initialize_journal(self.db_path, self.positions(), "2026-09-08"),
            "2026-09-08",
        )
        initialize_journal(self.db_path, self.positions(), "2026-09-08")
        week = load_week(self.db_path, "2026-09-07", self.positions(), "2026-09-08")
        monday = week[0]
        self.assertEqual(monday.holding_source, "estimated")
        self.assertEqual([move.ticker for move in monday.holdings], ["1111", "2222"])
        self.assertEqual(monday.holdings[0].change_pct, 10.0)
        self.assertEqual(monday.holdings[0].change_amount, 1000.0)
        self.assertEqual(monday.holdings[1].change_amount, -400.0)
        self.assertEqual(monday.total_change_amount, 600.0)

    def test_recorded_snapshot_forward_fill_and_empty_snapshot(self):
        initialize_journal(self.db_path, self.positions(), "2026-09-07")
        week = load_week(self.db_path, "2026-09-07", self.positions(), "2026-09-08")
        self.assertEqual(week[1].snapshot_date, "2026-09-07")
        self.assertEqual(len(week[1].holdings), 2)
        save_portfolio_snapshot(self.db_path, "2026-09-08", [])
        week = load_week(self.db_path, "2026-09-07", self.positions(), "2026-09-08")
        self.assertEqual(week[1].holding_source, "recorded")
        self.assertEqual(week[1].snapshot_date, "2026-09-08")
        self.assertEqual(week[1].holdings, ())

    def test_snapshot_overwrite_and_journal_round_trip(self):
        initialize_journal(self.db_path, self.positions(), "2026-09-07")
        save_portfolio_snapshot(self.db_path, "2026-09-07", self.positions()[:1])
        save_journal_entry(self.db_path, "2026-09-07", "焦慮，但沒有追價。\n等待訊號。")
        monday = load_week(
            self.db_path, "2026-09-07", self.positions(), "2026-09-08"
        )[0]
        self.assertEqual(len(monday.holdings), 1)
        self.assertIn("沒有追價", monday.note)
        self.assertTrue(monday.market_open)
        save_journal_entry(self.db_path, "2026-09-07", "")
        self.assertEqual(
            load_week(self.db_path, "2026-09-07", self.positions(), "2026-09-08")[0].note,
            "",
        )

    def test_weekend_and_missing_previous_price(self):
        initialize_journal(self.db_path, self.positions(), "2026-09-07")
        week = load_week(self.db_path, "2026-09-07", self.positions(), "2026-09-13")
        saturday = week[5]
        self.assertFalse(saturday.market_open)
        self.assertIsNone(saturday.total_change_amount)
        tuesday_second = next(move for move in week[1].holdings if move.ticker == "2222")
        self.assertIsNone(tuesday_second.close)
        self.assertIsNone(tuesday_second.change_amount)

    def test_empty_new_install_without_price_table(self):
        empty_db = Path(self.temp_dir.name) / "empty.db"
        initialize_journal(empty_db, [], "2026-09-07")
        week = load_week(empty_db, "2026-09-07", [], "2026-09-07")
        self.assertEqual(len(week), 7)
        self.assertFalse(week[0].market_open)
        self.assertEqual(week[0].holdings, ())


if __name__ == "__main__":
    unittest.main()
