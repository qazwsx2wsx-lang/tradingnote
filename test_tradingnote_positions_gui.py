import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

import tradingnote_gui as gui
from ui import app_paths
from ui.dialogs.positions import ExecutionFormDialog, PositionFormDialog, PositionPlanFormDialog
from tradingnote_tradejournal import (
    create_tag,
    get_trade,
    open_trade,
    record_execution,
)


class PositionsHarness(gui.TradingNoteWindow):
    def __init__(self):
        QtWidgets.QMainWindow.__init__(self)
        root = QtWidgets.QWidget()
        self._central_layout = QtWidgets.QVBoxLayout(root)
        self.positions_tab = QtWidgets.QWidget()
        self._central_layout.addWidget(self.positions_tab)
        self.setCentralWidget(root)
        self.snapshot = {}
        self.settings = {}
        self._ticker_concept_map = {}
        self.positions = []
        self.last_error = None
        self.staleness_warning = ""
        self.continuity_note = ""
        self.large_traders_note = ""
        self.institutional_note = ""
        self._total_pnl = None
        self.status_bar_label = QtWidgets.QLabel()
        self._build_positions_tab()


class PositionsGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"
        self.original_db_path = app_paths.HISTORY_DB_PATH
        app_paths.HISTORY_DB_PATH = self.db_path

        self.window = PositionsHarness()
        self.window.resize(1200, 700)
        self.window.show()
        self.window.refresh_table()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        app_paths.HISTORY_DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _select_row(self, row):
        self.window.table.selectRow(row)
        self.app.processEvents()

    def test_window_has_six_pages_and_no_standalone_trade_tab(self):
        self.assertFalse(hasattr(self.window, "trade_journal_tab"))

    def test_table_populates_and_plan_subtab_renders(self):
        trade_id = open_trade(
            self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0,
            opened_at="2026-10-01T09:00:00",
        )
        record_execution(self.db_path, trade_id, action="BUY", price=10.0, qty=1000)
        create_tag(self.db_path, "FOMO", category="EMOTION")
        self.window.refresh_table()

        self.assertEqual(self.window.table.rowCount(), 1)
        self._select_row(0)

        self.assertEqual(self.window.position_execution_table.rowCount(), 1)
        self.assertIn(
            "FOMO", [btn.text() for btn in self.window.position_tag_buttons.values()]
        )

    def test_tag_toggle_attaches_and_detaches(self):
        trade_id = open_trade(self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0)
        tag_id = create_tag(self.db_path, "FOMO", category="EMOTION")
        self.window.refresh_table()
        self._select_row(0)

        button = self.window.position_tag_buttons[tag_id]
        self.assertFalse(button.isChecked())
        button.click()
        self.app.processEvents()
        self.assertEqual([t.tag_id for t in get_trade(self.db_path, trade_id).tags], [tag_id])

        button.click()
        self.app.processEvents()
        self.assertEqual(get_trade(self.db_path, trade_id).tags, ())

    def test_notes_dirty_flag_and_autosave_on_row_switch(self):
        open_trade(self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0)
        open_trade(self.db_path, symbol="2317", side="LONG", initial_stop_loss=100.0)
        self.window.refresh_table()
        self._select_row(0)
        self.assertFalse(self.window.position_plan_dirty)

        first_trade_id = int(self.window._selected_position_id())
        self.window.position_notes_edit.setPlainText("測試筆記")
        self.assertTrue(self.window.position_plan_dirty)

        self._select_row(1)
        self.assertNotEqual(int(self.window._selected_position_id()), first_trade_id)
        self.assertEqual(get_trade(self.db_path, first_trade_id).notes, "測試筆記")
        self.assertFalse(self.window.position_plan_dirty)

    def test_analytics_round_trip_through_save_button(self):
        trade_id = open_trade(self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0)
        self.window.refresh_table()
        self._select_row(0)

        self.window.position_analytics_inputs["mae_r"].setText("-0.5")
        self.window.position_analytics_inputs["mfe_r"].setText("3.2")
        self.window._save_position_detail_clicked()

        trade = get_trade(self.db_path, trade_id)
        self.assertEqual(trade.analytics.mae_r, -0.5)
        self.assertEqual(trade.analytics.mfe_r, 3.2)

    def test_position_form_dialog_submit_opens_position(self):
        received = {}

        def on_submit(**kwargs):
            received.update(kwargs)

        dialog = PositionFormDialog(self.window, on_submit, [], self.db_path)
        dialog.ticker_edit.setText("2330")
        dialog.shares_edit.setText("1000")
        dialog.entry_price_edit.setText("10.0")
        dialog.stop_loss_edit.setText("9.0")
        dialog._submit()

        self.assertEqual(received["ticker"], "2330")
        self.assertEqual(received["shares"], 1000)
        self.assertEqual(received["entry_price"], 10.0)
        self.assertEqual(received["initial_stop_loss"], 9.0)
        self.assertEqual(received["side"], "LONG")

    def test_position_form_dialog_rejects_missing_stop_loss(self):
        dialog = PositionFormDialog(self.window, lambda **_k: None, [], self.db_path)
        dialog.ticker_edit.setText("2330")
        dialog.shares_edit.setText("1000")
        dialog.entry_price_edit.setText("10.0")
        dialog.stop_loss_edit.setText("not-a-number")
        with patch.object(QtWidgets.QMessageBox, "critical", return_value=None):
            dialog._submit()
        self.assertFalse(dialog.result())

    def test_position_plan_form_dialog_does_not_touch_notes(self):
        trade_id = open_trade(
            self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0, notes="原始筆記",
        )
        received = {}

        def on_submit(**kwargs):
            received.update(kwargs)

        trade = get_trade(self.db_path, trade_id)
        dialog = PositionPlanFormDialog(self.window, on_submit, [], self.db_path, trade)
        dialog.stop_loss_edit.setText("8.5")
        dialog._submit()

        self.assertEqual(received["initial_stop_loss"], 8.5)
        self.assertNotIn("notes", received)

    def test_execution_form_dialog_submit_records_execution(self):
        received = {}

        def on_submit(**kwargs):
            received.update(kwargs)

        dialog = ExecutionFormDialog(self.window, on_submit)
        dialog.price_edit.setText("10.0")
        dialog.qty_edit.setText("100")
        dialog._submit()

        self.assertEqual(received["action"], "BUY")
        self.assertEqual(received["price"], 10.0)
        self.assertEqual(received["qty"], 100.0)

    def test_history_checkbox_toggles_status_filter(self):
        open_id = open_trade(self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0)
        cancelled_id = open_trade(self.db_path, symbol="2317", side="LONG", initial_stop_loss=100.0)
        from tradingnote_tradejournal import cancel_trade
        cancel_trade(self.db_path, cancelled_id)
        self.window.refresh_table()

        self.assertEqual(self.window.table.rowCount(), 1)  # OPEN only by default

        self.window.position_history_checkbox.setChecked(True)
        self.app.processEvents()
        self.assertEqual(self.window.table.rowCount(), 2)

    def test_mutation_buttons_blocked_on_non_open_selection(self):
        trade_id = open_trade(self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0)
        from tradingnote_tradejournal import cancel_trade
        cancel_trade(self.db_path, trade_id)
        self.window.position_history_checkbox.setChecked(True)
        self.window.refresh_table()
        self._select_row(0)

        with patch.object(QtWidgets.QMessageBox, "information", return_value=None) as mock_info:
            self.window.open_add_execution_dialog()
            mock_info.assert_called_once()

    def test_close_and_cancel_selected_position(self):
        trade_id = open_trade(self.db_path, symbol="2330", side="LONG", initial_stop_loss=9.0)
        self.window.refresh_table()
        self._select_row(0)

        with patch.object(
            QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.Yes
        ):
            self.window.close_selected_position()
        self.assertEqual(get_trade(self.db_path, trade_id).status, "CLOSED")

        trade_id2 = open_trade(self.db_path, symbol="2317", side="LONG", initial_stop_loss=100.0)
        self.window.position_history_checkbox.setChecked(True)
        self.window.refresh_table()
        for row in range(self.window.table.rowCount()):
            if self.window.table.item(row, self.window._column_keys.index("id")).text() == str(trade_id2):
                self._select_row(row)
                break
        with patch.object(
            QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.Yes
        ):
            self.window.cancel_selected_position()
        self.assertEqual(get_trade(self.db_path, trade_id2).status, "CANCELLED")


if __name__ == "__main__":
    unittest.main()
