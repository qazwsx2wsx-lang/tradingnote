import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

import tradingnote_tradejournal as tj
from ui import app_paths
from ui.dialogs.futures_import import FuturesImportDialog
from ui.pages.futures_backtest_page import FuturesBacktestPage


def _closed(db_path, side, entry, exit_price, entry_date, exit_date, code="CDF"):
    trade_id = tj.open_trade(
        db_path, symbol="2330", side=side, initial_stop_loss=0, opened_at=entry_date,
        instrument="SSF", contract_size=2000, futures_code=code, contract_month="202611",
    )
    open_action, close_action = ("BUY", "SELL") if side == "LONG" else ("SELL", "BUY")
    tj.record_execution(db_path, trade_id, action=open_action, price=entry, qty=1,
                        executed_at=entry_date, tax=0)
    tj.record_execution(db_path, trade_id, action=close_action, price=exit_price, qty=1,
                        executed_at=exit_date, tax=0)
    return trade_id


class FuturesBacktestPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"
        self.original_db_path = app_paths.HISTORY_DB_PATH
        app_paths.HISTORY_DB_PATH = self.db_path
        _closed(self.db_path, "LONG", 100, 110, "2026-11-02", "2026-11-05")
        _closed(self.db_path, "SHORT", 100, 103, "2026-11-03", "2026-11-06")
        _closed(self.db_path, "LONG", 100, 95, "2026-11-04", "2026-11-09")
        self.page = FuturesBacktestPage()
        self.page.resize(1200, 900)

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        app_paths.HISTORY_DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _wait(self, attr):
        deadline = time.time() + 10
        while getattr(self.page, attr) is not None and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.02)
        self.app.processEvents()
        self.assertIsNone(getattr(self.page, attr), f"{attr} did not finish")

    def test_actual_performance_renders(self):
        self.page.show()
        self._wait("_load_task")
        self.assertEqual(self.page.trade_table.rowCount(), 3)
        self.assertEqual(self.page.kpi_cards["count"].value_label.text(), "3")
        self.assertEqual(self.page.kpi_cards["total"].value_label.text(), "+4,000 元")
        self.assertEqual(self.page.kpi_cards["win_rate"].value_label.text(), "33.3%")
        # 最新出場排最上面
        self.assertEqual(self.page.trade_table.item(0, 0).text(), "2026-11-09")

        self.page.side_combo.setCurrentIndex(2)  # 空單
        self._wait("_load_task")
        self.assertEqual(self.page.trade_table.rowCount(), 1)

    def test_whatif_runs_with_injected_bars(self):
        self.page.show()
        self._wait("_load_task")
        self.page.stop_mode_combo.setCurrentIndex(1)  # 固定 %
        self.page.stop_pct_spin.setValue(2)

        def fetch(trade):
            return [{"date": "2026-11-10", "open": 100, "high": 104, "low": 97, "close": 100,
                     "settlement": None}]

        self.page.run_whatif(fetch_bars=fetch)
        self._wait("_whatif_task")
        report = self.page._whatif_report
        self.assertEqual(report.actual.count, 3)
        self.assertEqual(self.page.whatif_table.rowCount(), 3)
        self.assertEqual(self.page.compare_table.item(0, 0).text(), "總淨損益")
        self.assertTrue(self.page.run_button.isEnabled())
        self.assertIn("停損 2%", self.page.whatif_status.text())


class FuturesImportDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp_dir.name)
        self.db_path = self.dir / "history.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_preview_and_import(self):
        path = self.dir / "t.csv"
        path.write_text(
            "日期,標的代號,合約,合約月份,買賣,口數,價格,手續費\n"
            "2026-10-01,2330,小型,202611,買,2,1000,10\n"
            "2026-10-03,2330,小型,202611,賣,2,1010,10\n",
            encoding="utf-8",
        )
        imported = []
        dialog = FuturesImportDialog(None, self.db_path, on_imported=imported.append)
        dialog.load_file(path)
        self.assertEqual(dialog.preview.rowCount(), 2)
        self.assertTrue(dialog.import_button.isEnabled())
        dialog._import()
        self.assertEqual(imported[0].added, 2)
        self.assertFalse(dialog.import_button.isEnabled())
        trade = tj.list_trades(self.db_path, instrument="SSF")[0]
        self.assertEqual(trade.status, "CLOSED")

    def test_errors_block_import(self):
        path = self.dir / "bad.csv"
        path.write_text(
            "日期,標的代號,合約,合約月份,買賣,口數,價格\n2026-10-01,2330,小型,2026,買,2,1000\n",
            encoding="utf-8",
        )
        dialog = FuturesImportDialog(None, self.db_path)
        dialog.load_file(path)
        self.assertFalse(dialog.import_button.isEnabled())
        self.assertIn("第 2 行", dialog.status_label.text())


if __name__ == "__main__":
    unittest.main()
