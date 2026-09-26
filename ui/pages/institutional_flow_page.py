"""「法人資金流去哪？」頁面：三大法人在各類股的買賣超、加速流入與土洋動向。

資料全部來自 tradingnote_institutional_history 預先算好的表（sector_metrics／
stock_metrics／market_summary），這裡只負責查詢與畫圖，不做任何指標計算；
查詢都是單日／單一類股的小查詢，直接在主執行緒跑。只有「更新法人資料」
（打 API 回補）走背景執行緒。
"""

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

import tradingnote_institutional_history as ih
from tradingnote_tasks import run_background_task
from ui.components.insight_card import InsightCard
from ui.components.section_card import SectionCard
from ui.components.signal_badge import SignalBadge
from ui.components.stat_card import StatCard
from ui.stock_charts import set_date_ticks
from ui.theme import (
    COLOR_ACCENT,
    COLOR_CARD_BG,
    COLOR_GAIN,
    COLOR_LOSS,
    COLOR_MUTED,
    COLOR_TEXT,
    COLOR_WARNING,
)

INVESTOR_ORDER = ("all", "foreign", "trust", "dealer")
INVESTOR_TAB_LABELS = {"all": "合計", "foreign": "外資", "trust": "投信", "dealer": "自營"}
BUBBLE_NOTE = "加速流入＝近 5 天比前 5 天，平均每天多買多少（億／天）"
DETAIL_NOTE = "金額以「張數 × 當日收盤價」估算；近 5 日漲跌為成分股平均"
FOOTER_TEXT = (
    "<b>計算方式</b>　盤後結論金額直接採用證交所與櫃買中心公布的三大法人買賣差額（含 ETF）；"
    "個股金額（億）＝買賣超張數 × 1000 × 當日收盤價 ÷ 1 億，外資含外資自營商；"
    "類股＝成分股加總（上市櫃官方產業別，排除 ETF、權證，可用 sector_overrides.json 細分）；"
    "近 5／20 日為交易日累計；加速流入＝近 5 日平均每日買超 − 第 6～10 日平均；"
    "連續買賣：買超為正、賣超為負的連續天數；近 5 日漲跌為成分股等權平均。<br>"
    "<b>資料來源</b>　臺灣證券交易所、證券櫃檯買賣中心公開資訊。<br>"
    "<b>本網站僅彙整統計公開市場資訊，不構成任何投資建議。</b>"
)


def _fmt_yi(value, digits=2):
    if value is None:
        return "—"
    text = f"{value:.{digits}f}"
    if float(text) == 0:
        return f"{0:.{digits}f}"
    return f"+{text}" if value > 0 else text


def _fmt_pct(value):
    if value is None:
        return "—"
    return "0.00%" if abs(value) < 0.005 else f"{value:+.2f}%"


def _tone_color(value):
    if not value:
        return QtGui.QColor(COLOR_MUTED)
    return QtGui.QColor(COLOR_GAIN if value > 0 else COLOR_LOSS)


def _heat_color(value, max_abs):
    """依數值相對該欄最大絕對值上紅（正）綠（負）底色，深淺代表強度。"""
    if not value or not max_abs:
        return None
    color = QtGui.QColor(COLOR_GAIN if value > 0 else COLOR_LOSS)
    color.setAlphaF(0.10 + 0.50 * min(1.0, abs(value) / max_abs))
    return color


def _new_plot(min_height=260):
    chart = pg.PlotWidget()
    chart.setBackground(COLOR_CARD_BG)
    chart.setMinimumHeight(min_height)
    chart.getPlotItem().hideButtons()
    return chart


class _SortItem(QtWidgets.QTableWidgetItem):
    """依 UserRole 的數值排序（顯示文字含 + 號、「連買」等字樣，不能用字串排序）。"""

    def __lt__(self, other):
        mine = self.data(QtCore.Qt.UserRole)
        theirs = other.data(QtCore.Qt.UserRole)
        if isinstance(mine, str) or isinstance(theirs, str):
            return str(mine) < str(theirs)
        return (float("-inf") if mine is None else mine) < (
            float("-inf") if theirs is None else theirs)


