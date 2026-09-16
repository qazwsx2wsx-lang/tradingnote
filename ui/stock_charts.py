"""Date-aware stock charts and comparison of already loaded series."""
import math

import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from ui.theme import COLOR_ACCENT, COLOR_SPECIAL, COLOR_SURFACE, COLOR_TEXT, COLOR_MUTED


def apply_chart_theme(chart, *, min_height=240):
    """套用 `StockChart`／`FlowChartWidget` 等 `pg.PlotWidget` 共用的深色主題
    設定（背景／格線／圖例／最小高度）。取代原本在 `tradingnote_gui.py` 多處
    逐字重複的 setBackground／showGrid／addLegend／setMinimumHeight 四行。"""
    chart.setBackground(COLOR_SURFACE)
    chart.showGrid(x=True, y=True, alpha=.08)
    chart.addLegend()
    chart.setMinimumHeight(min_height)


def set_date_ticks(chart, dates, axis="bottom", max_labels=8):
    """把 `dates`（依時間排序的日期字串清單）等距抽樣成軸刻度標籤，取代原本
    在每個 `_populate_*_chart` 與 `ComparisonWidget.refresh` 裡逐字重複的
    「算 step、組 tick 清單」邏輯。"""
    if not dates:
        chart.getAxis(axis).setTicks([])
        return
    step = max(1, len(dates) // max_labels)
    chart.getAxis(axis).setTicks([[(i, dates[i]) for i in range(0, len(dates), step)]])


class StockChart(pg.PlotWidget):
    cleared = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dates = []
        self.marker = None
        self.readout = None
        self.inspect_series = None
        # PlotWidget installs forwarding methods on the instance during init.
        self.clear = self._clear_chart
        self.scene().sigMouseClicked.connect(self._clicked)

    def _clear_chart(self):
        self.plotItem.clear()
        self.dates = []
        self.marker = None
        self.inspect_series = None
        self.getAxis("bottom").setTicks([])
        if self.readout is not None:
            self.readout.setText("點擊圖表查看日期與數值")
        self.cleared.emit()

    def series(self):
        unit = self.getAxis("left").labelText
        result = []
        for curve in self.listDataItems():
            if not curve.name():
                continue
            xs, ys = curve.getData()
            if xs is None:
                continue
            values = {self.dates[int(x)]: float(y) for x, y in zip(xs, ys)
                      if 0 <= int(x) < len(self.dates)}
            result.append((curve.name(), unit, values))
        return result

    def select_index(self, index):
        if not self.dates:
            return
        index = max(0, min(len(self.dates) - 1, index))
        day = self.dates[index]
        parts = [day]
        for name, unit, values in self.inspect_series or self.series():
            value = values.get(day)
            text = f"{value:,.2f}" if value is not None and math.isfinite(value) else "無資料"
            parts.append(f"{name}：{text}（{unit}）")
        if self.marker is None:
            self.marker = pg.InfiniteLine(angle=90, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine))
            self.addItem(self.marker, ignoreBounds=True)
        self.marker.setPos(index)
        if self.readout is not None:
            self.readout.setText("　｜　".join(parts))

    def _clicked(self, event):
        vb = self.getViewBox()
        if event.button() == QtCore.Qt.LeftButton and vb.sceneBoundingRect().contains(event.scenePos()):
            self.select_index(round(vb.mapSceneToView(event.scenePos()).x()))


def chart_page(chart):
    page = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(page)
    label = QtWidgets.QLabel("點擊圖表查看日期與數值")
    label.setWordWrap(True)
    label.setTextFormat(QtCore.Qt.PlainText)
    chart.readout = label
    layout.addWidget(label)
    layout.addWidget(chart, 1)
    return page


class LazyTabBuilder(QtCore.QObject):
    """包裝一個 `QTabWidget`：分頁標籤／順序一次建好（使用者一開始就看到完整
    分頁列表），但每個分頁背後真正的內容延遲到第一次切到該分頁才建立——建立
    好的分頁不會因為切走再切回來就重建。換股票／換部位等資料來源改變時呼叫
    `reset()`：不會立刻重建任何分頁，只把全部分頁標成「下次被切到時要重新
    `build()`」，讓沒被造訪過的分頁維持延遲、已經造訪過的分頁下次切回去才
    看到新資料；呼叫端通常接著呼叫 `activate_current()`，確保目前作用中的
    分頁不會停在舊資料。

    `build(container)` 是否要重建 widget 或只重新 populate 既有 widget，由
    呼叫端自己決定（`container` 是這個分頁固定不變的容器，`build` 每次被呼叫
    都拿到同一個），`LazyTabBuilder` 本身不管內容、只管「什麼時候該呼叫」。"""

    def __init__(self, tabs, parent=None):
        super().__init__(parent or tabs)
        self._tabs = tabs
        self._entries = []
        tabs.currentChanged.connect(self._on_current_changed)

    def add_tab(self, label, build):
        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        index = self._tabs.addTab(container, label)
        self._entries.append({"container": container, "build": build, "dirty": True})
        if index == self._tabs.currentIndex():
            self._maybe_build(index)
        return index

    def _on_current_changed(self, index):
        self._maybe_build(index)

    def _maybe_build(self, index):
        if not (0 <= index < len(self._entries)):
            return
        entry = self._entries[index]
        if entry["dirty"]:
            entry["dirty"] = False
            entry["build"](entry["container"])

    def activate_current(self):
        self._maybe_build(self._tabs.currentIndex())

    def reset(self):
        for entry in self._entries:
            entry["dirty"] = True


