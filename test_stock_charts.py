import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from PySide6 import QtCore, QtGui, QtWidgets
from ui.theme import STYLESHEET
from ui.stock_charts import StockChart, ComparisonWidget, chart_page, LazyTabBuilder
import tradingnote_gui as gui


class StockChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.setFont(QtGui.QFont("Microsoft JhengHei", 10))
        cls.app.setStyleSheet(STYLESHEET)

    def test_date_alignment_missing_values_and_axis_switch(self):
        price, margin = StockChart(), StockChart()
        gui._populate_price_chart(price, [dict(date="2026-09-10", close=100), dict(date="2026-09-14", close=110)])
        gui._populate_margin_chart(margin, {"dates": ["2026-09-11", "2026-09-14"], "series": {"融資餘額": [20, 25], "融券餘額": [10, 12]}})
        compare = ComparisonWidget()
        compare.resize(900, 500)
        compare.show()
        compare.set_sources([("股價", price), ("融資融券", margin)])
        self.app.processEvents()
        self.assertEqual(compare.chart.dates, ["2026-09-10", "2026-09-11", "2026-09-14"])
        compare.chart.select_index(1)
        self.assertIn("收盤價：無資料", compare.chart.readout.text())
        self.assertIn("融資餘額：20.00", compare.chart.readout.text())
        self.assertTrue(compare.chart.getAxis("right").isVisible())
        self.assertEqual(len(compare.right.addedItems), 1)
        compare.grab().save("_qa_gui/stock_comparison.png")
        compare.selectors[0].setCurrentIndex(1)
        compare.selectors[1].setCurrentIndex(2)
        self.assertFalse(compare.chart.getAxis("right").isVisible())
        self.assertEqual(len(compare.right.addedItems), 0)
        self.assertEqual(len(compare.chart.listDataItems()), 2)
        price.cleared.connect(compare.clear)
        price.clear()
        self.assertFalse(compare.chart.dates)
        self.assertEqual(compare.selectors[0].count(), 0)
        compare.close()
        price.close()
        margin.close()

    def test_click_readout_uses_cumulative_values_and_clears(self):
        chart = StockChart()
        page = chart_page(chart)
        gui._populate_flow_chart(chart, {"dates": ["2026-09-10", "2026-09-11"], "series": {"外資": [10, -3], "投信": [2, 4]}})
        page.resize(800, 400)
        page.show()
        self.app.processEvents()
        scene_pos = chart.getViewBox().mapViewToScene(QtCore.QPointF(1, 7))
        class Click:
            def button(self): return QtCore.Qt.LeftButton
            def scenePos(self): return scene_pos
        chart._clicked(Click())
        self.assertIn("2026-09-11", chart.readout.text())
        self.assertIn("外資：7.00", chart.readout.text())
        self.assertIn("投信：6.00", chart.readout.text())
        chart.clear()
        self.assertFalse(chart.dates)
        self.assertIsNone(chart.marker)
        self.assertNotIn("2026", chart.readout.text())
        page.close()

    def test_chart_series_helpers_match_populated_chart_series(self):
        """`_xxx_chart_series(data)`（雙資料比較延遲分頁用，見圖表架構統整
        第二階段）必須跟「先 populate 真正的 StockChart 再呼叫 .series()」
        產生完全一致的結果，否則延遲分頁時「雙資料比較」會跟其他分頁的資料
        對不起來。"""
        cases = [
            (gui._populate_price_chart, gui._price_chart_series,
             [dict(date="2026-09-10", close=100.0), dict(date="2026-09-11", close=102.0)]),
            (gui._populate_flow_chart, gui._flow_chart_series,
             {"dates": ["2026-09-10", "2026-09-11"], "series": {"外資": [10, -3], "投信": [2, 4]}}),
            (gui._populate_institutional_detail_chart, gui._institutional_detail_chart_series,
             {"dates": ["2026-09-10", "2026-09-11"], "series": {"外資": [10, -3], "投信": [2, 4]}}),
            (gui._populate_margin_chart, gui._margin_chart_series,
             {"dates": ["2026-09-10", "2026-09-11"], "series": {"融資餘額": [20, 25], "融券餘額": [10, 12]}}),
            (gui._populate_vpt_chart, gui._vpt_chart_series,
             {"dates": ["2026-09-10", "2026-09-11"], "vpt": [5.0, 9.0], "mfi": [40.0, 55.0]}),
            (gui._populate_mfi_chart, gui._mfi_chart_series,
             {"dates": ["2026-09-10", "2026-09-11"], "vpt": [5.0, 9.0], "mfi": [40.0, 55.0]}),
            (gui._populate_short_sale_balance_chart, gui._short_sale_balance_chart_series,
             {"dates": ["2026-09-10", "2026-09-11"], "series": {"借券賣出餘額": [300000, 301000]}}),
            (gui._populate_lending_volume_chart, gui._lending_volume_chart_series,
             {"dates": ["2026-09-10", "2026-09-11"], "series": {"借券成交量": [50, 62]}}),
        ]
        for populate_fn, series_fn, data in cases:
            with self.subTest(populate_fn.__name__):
                chart = StockChart()
                populate_fn(chart, data)
                self.assertEqual(sorted(chart.series()), sorted(series_fn(data)))
                chart.close()