def _item(text, sort_value=None, align_right=True, color=None, background=None):
    item = _SortItem(text)
    item.setData(QtCore.Qt.UserRole, sort_value)
    item.setFlags(QtCore.Qt.ItemIsEnabled | QtCore.Qt.ItemIsSelectable)
    if align_right:
        item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
    if color is not None:
        item.setForeground(color)
    if background is not None:
        item.setBackground(background)
    return item


class InvestorToggle(QtWidgets.QWidget):
    """合計／外資／投信／自營 四選一（沿用 flowSectionButton 樣式）。"""

    changed = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._group = QtWidgets.QButtonGroup(self)
        self._buttons = {}
        for investor in INVESTOR_ORDER:
            button = QtWidgets.QPushButton(INVESTOR_TAB_LABELS[investor])
            button.setCheckable(True)
            button.setProperty("flowSectionButton", True)
            button.clicked.connect(lambda _checked, inv=investor: self.changed.emit(inv))
            self._group.addButton(button)
            self._buttons[investor] = button
            layout.addWidget(button)
        self._buttons["all"].setChecked(True)

    def value(self):
        return next(inv for inv, b in self._buttons.items() if b.isChecked())

    def set_value(self, investor):
        self._buttons[investor].setChecked(True)


class SectorFlowChart(QtWidgets.QWidget):
    """類股近 N 日每日資金流（紅綠柱）＋可勾選的累計線（右軸）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.cumulative_check = QtWidgets.QCheckBox("疊加累計線")
        self.cumulative_check.setChecked(True)
        self.cumulative_check.toggled.connect(lambda _on: self._draw())
        layout.addWidget(self.cumulative_check)
        self.chart = _new_plot(240)
        layout.addWidget(self.chart, 1)

        plot = self.chart.getPlotItem()
        plot.showGrid(x=False, y=True, alpha=0.08)
        plot.setLabel("left", "億／日", color=COLOR_MUTED)
        self._cum_view = pg.ViewBox()
        self._cum_view.setZValue(10)  # 累計線畫在柱子上面，不被遮住
        plot.scene().addItem(self._cum_view)
        plot.getAxis("right").linkToView(self._cum_view)
        self._cum_view.setXLink(plot)
        plot.vb.sigResized.connect(self._sync_views)
        self._points = []

    def _sync_views(self):
        vb = self.chart.getPlotItem().vb
        self._cum_view.setGeometry(vb.sceneBoundingRect())
        self._cum_view.linkedViewChanged(vb, self._cum_view.XAxis)

    def set_points(self, points):
        self._points = list(points)
        self._draw()

    def _draw(self):
        plot = self.chart.getPlotItem()
        plot.clear()
        self._cum_view.clear()
        if not self._points:
            plot.hideAxis("right")
            empty = pg.TextItem("沒有資料", color=COLOR_MUTED, anchor=(0.5, 0.5))
            plot.addItem(empty)
            return
        xs = list(range(len(self._points)))
        values = [v for _d, v in self._points]
        plot.addItem(pg.BarGraphItem(
            x=xs, height=values, width=0.7,
            brushes=[pg.mkBrush(_tone_color(v)) for v in values], pen=pg.mkPen(None),
        ))
        plot.addLine(y=0, pen=pg.mkPen(COLOR_MUTED, width=1))
        set_date_ticks(plot, [d[5:] for d, _v in self._points])
        if self.cumulative_check.isChecked():
            running, cumulative = 0.0, []
            for v in values:
                running += v
                cumulative.append(running)
            plot.showAxis("right")
            plot.getAxis("right").setLabel("累計（億）", color=COLOR_MUTED)
            self._cum_view.addItem(pg.PlotCurveItem(xs, cumulative, pen=pg.mkPen(COLOR_ACCENT, width=2)))
            self._cum_view.enableAutoRange(axis=pg.ViewBox.YAxis)
        else:
            plot.hideAxis("right")
        plot.enableAutoRange()
        self._sync_views()


class SectorDetailDialog(QtWidgets.QDialog):
    """類股明細：近 30 日資金流＋成分股表格。"""

    STOCK_COLUMNS = ("代號 名稱", "收盤", "當日漲跌", "當日買超(億)", "外資連買", "20日累計(億)")

    def __init__(self, db_path, sector, date, investor="all", parent=None):
        super().__init__(parent)
        self.db_path, self.sector, self.date = db_path, sector, date
        self.setWindowTitle(f"{sector}｜類股明細")
        self.resize(820, 760)

        layout = QtWidgets.QVBoxLayout(self)
        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel(f"{sector}")
        title.setObjectName("pageTitle")
        header.addWidget(title)
        date_label = QtWidgets.QLabel(f"資料日期 {date}")
        date_label.setProperty("muted", True)
        header.addWidget(date_label)
        header.addStretch(1)
        close_button = QtWidgets.QPushButton("✕")
        close_button.setToolTip("關閉")
        close_button.setFixedWidth(40)
        close_button.clicked.connect(self.close)
        header.addWidget(close_button)
        layout.addLayout(header)

        note = QtWidgets.QLabel(DETAIL_NOTE)
        note.setProperty("muted", True)
        layout.addWidget(note)

        self.investor_toggle = InvestorToggle()
        self.investor_toggle.set_value(investor)
        self.investor_toggle.changed.connect(lambda _inv: self._load())
        toggle_row = QtWidgets.QHBoxLayout()
        toggle_row.addWidget(self.investor_toggle)
        toggle_row.addStretch(1)
        layout.addLayout(toggle_row)

        flow_card = SectionCard("近 30 日類股資金流（億／日）")
        self.flow_chart = SectorFlowChart()
        flow_card.body_layout.addWidget(self.flow_chart)
        layout.addWidget(flow_card, 2)

        self.table = QtWidgets.QTableWidget(0, len(self.STOCK_COLUMNS))
        self.table.setHorizontalHeaderLabels(self.STOCK_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        layout.addWidget(self.table, 3)
        self._load()

    def _load(self):
        investor = self.investor_toggle.value()
        self.flow_chart.set_points(
            ih.sector_history(self.db_path, self.sector, investor, self.date, days=30))
        stocks = ih.sector_stocks(self.db_path, self.sector, investor, self.date)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(stocks))
        for row, s in enumerate(stocks):
            close = s["close"]
            self.table.setItem(row, 0, _item(f"{s['ticker']}  {s['name']}", s["ticker"], False))
            self.table.setItem(row, 1, _item("—" if close is None else f"{close:,.2f}", close))
            self.table.setItem(row, 2, _item(_fmt_pct(s["change_pct"]), s["change_pct"],
                                             color=_tone_color(s["change_pct"])))
            self.table.setItem(row, 3, _item(_fmt_yi(s["day_amt"]), s["day_amt"],
                                             color=_tone_color(s["day_amt"])))
            self.table.setItem(row, 4, _item(ih.streak_label(s["foreign_streak"]), s["foreign_streak"],
                                             color=_tone_color(s["foreign_streak"])))
            self.table.setItem(row, 5, _item(_fmt_yi(s["sum20"]), s["sum20"],
                                             color=_tone_color(s["sum20"])))
        self.table.setSortingEnabled(True)
        self.table.sortItems(3, QtCore.Qt.DescendingOrder)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)


class InstitutionalFlowPage(QtWidgets.QWidget):
    """左側導覽「法人資金流去哪？」頁。"""

    HEAT_COLUMNS = (
        ("sector", "類股"),
        ("day_amt", "當日買賣超(億)"),
        ("sum5", "近5日買賣超(億)"),
        ("accel", "加速流入(億/天)"),
        ("sum20", "近20日買賣超(億)"),
        ("streak", "連續買賣"),
        ("change5_pct", "近5日漲跌"),
    )

    def __init__(self, db_path, parent=None):
        super().__init__(parent)
        self.db_path = db_path
        self.date = None
        self.rows = {inv: [] for inv in INVESTOR_ORDER}
        self._signature = None
        self._update_task = None
        self._detail_dialog = None
        self._build()

    # ---------- 版面 ----------

    def _build(self):
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(SignalBadge("盤後", tone="info"))
        toolbar.addWidget(QtWidgets.QLabel("資料日期："))
        self.date_combo = QtWidgets.QComboBox()
        self.date_combo.currentIndexChanged.connect(lambda _i: self._load_date())
        toolbar.addWidget(self.date_combo)
        toolbar.addSpacing(12)
        self.update_button = QtWidgets.QPushButton("更新法人資料")
        self.update_button.setToolTip("抓取最新交易日並補齊最近 60 個交易日（每個請求間隔 3 秒）")
        self.update_button.clicked.connect(self._start_update)
        toolbar.addWidget(self.update_button)
        self.status_label = QtWidgets.QLabel()
        self.status_label.setProperty("muted", True)
        toolbar.addWidget(self.status_label, 1)
        layout.addLayout(toolbar)

        summary_card = SectionCard("盤後結論")
        self.insight = InsightCard("資料載入中...", tone="neutral")
        summary_card.body_layout.addWidget(self.insight)
        stat_row = QtWidgets.QHBoxLayout()
        stat_row.setSpacing(8)
        self.stat_cards = {}
        for investor, title in (("foreign", "外資"), ("trust", "投信"),
                                ("dealer", "自營商"), ("all", "三大法人合計")):
            card = StatCard(title=title)
            stat_row.addWidget(card, 1)
            self.stat_cards[investor] = card
        summary_card.body_layout.addLayout(stat_row)
        summary_note = QtWidgets.QLabel(
            "金額為證交所＋櫃買中心公布之三大法人買賣差額（含 ETF），單位億元；紅色買超、綠色賣超。")
        summary_note.setProperty("muted", True)
        summary_card.body_layout.addWidget(summary_note)
        layout.addWidget(summary_card)

        section_bar = QtWidgets.QHBoxLayout()
        section_bar.setSpacing(6)
        self.section_stack = QtWidgets.QStackedWidget()
        section_group = QtWidgets.QButtonGroup(self)
        self.section_buttons = []
        for index, (label, builder) in enumerate((
            ("資金流向泡泡圖", self._build_bubble_section),
            ("今日法人買賣榜", self._build_ranking_section),
            ("近 5 日土洋操作", self._build_tu_yang_section),
            ("法人買賣熱力圖", self._build_heat_section),
        )):
            button = QtWidgets.QPushButton(label)
            button.setCheckable(True)
            button.setProperty("flowSectionButton", True)
            button.clicked.connect(lambda _c, i=index: self.section_stack.setCurrentIndex(i))
            section_group.addButton(button)
            self.section_buttons.append(button)
            section_bar.addWidget(button)
            page = QtWidgets.QWidget()
            page_layout = QtWidgets.QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            builder(page_layout)
            self.section_stack.addWidget(page)
        self.section_buttons[0].setChecked(True)
        section_bar.addStretch(1)
        section_bar.addWidget(QtWidgets.QLabel("法人："))
        self.investor_toggle = InvestorToggle()
        self.investor_toggle.changed.connect(lambda _inv: self._render())
        section_bar.addWidget(self.investor_toggle)
        layout.addLayout(section_bar)
        layout.addWidget(self.section_stack, 1)

        footer = QtWidgets.QLabel(FOOTER_TEXT)
        footer.setProperty("muted", True)
        footer.setWordWrap(True)
        footer.setTextFormat(QtCore.Qt.RichText)
        layout.addWidget(footer)

    def _build_bubble_section(self, layout):
        card = SectionCard("法人資金流向泡泡圖", "右上＝近 5 日買超且正在加速；點泡泡看類股明細")
        size_row = QtWidgets.QHBoxLayout()
        size_row.addWidget(QtWidgets.QLabel("泡泡大小："))
        self.bubble_size_combo = QtWidgets.QComboBox()
        self.bubble_size_combo.addItem("當日成交金額", "value")
        self.bubble_size_combo.addItem("近 20 日買賣超絕對值", "sum20")
        self.bubble_size_combo.currentIndexChanged.connect(lambda _i: self._render_bubble())
        size_row.addWidget(self.bubble_size_combo)
        size_row.addStretch(1)
        card.body_layout.addLayout(size_row)

        self.bubble_chart = _new_plot(420)
        self.bubble_chart.showGrid(x=True, y=True, alpha=0.08)
        self.bubble_chart.setLabel("bottom", "近 5 日買賣超（億）", color=COLOR_TEXT)
        self.bubble_chart.setLabel("left", "加速流入（億／天）", color=COLOR_TEXT)
        card.body_layout.addWidget(self.bubble_chart, 1)
        note = QtWidgets.QLabel(BUBBLE_NOTE + "。滾輪可縮放、拖曳可平移。")
        note.setProperty("muted", True)
        card.body_layout.addWidget(note)

        self.trend_toggle = QtWidgets.QPushButton("▶ 過去 30 天")
        self.trend_toggle.clicked.connect(lambda: self._toggle_trend(not self.trend_panel.isVisible()))
        card.body_layout.addWidget(self.trend_toggle, 0, QtCore.Qt.AlignLeft)
        self.trend_panel = QtWidgets.QWidget()
        trend_layout = QtWidgets.QVBoxLayout(self.trend_panel)
        trend_layout.setContentsMargins(0, 0, 0, 0)
        self.trend_sector_combo = QtWidgets.QComboBox()
        self.trend_sector_combo.currentIndexChanged.connect(lambda _i: self._render_trend())
        trend_layout.addWidget(self.trend_sector_combo, 0, QtCore.Qt.AlignLeft)
        self.trend_chart = SectorFlowChart()
        trend_layout.addWidget(self.trend_chart)
        self.trend_panel.setVisible(False)
        card.body_layout.addWidget(self.trend_panel)
        layout.addWidget(card, 1)

    def _build_ranking_section(self, layout):
        card = SectionCard("今日法人買賣榜", "類股當日買賣超前 10 名，單位：億元")
        row = QtWidgets.QHBoxLayout()
        self.buy_chart = _new_plot(360)
        self.sell_chart = _new_plot(360)
        row.addWidget(self.buy_chart, 1)
        row.addWidget(self.sell_chart, 1)
        card.body_layout.addLayout(row, 1)
        layout.addWidget(card, 1)

    def _build_tu_yang_section(self, layout):
        card = SectionCard("近 5 日土洋操作", "外資（洋）與投信（土）近 5 日類股買賣超比較；不受上方法人切換影響")
        self.tu_yang_chart = _new_plot(440)
        self.tu_yang_chart.showGrid(x=True, y=False, alpha=0.08)
        self.tu_yang_chart.addLegend(offset=(-10, 10))
        card.body_layout.addWidget(self.tu_yang_chart, 1)
        self.tu_yang_badges = QtWidgets.QGridLayout()
        self.tu_yang_badges.setHorizontalSpacing(6)
        self.tu_yang_badges.setVerticalSpacing(4)
        card.body_layout.addLayout(self.tu_yang_badges)
        layout.addWidget(card, 1)

    def _build_heat_section(self, layout):
        card = SectionCard("法人買賣熱力圖", "點欄位標題排序；點一列看類股明細")
        self.heat_table = QtWidgets.QTableWidget(0, len(self.HEAT_COLUMNS))
        self.heat_table.setHorizontalHeaderLabels([label for _k, label in self.HEAT_COLUMNS])
        self.heat_table.verticalHeader().setVisible(False)
        self.heat_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.heat_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.heat_table.setMinimumHeight(520)
        self.heat_table.cellClicked.connect(
            lambda row, _col: self._open_detail(self.heat_table.item(row, 0).text()))
        card.body_layout.addWidget(self.heat_table, 1)
        layout.addWidget(card, 1)

    # ---------- 資料 ----------

    def showEvent(self, event):
        super().showEvent(event)
        self.reload_if_changed()

    def reload_if_changed(self):
        signature = ih.data_signature(self.db_path)
        if signature != self._signature:
            self._signature = signature
            self.reload()

    def reload(self):
        dates = ih.metric_dates(self.db_path)
        current = self.date_combo.currentData()
        self.date_combo.blockSignals(True)
        self.date_combo.clear()
        for d in dates:
            self.date_combo.addItem(d, d)
        index = self.date_combo.findData(current) if current in dates else 0
        self.date_combo.setCurrentIndex(max(index, 0))
        self.date_combo.blockSignals(False)
        if not dates:
            self.date = None
            self.insight.set_content(
                "尚無法人歷史資料",
                "按上方「更新法人資料」回補最近 60 個交易日（約 20 分鐘，可中途關閉程式，下次會接著補）。",
                tone="warning",
            )
            return
        self._load_date()

    def _load_date(self):
        self.date = self.date_combo.currentData()
        if not self.date:
            return
        self.rows = {inv: ih.sector_rows(self.db_path, self.date, inv) for inv in INVESTOR_ORDER}
        amounts = ih.market_amounts(self.db_path, self.date)
        for investor, card in self.stat_cards.items():
            value = amounts[investor]
            card.set_value(_fmt_yi(value), unit="億",
                           status="positive" if value > 0 else "negative" if value < 0 else "neutral")
        text = ih.summary_text(self.rows)
        total = amounts["all"]
        self.insight.set_content(
            text,
            f"{self.date} 三大法人合計{'買超' if total >= 0 else '賣超'} {abs(total):,.1f} 億",
            tone="positive" if total > 0 else "negative" if total < 0 else "neutral",
        )
        sectors = [r["sector"] for r in sorted(self.rows["all"], key=lambda r: -abs(r["sum5"]))]
        current = self.trend_sector_combo.currentText()
        self.trend_sector_combo.blockSignals(True)
        self.trend_sector_combo.clear()
        self.trend_sector_combo.addItems(sectors)
        if current in sectors:
            self.trend_sector_combo.setCurrentText(current)
        self.trend_sector_combo.blockSignals(False)
        self._render()

    def _investor(self):
        return self.investor_toggle.value()

    def _render(self):
        if not self.date:
            return
        self._render_bubble()
        self._render_trend()
        self._render_ranking()
        self._render_tu_yang()
        self._render_heat()

    # ---------- 泡泡圖 ----------

    def _render_bubble(self):
        chart = self.bubble_chart
        chart.clear()
        rows = self.rows.get(self._investor()) or []
        if not rows:
            return
        by_value = self.bubble_size_combo.currentData() == "value"
        size_of = (lambda r: r["trading_value"]) if by_value else (lambda r: abs(r["sum20"]))
        max_size = max((size_of(r) for r in rows), default=0) or 1
        spots = []
        for r in rows:
            accel = r["accel"] if r["accel"] is not None else 0.0
            color = _tone_color(r["sum5"])
            fill = QtGui.QColor(color)
            fill.setAlphaF(0.45)
            tip = (
                f"{r['sector']}（{r['stock_count']} 檔）\n"
                f"近 5 日：{_fmt_yi(r['sum5'])} 億\n"
                f"加速流入：{_fmt_yi(r['accel'])} 億／天\n"
                f"近 20 日：{_fmt_yi(r['sum20'])} 億\n"
                f"當日成交：{r['trading_value']:,.0f} 億\n點擊看類股明細"
            )
            spots.append({
                "pos": (r["sum5"], accel),
                "size": 12 + 48 * (size_of(r) / max_size) ** 0.5,
                "brush": pg.mkBrush(fill),
                "pen": pg.mkPen(color, width=1.5),
                "data": {"sector": r["sector"], "tip": tip},
            })
        scatter = pg.ScatterPlotItem(
            spots=spots, hoverable=True, tip=lambda x, y, data: data["tip"],
            hoverPen=pg.mkPen(COLOR_ACCENT, width=2), antialias=False,
        )
        scatter.sigClicked.connect(
            lambda _p, points, _ev: points and self._open_detail(points[0].data()["sector"]))
        chart.addItem(scatter)
        # 只標離原點最遠（兩軸各自正規化）的 6 個類股；擠在原點附近的標了只會重疊
        max_x = max(abs(r["sum5"]) for r in rows) or 1
        max_y = max(abs(r["accel"] or 0) for r in rows) or 1
        prominent = sorted(rows, key=lambda r: -((r["sum5"] / max_x) ** 2 + ((r["accel"] or 0) / max_y) ** 2))
        span_x = (max(r["sum5"] for r in rows) - min(r["sum5"] for r in rows)) or 1
        span_y = (max(r["accel"] or 0 for r in rows) - min(r["accel"] or 0 for r in rows)) or 1
        placed = []
        for r in prominent:
            if len(placed) == 6:
                break
            x, y = r["sum5"] / span_x, (r["accel"] or 0) / span_y
            if any(abs(x - px) < 0.12 and abs(y - py) < 0.06 for px, py in placed):
                continue  # 會跟已標的名稱重疊，hover 仍看得到
            placed.append((x, y))
            label = pg.TextItem(r["sector"], color=COLOR_TEXT, anchor=(0.5, 1.4))
            label.setPos(r["sum5"], r["accel"] or 0.0)
            chart.addItem(label)
        dash = pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine, width=1)
        chart.addLine(x=0, pen=dash)
        chart.addLine(y=0, pen=dash)
        chart.enableAutoRange()

    def _toggle_trend(self, expanded):
        self.trend_toggle.setText(f"{'▼' if expanded else '▶'} 過去 30 天")
        self.trend_panel.setVisible(expanded)
        self._render_trend()

    def _render_trend(self):
        sector = self.trend_sector_combo.currentText()
        if not (self.trend_panel.isVisible() and sector and self.date):
            return
        self.trend_chart.set_points(
            ih.sector_history(self.db_path, sector, self._investor(), self.date, days=30))

    # ---------- 買賣榜 ----------

    def _render_ranking(self):
        rows = self.rows.get(self._investor()) or []
        buys = sorted((r for r in rows if r["day_amt"] > 0), key=lambda r: -r["day_amt"])[:10]
        sells = sorted((r for r in rows if r["day_amt"] < 0), key=lambda r: r["day_amt"])[:10]
        label = INVESTOR_TAB_LABELS[self._investor()]
        self._draw_rank(self.buy_chart, buys, f"{label}買超前 10", COLOR_GAIN, names_right=False)
        self._draw_rank(self.sell_chart, sells, f"{label}賣超前 10", COLOR_LOSS, names_right=True)

    @staticmethod
    def _draw_rank(chart, rows, title, color, names_right):
        chart.clear()
        plot = chart.getPlotItem()
        plot.setMouseEnabled(x=False, y=False)
        chart.setTitle(title, color=color, size="11pt")
        if not rows:
            plot.getAxis("left").setTicks([[]])
            return
        rows = list(reversed(rows))  # 第一名畫在最上面
        ys = list(range(len(rows)))
        values = [r["day_amt"] for r in rows]
        chart.addItem(pg.BarGraphItem(
            x0=0, y=ys, height=0.62, width=values,
            brush=pg.mkBrush(color), pen=pg.mkPen(None),
        ))
        ticks = [[(y, r["sector"]) for y, r in zip(ys, rows)]]
        name_axis, other_axis = ("right", "left") if names_right else ("left", "right")
        plot.showAxis(name_axis)
        plot.hideAxis(other_axis)
        plot.getAxis(name_axis).setTicks(ticks)
        plot.hideAxis("bottom")
        max_abs = max(abs(v) for v in values) or 1
        for y, v in zip(ys, values):
            text = pg.TextItem(f"{abs(v):,.1f}", color=COLOR_MUTED, anchor=(1.1, 0.5) if v < 0 else (-0.1, 0.5))
            text.setPos(v, y)
            chart.addItem(text)
        span = max_abs * 1.25
        plot.setXRange(-span if names_right else 0, 0 if names_right else span, padding=0)
        plot.setYRange(-0.7, len(rows) - 0.3, padding=0)

    # ---------- 土洋 ----------

    def _render_tu_yang(self):
        chart = self.tu_yang_chart
        chart.clear()
        plot = chart.getPlotItem()
        while self.tu_yang_badges.count():
            widget = self.tu_yang_badges.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        trust = {r["sector"]: r["sum5"] for r in self.rows.get("trust") or []}
        pairs = sorted(
            ((r["sector"], r["sum5"], trust.get(r["sector"], 0.0)) for r in self.rows.get("foreign") or []),
            key=lambda p: -(abs(p[1]) + abs(p[2])),
        )[:12]
        if not pairs:
            return
        pairs.reverse()  # 最大的畫在最上面
        ys = list(range(len(pairs)))
        chart.addItem(pg.BarGraphItem(
            x0=0, y=[y + 0.19 for y in ys], height=0.36, width=[p[1] for p in pairs],
            brush=pg.mkBrush(COLOR_ACCENT), pen=pg.mkPen(None), name="外資",
        ))
        chart.addItem(pg.BarGraphItem(
            x0=0, y=[y - 0.19 for y in ys], height=0.36, width=[p[2] for p in pairs],
            brush=pg.mkBrush(COLOR_WARNING), pen=pg.mkPen(None), name="投信",
        ))
        chart.addLine(x=0, pen=pg.mkPen(COLOR_MUTED, width=1))
        ticks = []
        for y, (sector, foreign, trust_value) in zip(ys, pairs):
            tag = ih.tu_yang_tag(foreign, trust_value)
            ticks.append((y, f"{sector}  {tag[2:]}" if tag else sector))
        tag_count = 0
        for sector, foreign, trust_value in reversed(pairs):  # 徽章依金額大到小
            tag = ih.tu_yang_tag(foreign, trust_value)
            if tag in ("土洋同買", "土洋對作"):
                # 對作用 special（紫），跟同買（紅）區分，不再加一種紅綠
                badge = SignalBadge(f"{tag}・{sector}", tone="positive" if tag == "土洋同買" else "special")
                self.tu_yang_badges.addWidget(badge, tag_count // 4, tag_count % 4)
                tag_count += 1
        plot.getAxis("left").setTicks([ticks])
        plot.setLabel("bottom", "近 5 日買賣超（億）", color=COLOR_MUTED)
        plot.setYRange(-0.7, len(pairs) - 0.3, padding=0)
        plot.enableAutoRange(axis=pg.ViewBox.XAxis)

    # ---------- 熱力圖 ----------

    def _render_heat(self):
        rows = self.rows.get(self._investor()) or []
        table = self.heat_table
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        max_abs = {
            key: max((abs(r[key] or 0) for r in rows), default=0)
            for key, _label in self.HEAT_COLUMNS if key != "sector"
        }
        for row_index, r in enumerate(rows):
            table.setItem(row_index, 0, _item(r["sector"], r["sector"], align_right=False))
            for col, (key, _label) in enumerate(self.HEAT_COLUMNS[1:], start=1):
                value = r[key]
                if key == "streak":
                    text = ih.streak_label(value)
                elif key == "change5_pct":
                    text = _fmt_pct(value)
                else:
                    text = _fmt_yi(value)
                table.setItem(row_index, col, _item(
                    text, value, background=_heat_color(value, max_abs[key])))
        table.setSortingEnabled(True)
        table.sortItems(1, QtCore.Qt.DescendingOrder)
        table.resizeColumnsToContents()
        table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)

    # ---------- 互動 ----------

    def _open_detail(self, sector):
        if not self.date:
            return
        self._detail_dialog = SectorDetailDialog(
            self.db_path, sector, self.date, self._investor(), self)
        self._detail_dialog.show()

    def _start_update(self):
        if self._update_task is not None:
            return
        self.update_button.setEnabled(False)
        self.status_label.setText("更新中…（每個請求間隔 3 秒）")

        def work(_cancel_event, emit):
            return ih.backfill(self.db_path, log=emit)

        def done(written):
            self._update_task = None
            self.update_button.setEnabled(True)
            self.status_label.setText(
                f"已更新 {len(written)} 個交易日" if written else "已是最新（或今日資料尚未公布）")
            self.reload_if_changed()

        def failed(message):
            self._update_task = None
            self.update_button.setEnabled(True)
            self.status_label.setText(f"更新失敗：{message}")

        # 回傳值一定要留住，否則 BackgroundTask 會被回收、done 永遠不會觸發（見 ARCHITECTURE.md）
        self._update_task = run_background_task(
            self, work, done, failed, on_progress=self.status_label.setText)