class ComparisonWidget(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        layout = QtWidgets.QVBoxLayout(self)
        controls = QtWidgets.QHBoxLayout()
        self.selectors = [QtWidgets.QComboBox(), QtWidgets.QComboBox()]
        for title, combo in zip(("資料 A", "資料 B"), self.selectors):
            controls.addWidget(QtWidgets.QLabel(title))
            controls.addWidget(combo, 1)
            combo.currentIndexChanged.connect(self.refresh)
        layout.addLayout(controls)
        self.chart = StockChart()
        apply_chart_theme(self.chart)
        layout.addWidget(chart_page(self.chart))
        self.right = pg.ViewBox()
        self.chart.scene().addItem(self.right)
        self.chart.getAxis("right").linkToView(self.right)
        self.right.setXLink(self.chart.getViewBox())
        self.chart.getViewBox().sigResized.connect(self._resize)
        self.catalog = {}

    def _resize(self):
        self.right.setGeometry(self.chart.getViewBox().sceneBoundingRect())
        self.right.linkedViewChanged(self.chart.getViewBox(), self.right.XAxis)

    def clear(self):
        self.set_sources([])

    def set_sources(self, sources):
        """sources 是 (group, source) 的清單：source 可以是已經畫好資料的
        StockChart（用 .series() 讀出目前畫的曲線，例如股價／三大法人這幾張
        既有圖表），也可以直接是一份 [(name, unit, values), ...] 序列清單
        （不需要真的畫在畫面上，例如「技術分析」24 類指標——同一時間畫面上
        只會顯示其中一種指標，但比較功能要能選到全部 24 種，所以直接從計算
        結果取序列，不透過畫出來的曲線）。"""
        previous = [combo.currentText() for combo in self.selectors]
        self.catalog = {}
        for group, source in sources:
            series_list = source.series() if hasattr(source, "series") else source
            for name, unit, values in series_list:
                self.catalog[f"{group}／{name}"] = (name, unit, values)
        for i, combo in enumerate(self.selectors):
            with QtCore.QSignalBlocker(combo):
                combo.clear()
                combo.addItems(list(self.catalog))
                index = combo.findText(previous[i])
                combo.setCurrentIndex(index if index >= 0 else min(i, combo.count() - 1))
                combo.setEnabled(bool(self.catalog))
        self.refresh()

    def refresh(self):
        self.chart.clear()
        self.right.clear()
        self.chart.hideAxis("right")
        selected = [self.catalog.get(combo.currentText()) for combo in self.selectors]
        if not all(selected):
            self.chart.setTitle("尚無可比較資料", color=COLOR_TEXT)
            return
        if self.selectors[0].currentText() == self.selectors[1].currentText():
            self.chart.setTitle("請選擇兩項不同資料", color=COLOR_TEXT)
            return
        dates = sorted(set(selected[0][2]) | set(selected[1][2]))
        self.chart.dates = dates
        self.chart.inspect_series = selected
        # Axis captions can include the metric name; compare known compatible units.
        def unit_key(label):
            return label.split("（")[-1].rstrip("）") if "（" in label else label
        dual = unit_key(selected[0][1]) != unit_key(selected[1][1])
        for i, (name, unit, values) in enumerate(selected):
            color = (COLOR_ACCENT, COLOR_SPECIAL)[i]
            curve = pg.PlotDataItem(list(range(len(dates))),
                                   [values.get(day, float("nan")) for day in dates],
                                   pen=pg.mkPen(color, width=2), name=name, connect="finite",
                                   symbol="o", symbolSize=4, symbolBrush=color, symbolPen=None)
            if i == 1 and dual:
                self.chart.showAxis("right")
                self.chart.setLabel("right", unit, color=color)
                self.right.addItem(curve)
                self.chart.plotItem.legend.addItem(curve, name)
            else:
                self.chart.addItem(curve)
            if i == 0:
                self.chart.setLabel("left", unit if dual else " / ".join(dict.fromkeys(s[1] for s in selected)), color=color if dual else COLOR_TEXT)
        set_date_ticks(self.chart, dates, max_labels=6)
        self.chart.setTitle("雙資料比較｜點擊查看同日數值", color=COLOR_TEXT)
        self._resize()
        self.chart.enableAutoRange()
        self.right.enableAutoRange(axis=pg.ViewBox.YAxis)
        self.chart.setXRange(0, max(1, len(dates) - 1), padding=.02)
