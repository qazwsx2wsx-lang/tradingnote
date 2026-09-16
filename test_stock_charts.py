import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from PySide6 import QtCore, QtGui, QtWidgets
from ui.theme import STYLESHEET
from ui.stock_charts import StockChart, ComparisonWidget, chart_page
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


if __name__ == "__main__":
    unittest.main()
