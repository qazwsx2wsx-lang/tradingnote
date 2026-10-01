import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from contextlib import ExitStack
from dataclasses import replace
from unittest.mock import patch, Mock
from PySide6 import QtWidgets, QtCore
import tradingnote_gui as gui
from ui import app_paths
from ui.dialogs.stock_detail import IndustryTopStocksDialog
import pyqtgraph as pg
import unittest
import test_tradingnote_flow as fixtures
from tradingnote_flow import FlowPeriod
import tradingnote_history as history


class FlowGuiTests(unittest.TestCase):
    setUp = fixtures.FlowConsistencyTests.setUp
    period = fixtures.FlowConsistencyTests.period
    def test_full_window_six_pages_and_revision_refresh(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        with ExitStack() as stack:
            for name, value in vars(app_paths).copy().items():
                if name.endswith('_PATH') and hasattr(value, 'name'):
                    stack.enter_context(patch.object(app_paths, name, self.db.parent / value.name))
            app_paths.HISTORY_DB_PATH = self.db
            stack.enter_context(patch.object(gui, 'load_settings', return_value={'auto_check_continuity': False}))
            stack.enter_context(patch.object(gui, 'load_positions', return_value=[]))
            stack.enter_context(patch.object(gui, 'build_classification_catalog', return_value=Mock(groups=Mock(return_value={'測試族群': {'1111'}}), revision='test', value_chain_scopes=[])))
            stack.enter_context(patch.object(gui, 'get_industry_map', return_value={'1111':'測試族群'}))
            stack.enter_context(patch.object(gui, 'get_industry_directory', return_value={'1111':{'name':'測試', 'market':'TWSE','industry':'測試族群'}}))
            for name in ('get_cached_daily_futures_report', 'get_cached_large_traders_futures_report', 'get_cached_ssf_list'):
                stack.enter_context(patch.object(gui, name, return_value=[]))
            window = gui.TradingNoteWindow(self.snapshot, None)
            try:
                window.show()
                self.assertEqual(window.page_stack.count(), 7)
                for index in range(6):
                    window._set_main_page(index)
                    app.processEvents()
                    self.assertEqual(window.page_stack.currentIndex(), index)
                window.flow_period = self.period(3)
                window.refresh_flow_tab()
                self.assertEqual(window.flow_list.topLevelItem(0).text(5), '+21.00%')
                conn = history._connect(self.db)
                conn.execute("UPDATE daily_prices SET close=50 WHERE date='2026-09-11'")
                conn.commit()
                conn.close()
                window._refresh_flow_on_revision()
                self.assertEqual(window.flow_list.topLevelItem(0).text(5), '+142.00%')
                scatter = next(item for item in window.flow_chart.getPlotItem().items
                               if isinstance(item, pg.ScatterPlotItem))
                self.assertAlmostEqual(scatter.points()[0].pos().x(), 142)
                window.flow_period = self.period(1)
                updated_snapshot = {'1111': replace(self.snapshot['1111'], change=21)}
                window._apply_refresh_result(updated_snapshot, None)
                self.assertEqual(window.flow_list.topLevelItem(0).text(5), '+21.00%')
                window.snapshot = self.snapshot
                for days in (1, 2, 3, 5):
                    window.flow_period = self.period(days)
                    window.refresh_flow_tab()
                    rows = window.flow_service.get_group_top_stocks(self.snapshot, window.flow_period, '測試族群')
                    dialog = IndustryTopStocksDialog(window, '測試族群', rows, days)
                    if rows[0]['change_pct'] is not None:
                        table = dialog.findChild(QtWidgets.QTableWidget)
                        self.assertEqual(table.item(0, 3).text(), window.flow_list.topLevelItem(0).text(5))
                    dialog.show()
                    app.processEvents()
                    dialog.close()
            finally:
                for timer in window.findChildren(QtCore.QTimer):
                    timer.stop()
                window.close()
                app.processEvents()