class LazyTabBuilderTests(unittest.TestCase):
    """圖表架構統整第三階段（延遲建立）：LazyTabBuilder 本身的通用行為，不依賴
    任何實際的 StockChart／DetailChartPanel。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_builds_only_active_tab_once_and_rebuilds_only_after_reset(self):
        tabs = QtWidgets.QTabWidget()
        calls = {0: 0, 1: 0, 2: 0}

        def make_build(index):
            def build(container):
                calls[index] += 1
            return build

        lazy = LazyTabBuilder(tabs)
        for i in range(3):
            lazy.add_tab(f"tab{i}", make_build(i))

        # 分頁 0 是預設作用中的分頁，建構時應該已經被建立；其餘兩個還沒有。
        self.assertEqual(calls, {0: 1, 1: 0, 2: 0})

        tabs.setCurrentIndex(1)
        self.assertEqual(calls, {0: 1, 1: 1, 2: 0})
        tabs.setCurrentIndex(2)
        self.assertEqual(calls, {0: 1, 1: 1, 2: 1})

        # 切回已經造訪過的分頁不應該重建。
        tabs.setCurrentIndex(0)
        tabs.setCurrentIndex(1)
        self.assertEqual(calls, {0: 1, 1: 1, 2: 1})

        # reset() 本身不會立刻重建任何分頁；activate_current() 只重建目前分頁；
        # 沒被重新切到的分頁維持原本次數，直到真的被切到才會再 +1。
        lazy.reset()
        self.assertEqual(calls, {0: 1, 1: 1, 2: 1})
        lazy.activate_current()
        self.assertEqual(calls, {0: 1, 1: 2, 2: 1})
        tabs.setCurrentIndex(2)
        self.assertEqual(calls, {0: 1, 1: 2, 2: 2})
        tabs.close()


class DetailChartPanelTests(unittest.TestCase):
    """圖表架構統整第三階段：StockDetailDialog／部位紀錄頁共用的
    DetailChartPanel（8 張明細圖＋技術分析＋雙資料比較，10 分頁延遲建立）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        cls.app.setFont(QtGui.QFont("Microsoft JhengHei", 10))
        cls.app.setStyleSheet(STYLESHEET)

    @staticmethod
    def _sample_data(dates):
        n = len(dates)

        def series(base, step):
            return [base + step * i for i in range(n)]

        return {
            "price_history": [dict(date=d, close=100.0 + i) for i, d in enumerate(dates)],
            "institutional_history": {"dates": dates, "series": {"外資": series(10, -3), "投信": series(2, 4)}},
            "institutional_detail_history": {"dates": dates, "series": {"外資": series(10, -3)}},
            "margin_history": {"dates": dates, "series": {"融資餘額": series(20, 5), "融券餘額": series(10, 2)},
                                "latest": {"date": dates[-1], "margin_balance": 25, "margin_change": 5, "short_balance": 12, "short_change": 2}},
            "vpt_mfi_history": {"dates": dates, "vpt": series(5.0, 4.0), "mfi": series(40.0, 15.0)},
            "sbl_short_balance": {"dates": dates, "series": {"借券賣出餘額": series(300000, 1000)},
                                   "date": dates[-1], "balance": 301000, "change": 1000},
            "lending": {"dates": dates, "series": {"借券成交量": series(50, 12)},
                        "date": dates[-1], "volume": 62, "avg_fee_rate": 1.2},
        }

    def test_only_active_tab_builds_widget_at_construction(self):
        panel = gui.DetailChartPanel()
        self.assertIsNotNone(panel._chart_states[0]["chart"])  # 歷史股價＝預設分頁
        for state in panel._chart_states[1:]:
            self.assertIsNone(state["chart"])
        self.assertIsNone(panel._technical_state["widget"])
        self.assertIsNone(panel._comparison_state["widget"])
        panel.tabs.close()

    def test_comparison_tab_works_without_visiting_other_tabs(self):
        panel = gui.DetailChartPanel()
        data = self._sample_data(["2026-09-10", "2026-09-11"])
        panel.set_data(data)
        comparison_index = panel.tabs.count() - 1
        panel.tabs.setCurrentIndex(comparison_index)
        self.app.processEvents()

        # 「雙資料比較」以外的 8 個明細分頁（index 1~7；index 0 是預設分頁，
        # 一開始就會建立）都還沒被造訪過，widget 應該仍是 None。
        for state in panel._chart_states[1:]:
            self.assertIsNone(state["chart"])
        comparison = panel._comparison_state["widget"]
        self.assertIsNotNone(comparison)
        self.assertIn("股價／收盤價", comparison.catalog)
        self.assertIn("三大法人累計／外資", comparison.catalog)
        self.assertIn("借券成交／借券成交量", comparison.catalog)
        panel.tabs.close()

    def test_reset_repopulates_existing_widget_instead_of_rebuilding(self):
        panel = gui.DetailChartPanel()
        panel.set_data(self._sample_data(["2026-09-10", "2026-09-11"]))
        flow_index = 1  # 「三大法人」
        panel.tabs.setCurrentIndex(flow_index)
        self.app.processEvents()
        chart = panel._chart_states[flow_index]["chart"]
        self.assertIsNotNone(chart)
        self.assertEqual(chart.dates, ["2026-09-10", "2026-09-11"])

        panel.tabs.setCurrentIndex(0)
        panel.set_data(self._sample_data(["2026-09-12", "2026-09-13", "2026-09-14"]))
        # 目前作用中的分頁（index 0）立刻用新資料重新 populate，未造訪的
        # flow_index 分頁維持舊內容，直到真的被切到才更新——不是立刻全部重建。
        self.assertEqual(chart.dates, ["2026-09-10", "2026-09-11"])

        panel.tabs.setCurrentIndex(flow_index)
        self.app.processEvents()
        self.assertIs(panel._chart_states[flow_index]["chart"], chart)  # 同一個 widget，沒有重建
        self.assertEqual(chart.dates, ["2026-09-12", "2026-09-13", "2026-09-14"])
        panel.tabs.close()

    def test_clear_shows_empty_state_on_active_tab(self):
        panel = gui.DetailChartPanel()
        panel.set_data(self._sample_data(["2026-09-10", "2026-09-11"]))
        panel.clear()
        self.assertFalse(panel._chart_states[0]["chart"].dates)
        panel.tabs.close()


if __name__ == "__main__":
    unittest.main()
