import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

import tradingnote_gui as gui
from ui import app_paths
from tradingnote_core import Position
from tradingnote_journal import initialize_journal, load_week


class JournalHarness(gui.TradingNoteWindow):
    def __init__(self, positions):
        QtWidgets.QMainWindow.__init__(self)
        self.positions = positions
        root = QtWidgets.QWidget()
        self._central_layout = QtWidgets.QVBoxLayout(root)
        self._central_layout.setContentsMargins(18, 14, 18, 8)
        self.journal_tab = QtWidgets.QWidget()
        self._central_layout.addWidget(self.journal_tab)
        self.setCentralWidget(root)
        self.tabs = QtWidgets.QTabWidget()
        self._build_journal_tab()


class JournalGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"
        self.original_db_path = app_paths.HISTORY_DB_PATH
        app_paths.HISTORY_DB_PATH = self.db_path
        today = date.today()
        previous = today - timedelta(days=1)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """CREATE TABLE daily_prices (
                    date TEXT NOT NULL, ticker TEXT NOT NULL, market TEXT, name TEXT,
                    close REAL, change_pct REAL, volume INTEGER, trading_value REAL,
                    PRIMARY KEY (date, ticker))"""
            )
            rows = []
            for index, ticker in enumerate(("1111", "2222", "3333", "4444"), start=1):
                rows.append((previous.isoformat(), ticker, 10.0 * index))
                rows.append((today.isoformat(), ticker, 10.0 * index + index))
            conn.executemany(
                """INSERT INTO daily_prices
                   (date, ticker, close, volume, trading_value)
                   VALUES (?, ?, ?, 1000, 100000)""",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        self.positions = [
            Position(ticker, index * 100, 1, "2020-01-01", name=f"股票{index}", id=str(index))
            for index, ticker in enumerate(("1111", "2222", "3333", "4444"), start=1)
        ]
        initialize_journal(self.db_path, self.positions, today.isoformat())
        self.window = JournalHarness(self.positions)
        self.window.resize(1200, 700)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        app_paths.HISTORY_DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_week_cards_details_and_auto_save(self):
        self.assertEqual(len(self.window.journal_cards), 7)
        today_index = date.today().weekday()
        card_lines = self.window.journal_cards[today_index].text().splitlines()
        self.assertTrue(any("合計" in line for line in card_lines))
        self.assertEqual(len([line for line in card_lines if line[:4].isdigit()]), 3)
        for index in range(today_index + 1, 7):
            self.assertFalse(self.window.journal_cards[index].isEnabled())
        self.assertEqual(self.window.journal_holdings_table.rowCount(), 4)

        self.window.journal_note_edit.setPlainText("今天保持耐心。\n沒有追高。")
        self.assertTrue(self.window.journal_dirty)
        other_index = max(0, today_index - 1)
        self.window._select_journal_day_index(other_index)
        week = load_week(
            self.db_path,
            date.today() - timedelta(days=today_index),
            self.positions,
            date.today().isoformat(),
        )
        self.assertEqual(week[today_index].note, "今天保持耐心。\n沒有追高。")

        current_week = date.today() - timedelta(days=today_index)
        self.window._journal_previous_week()
        self.assertEqual(self.window.journal_week_start, current_week - timedelta(days=7))
        self.assertTrue(self.window.journal_next_button.isEnabled())
        self.window._journal_next_week()
        self.assertEqual(self.window.journal_week_start, current_week)
        self.window._journal_current_week()
        self.assertEqual(self.window.journal_selected_date, date.today().isoformat())

    def test_responsive_density_and_larger_icons(self):
        self.window.resize(760, 600)
        self.app.processEvents()
        self.assertTrue(self.window._compact_layout)
        self.assertEqual(self.window.journal_cards[0].height(), 132)
        self.assertEqual(self.window.journal_previous_button.text(), "上週")
        self.assertFalse(self.window.journal_jump_label.isVisible())
        self.assertEqual(self.window.journal_previous_button.iconSize().width(), 22)

        self.window.resize(1280, 800)
        self.app.processEvents()
        self.assertFalse(self.window._compact_layout)
        self.assertEqual(self.window.journal_cards[0].height(), 164)
        self.assertEqual(self.window.journal_previous_button.text(), "上一週")
        self.assertTrue(self.window.journal_jump_label.isVisible())


if __name__ == "__main__":
    unittest.main()
