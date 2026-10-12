"""製圖統一化的防呆測試：圖表程式碼不得自己寫 hex 色碼或直接建 pg.PlotWidget，
且所有圖的背景／系列色／正負色都來自 ui.theme（見 ui/charts/base.py）。"""

import os
import pathlib
import re
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from ui.charts.base import line, new_plot, signed_bars
from ui.stock_charts import StockChart, apply_chart_theme
from ui.theme import CHART_BG, COLOR_GAIN, COLOR_LOSS, ENTITY_COLORS

ROOT = pathlib.Path(__file__).resolve().parent
ALLOWED = {ROOT / "ui" / "theme.py", ROOT / "ui" / "charts" / "base.py"}
HEX = re.compile(r"[\"']#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?[\"']")


def _source_files():
    yield ROOT / "tradingnote_gui.py"
    yield from (ROOT / "ui").rglob("*.py")


class ChartSourceRulesTest(unittest.TestCase):
    def test_no_hex_colors_outside_theme(self):
        offenders = [
            f"{path.relative_to(ROOT)}:{lineno}"
            for path in _source_files() if path not in ALLOWED
            for lineno, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if HEX.search(text)
        ]
        self.assertEqual(offenders, [], "顏色請放進 ui/theme.py")

    def test_no_raw_plot_widget(self):
        offenders = [
            str(path.relative_to(ROOT))
            for path in _source_files() if path not in ALLOWED
            if "pg.PlotWidget(" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [], "請改用 ui.charts.base.new_plot()")


class ChartStyleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_backgrounds_are_unified(self):
        stock = StockChart()
        apply_chart_theme(stock)
        for chart in (new_plot(), new_plot(interaction="static"), stock):
            self.assertEqual(chart.backgroundBrush().color().name().upper(), CHART_BG.upper())

    def test_entity_color_is_shared(self):
        chart = new_plot()
        curve = line(chart, [0, 1], [1, 2], "外資")
        self.assertEqual(curve.opts["pen"].color().name().upper(), ENTITY_COLORS["外資"].upper())

    def test_signed_bars_follow_gain_loss(self):
        chart = new_plot()
        bars = signed_bars(chart, [0, 1], [5, -3])
        colors = [b.color().name().upper() for b in bars.opts["brushes"]]
        self.assertEqual(colors, [COLOR_GAIN.upper(), COLOR_LOSS.upper()])

    def test_static_charts_ignore_mouse(self):
        chart = new_plot(interaction="static")
        self.assertEqual(chart.getPlotItem().getViewBox().state["mouseEnabled"], [False, False])


if __name__ == "__main__":
    unittest.main()
