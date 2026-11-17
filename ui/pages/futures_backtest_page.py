"""「個股期回測」頁：實單績效統計＋What-if 出場模擬。

資料都來自 tradingnote_futures_backtest（交易紀錄在「部位紀錄」頁手動輸入，或用
本頁「匯入 CSV」）。讀紀錄與跑模擬都在背景執行緒（模擬可能要連網補期交所日 K），
頁面只負責畫。資料庫路徑一律在呼叫當下讀 app_paths.HISTORY_DB_PATH。"""

from datetime import date, timedelta

from PySide6 import QtCore, QtGui, QtWidgets

import tradingnote_futures_backtest as fb
from tradingnote_tasks import run_background_task
from tradingnote_tradejournal import list_strategies
from ui import app_paths
from ui.charts.base import (
    empty_state,
    line,
    new_plot,
    ref_line,
    series_color,
    set_date_ticks,
    set_title,
    signed_bars,
    tone_color,
)
from ui.components.section_card import SectionCard
from ui.components.stat_card import StatCard
from ui.dialogs.futures_import import FuturesImportDialog, save_template
from ui.dialogs.positions import lookup_futures_code

_PERIODS = (("全部", None), ("今年", "ytd"), ("近 3 個月", 92), ("近 1 年", 365))
_SIDES = (("多空", None), ("多單", "LONG"), ("空單", "SHORT"))
_TRADE_COLUMNS = [
    "出場日", "標的", "期貨", "月份", "多空", "口數", "進場日", "進場均價",
    "出場均價", "手續費＋稅", "淨損益", "R", "持有天數", "策略",
]
_WHATIF_COLUMNS = [
    "標的", "多空", "進場日", "進場均價", "實際出場日", "實際淨損益",
    "模擬出場日", "模擬出場價", "出場原因", "模擬淨損益", "差異", "備註",
]
ASSUMPTIONS = (
    "模擬假設：進場用實際第一筆進場日與全部進場口數的均價；從進場隔天的期交所日盤 K 開始檢查；"
    "跳空越過停損／停利以開盤價成交；同一根同時碰到停損與停利當作先停損；"
    "到期日（第三個星期三）以結算價出場；夜盤不納入；手續費沿用實單、期交稅重算。"
)


def _money(value):
    return "—" if value is None else f"{value:+,.0f}"


def _pct(value):
    return "—" if value is None else f"{value * 100:.1f}%"


def _ratio(value, none_text="—"):
    return none_text if value is None else f"{value:.2f}"


def _item(text, color=None, align_right=False):
    item = QtWidgets.QTableWidgetItem(str(text))
    if color:
        item.setForeground(QtGui.QColor(color))
    if align_right:
        item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
    return item


def _table(columns):
    table = QtWidgets.QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    table.setMinimumHeight(220)
    return table


def _aligned_curves(curves):
    """多條 [(date, value)] 對齊到同一組日期（沒出場的日子沿用前值）。"""
    dates = sorted({d for curve in curves for d, _ in curve})
    aligned = []
    for curve in curves:
        values = dict(curve)
        current, series = 0.0, []
        for d in dates:
            current = values.get(d, current)
            series.append(current)
        aligned.append(series)
    return dates, aligned


class FuturesBacktestPage(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._load_task = None
        self._whatif_task = None
        self._results = []
        self._stats = fb.Stats()
        self._whatif_report = None

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setSpacing(8)
        self.period_combo = QtWidgets.QComboBox()
        for label, value in _PERIODS:
            self.period_combo.addItem(label, value)
        self.side_combo = QtWidgets.QComboBox()
        for label, value in _SIDES:
            self.side_combo.addItem(label, value)
        self.strategy_combo = QtWidgets.QComboBox()
        self.strategy_combo.addItem("全部策略", None)
        for combo in (self.period_combo, self.side_combo, self.strategy_combo):
            combo.currentIndexChanged.connect(self.reload)
            toolbar.addWidget(combo)
        toolbar.addStretch(1)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setProperty("muted", True)
        toolbar.addWidget(self.status_label)
        import_button = QtWidgets.QPushButton("匯入 CSV…")
        import_button.clicked.connect(self.open_import_dialog)
        toolbar.addWidget(import_button)
        template_button = QtWidgets.QPushButton("下載範本…")
        template_button.clicked.connect(lambda: save_template(self))
        toolbar.addWidget(template_button)
        refresh_button = QtWidgets.QPushButton("重新整理")
        refresh_button.clicked.connect(self.reload)
        toolbar.addWidget(refresh_button)
        outer.addLayout(toolbar)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._scrolling(self._build_actual_tab()), "實單績效")
        self.tabs.addTab(self._scrolling(self._build_whatif_tab()), "What-if 出場模擬")
        outer.addWidget(self.tabs, 1)

    @staticmethod
    def _scrolling(widget):
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QtWidgets.QFrame.NoFrame)
        area.setWidget(widget)
        return area

    # ---------- 版面 ----------

    def _build_actual_tab(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)

        self.kpi_cards = {}
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(8)
        specs = (
            ("total", "總淨損益"), ("count", "已平倉筆數"), ("win_rate", "勝率"),
            ("payoff", "盈虧比（均賺÷均賠）"), ("pf", "獲利因子"),
            ("expectancy", "每筆期望值"), ("mdd", "最大回撤"), ("streak", "最大連虧"),
            ("holding", "平均持有天數"), ("avg_r", "平均 R"),
        )
        for index, (key, title) in enumerate(specs):
            card = StatCard(title=title)
            self.kpi_cards[key] = card
            grid.addWidget(card, index // 5, index % 5)
        layout.addLayout(grid)

        charts = SectionCard("權益曲線", "依出場日累計淨損益；下方為距前高的回撤")
        self.equity_chart = new_plot(min_height=240)
        self.drawdown_chart = new_plot(min_height=120)
        self.drawdown_chart.setMaximumHeight(160)
        self.drawdown_chart.setXLink(self.equity_chart)
        charts.body_layout.addWidget(self.equity_chart)
        charts.body_layout.addWidget(self.drawdown_chart)
        layout.addWidget(charts)

        groups = QtWidgets.QHBoxLayout()
        month_card = SectionCard("月別淨損益")
        self.month_chart = new_plot(min_height=200, grid="y", interaction="static")
        month_card.body_layout.addWidget(self.month_chart)
        groups.addWidget(month_card, 3)
        group_card = SectionCard("分組", "多空／策略")
        self.group_table = _table(["分組", "筆數", "勝率", "淨損益"])
        self.group_table.setMinimumHeight(200)
        group_card.body_layout.addWidget(self.group_table)
        groups.addWidget(group_card, 2)
        layout.addLayout(groups)

        trades_card = SectionCard("已平倉交易")
        self.trade_table = _table(_TRADE_COLUMNS)
        trades_card.body_layout.addWidget(self.trade_table)
        layout.addWidget(trades_card)
        return page

    def _build_whatif_tab(self):
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)

        rule_card = SectionCard("出場規則", "保留實際進場，只換出場方式；條件可以組合")
        form = QtWidgets.QGridLayout()
        form.setHorizontalSpacing(10)

        self.stop_mode_combo = QtWidgets.QComboBox()
        self.stop_mode_combo.addItem("不設停損", "none")
        self.stop_mode_combo.addItem("固定 % 停損", "pct")
        self.stop_mode_combo.addItem("用原計畫停損價", "plan")
        self.stop_pct_spin = self._spin(0.1, 50, 5.0, "%")
        form.addWidget(QtWidgets.QLabel("停損"), 0, 0)
        form.addWidget(self.stop_mode_combo, 0, 1)
        form.addWidget(self.stop_pct_spin, 0, 2)

        self.tp_mode_combo = QtWidgets.QComboBox()
        self.tp_mode_combo.addItem("不設停利", "none")
        self.tp_mode_combo.addItem("固定 % 停利", "pct")
        self.tp_mode_combo.addItem("R 倍數停利", "r")
        self.tp_value_spin = self._spin(0.1, 100, 10.0, "")
        form.addWidget(QtWidgets.QLabel("停利"), 1, 0)
        form.addWidget(self.tp_mode_combo, 1, 1)
        form.addWidget(self.tp_value_spin, 1, 2)

        self.trailing_spin = self._spin(0, 50, 0, "%")
        self.trailing_spin.setSpecialValueText("不使用")
        form.addWidget(QtWidgets.QLabel("移動停損"), 2, 0)
        form.addWidget(self.trailing_spin, 2, 1)

        self.hold_spin = QtWidgets.QSpinBox()
        self.hold_spin.setRange(0, 250)
        self.hold_spin.setSpecialValueText("不限（抱到到期）")
        self.hold_spin.setSuffix(" 天")
        form.addWidget(QtWidgets.QLabel("最多持有"), 3, 0)
        form.addWidget(self.hold_spin, 3, 1)
        form.setColumnStretch(3, 1)
        rule_card.body_layout.addLayout(form)

        run_row = QtWidgets.QHBoxLayout()
        self.run_button = QtWidgets.QPushButton("執行模擬")
        self.run_button.setProperty("accent", True)
        self.run_button.clicked.connect(lambda: self.run_whatif())
        run_row.addWidget(self.run_button)
        self.whatif_status = QtWidgets.QLabel("")
        self.whatif_status.setProperty("muted", True)
        run_row.addWidget(self.whatif_status, 1)
        rule_card.body_layout.addLayout(run_row)
        note = QtWidgets.QLabel(ASSUMPTIONS)
        note.setWordWrap(True)
        note.setProperty("muted", True)
        rule_card.body_layout.addWidget(note)
        layout.addWidget(rule_card)

        self.tp_mode_combo.currentIndexChanged.connect(self._sync_rule_inputs)
        self.stop_mode_combo.currentIndexChanged.connect(self._sync_rule_inputs)
        self._sync_rule_inputs()

        compare_card = SectionCard("實際 vs 模擬", "只比較抓得到期貨日 K 的同一批交易")
        self.compare_table = _table(["指標", "實際", "模擬", "差異"])
        self.compare_table.setMinimumHeight(260)
        compare_card.body_layout.addWidget(self.compare_table)
        self.whatif_chart = new_plot(min_height=240, legend=True)
        compare_card.body_layout.addWidget(self.whatif_chart)
        layout.addWidget(compare_card)

        rows_card = SectionCard("逐筆比較")
        self.whatif_table = _table(_WHATIF_COLUMNS)
        rows_card.body_layout.addWidget(self.whatif_table)
        layout.addWidget(rows_card)
        return page

    @staticmethod
    def _spin(minimum, maximum, value, suffix):
        spin = QtWidgets.QDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(1)
        spin.setSingleStep(0.5)
        spin.setValue(value)
        if suffix:
            spin.setSuffix(f" {suffix}")
        return spin

    def _sync_rule_inputs(self, *_args):
        self.stop_pct_spin.setEnabled(self.stop_mode_combo.currentData() == "pct")
        tp_mode = self.tp_mode_combo.currentData()
        self.tp_value_spin.setEnabled(tp_mode != "none")
        self.tp_value_spin.setSuffix(" R" if tp_mode == "r" else " %")

    # ---------- 資料 ----------

    def showEvent(self, event):
        super().showEvent(event)
        self.reload_strategies()
        self.reload()

    def reload_strategies(self):
        current = self.strategy_combo.currentData()
        self.strategy_combo.blockSignals(True)
        self.strategy_combo.clear()
        self.strategy_combo.addItem("全部策略", None)
        for strategy in list_strategies(app_paths.HISTORY_DB_PATH):
            self.strategy_combo.addItem(strategy.name, strategy.strategy_id)
        index = self.strategy_combo.findData(current)
        self.strategy_combo.setCurrentIndex(max(index, 0))
        self.strategy_combo.blockSignals(False)

    def filters(self, today=None):
        today = today or date.today()
        period = self.period_combo.currentData()
        date_from = None
        if period == "ytd":
            date_from = date(today.year, 1, 1).isoformat()
        elif period:
            date_from = (today - timedelta(days=period)).isoformat()
        return {
            "date_from": date_from,
            "side": self.side_combo.currentData(),
            "strategy_id": self.strategy_combo.currentData(),
        }

    def reload(self, *_args):
        if self._load_task is not None:
            self._load_task.cancel()
        db_path = app_paths.HISTORY_DB_PATH
        filters = self.filters()
        self.status_label.setText("讀取中…")

        def work(_cancel, _progress):
            results = fb.closed_trade_results(db_path, **filters)
            return results, fb.compute_stats(results)

        self._load_task = run_background_task(self, work, self._on_loaded, self._on_load_error)

    def _on_load_error(self, message):
        self._load_task = None
        self.status_label.setText(f"讀取失敗：{message}")

    def _on_loaded(self, payload):
        self._load_task = None
        self._results, self._stats = payload
        self.status_label.setText(
            f"{self._stats.count} 筆已平倉個股期交易" if self._stats.count else "尚無已平倉的個股期交易"
        )
        self._render_actual()

    # ---------- 實單績效 ----------

    def _render_actual(self):
        stats, results = self._stats, self._results
        cards = self.kpi_cards
        cards["total"].set_value(_money(stats.total_net) if stats.count else "—", unit="元",
                                 status=self._status(stats.total_net))
        cards["count"].set_value(stats.count, change=f"贏 {stats.wins}／輸 {stats.losses}")
        cards["win_rate"].set_value(_pct(stats.win_rate))
        cards["payoff"].set_value(_ratio(stats.payoff_ratio))
        cards["pf"].set_value(_ratio(stats.profit_factor, "∞" if stats.wins else "—"))
        cards["expectancy"].set_value(_money(stats.expectancy), unit="元",
                                      status=self._status(stats.expectancy))
        period = (
            f"{stats.max_drawdown_peak_date} → {stats.max_drawdown_trough_date}"
            if stats.max_drawdown_trough_date else None
        )
        cards["mdd"].set_value(f"{-stats.max_drawdown:,.0f}" if stats.count else "—", unit="元",
                               change=period)
        cards["streak"].set_value(stats.max_consecutive_losses if stats.count else "—", unit="筆")
        cards["holding"].set_value(
            "—" if stats.avg_holding_days is None else f"{stats.avg_holding_days:.1f}", unit="天"
        )
        cards["avg_r"].set_value(_ratio(stats.avg_r), change="有設停損的交易才算")

        self._render_equity(stats)
        self._render_months(stats)
        self._render_groups(stats)
        self._render_trades(results)

    @staticmethod
    def _status(value):
        if not value:
            return None
        return "positive" if value > 0 else "negative"

    def _render_equity(self, stats):
        for chart in (self.equity_chart, self.drawdown_chart):
            chart.clear()
        if not stats.equity_curve:
            empty_state(self.equity_chart, "尚無已平倉交易")
            return
        dates = [d for d, _ in stats.equity_curve]
        xs = list(range(len(dates)))
        line(self.equity_chart, xs, [v for _, v in stats.equity_curve], "累計淨損益", index=0)
        ref_line(self.equity_chart, y=0)
        line(self.drawdown_chart, xs, [v for _, v in stats.drawdown_curve], "回撤",
             color=tone_color(-1))
        for chart in (self.equity_chart, self.drawdown_chart):
            set_date_ticks(chart, dates)
            chart.getPlotItem().getViewBox().autoRange()

    def _render_months(self, stats):
        self.month_chart.clear()
        if not stats.by_month:
            empty_state(self.month_chart, "尚無資料")
            return
        signed_bars(self.month_chart, range(len(stats.by_month)), [g.net_pnl for g in stats.by_month])
        self.month_chart.getAxis("bottom").setTicks(
            [[(i, g.key) for i, g in enumerate(stats.by_month)]]
        )
        ref_line(self.month_chart, y=0, dashed=False)
        self.month_chart.getPlotItem().getViewBox().autoRange()

    def _render_groups(self, stats):
        rows = [*stats.by_side, *stats.by_strategy]
        self.group_table.setRowCount(len(rows))
        for r, group in enumerate(rows):
            values = (
                _item(group.key), _item(group.count, align_right=True),
                _item(_pct(group.win_rate), align_right=True),
                _item(_money(group.net_pnl), tone_color(group.net_pnl), True),
            )
            for c, item in enumerate(values):
                self.group_table.setItem(r, c, item)
        self.group_table.resizeColumnsToContents()

    def _render_trades(self, results):
        rows = list(reversed(results))
        self.trade_table.setRowCount(len(rows))
        for r, t in enumerate(rows):
            r_text = "—" if t.r_multiple is None else f"{t.r_multiple:+.2f}"
            values = (
                _item(t.exit_date), _item(t.symbol), _item(t.futures_code or "—"),
                _item(t.contract_month or "—"), _item("多" if t.side == "LONG" else "空"),
                _item(f"{t.qty:g}", align_right=True), _item(t.entry_date),
                _item(f"{t.avg_entry:,.2f}", align_right=True),
                _item(f"{t.avg_exit:,.2f}", align_right=True),
                _item(f"{t.commission + t.tax:,.0f}", align_right=True),
                _item(_money(t.net_pnl), tone_color(t.net_pnl), True),
                _item(r_text, align_right=True), _item(t.holding_days, align_right=True),
                _item(t.strategy),
            )
            for c, item in enumerate(values):
                self.trade_table.setItem(r, c, item)
        self.trade_table.resizeColumnsToContents()

    # ---------- What-if ----------

    def current_rule(self):
        tp_mode = self.tp_mode_combo.currentData()
        return fb.ExitRule(
            stop_mode=self.stop_mode_combo.currentData(),
            stop_pct=self.stop_pct_spin.value(),
            take_profit_mode=tp_mode,
            take_profit_pct=self.tp_value_spin.value() if tp_mode == "pct" else None,
            take_profit_r=self.tp_value_spin.value() if tp_mode == "r" else None,
            max_hold_days=self.hold_spin.value() or None,
            trailing_pct=self.trailing_spin.value() or None,
        )

    def run_whatif(self, fetch_bars=None):
        if self._whatif_task is not None:
            return
        db_path = app_paths.HISTORY_DB_PATH
        rule = self.current_rule()
        filters = self.filters()
        self.run_button.setEnabled(False)
        self.whatif_status.setText(f"模擬中（{rule.describe()}）…第一次會從期交所下載日 K，可能需要一點時間")

        def work(_cancel, progress):
            return fb.run_whatif(
                db_path, rule, fetch_bars=fetch_bars,
                on_progress=lambda done, total: progress(done, total), **filters,
            )

        self._whatif_task = run_background_task(
            self, work, self._on_whatif_done, self._on_whatif_error,
            on_progress=lambda done, total: self.whatif_status.setText(f"模擬中… {done}/{total}"),
        )

    def _on_whatif_error(self, message):
        self._whatif_task = None
        self.run_button.setEnabled(True)
        self.whatif_status.setText(f"模擬失敗：{message}")

    def _on_whatif_done(self, report):
        self._whatif_task = None
        self.run_button.setEnabled(True)
        self._whatif_report = report
        skipped = f"，{report.skipped} 筆沒有期貨日 K 未納入" if report.skipped else ""
        self.whatif_status.setText(
            f"完成：{report.rule.describe()}；比較 {report.actual.count} 筆{skipped}"
        )
        self._render_compare(report)
        self._render_whatif_chart(report)
        self._render_whatif_rows(report)

    def _render_compare(self, report):
        a, s = report.actual, report.simulated

        def diff(x, y, fmt):
            return "—" if x is None or y is None else fmt(y - x)

        rows = (
            ("總淨損益", a.total_net, s.total_net, _money, _money),
            ("勝率", a.win_rate, s.win_rate, _pct, lambda v: f"{v * 100:+.1f} pp"),
            ("盈虧比", a.payoff_ratio, s.payoff_ratio, _ratio, lambda v: f"{v:+.2f}"),
            ("獲利因子", a.profit_factor, s.profit_factor, _ratio, lambda v: f"{v:+.2f}"),
            ("每筆期望值", a.expectancy, s.expectancy, _money, _money),
            ("最大回撤", -a.max_drawdown, -s.max_drawdown, _money, _money),
            ("最大連虧", a.max_consecutive_losses, s.max_consecutive_losses,
             lambda v: f"{v} 筆", lambda v: f"{v:+d} 筆"),
        )
        self.compare_table.setRowCount(len(rows))
        for r, (label, actual, simulated, fmt, diff_fmt) in enumerate(rows):
            delta = None if actual is None or simulated is None else simulated - actual
            values = (
                _item(label),
                _item("—" if actual is None else fmt(actual), align_right=True),
                _item("—" if simulated is None else fmt(simulated), align_right=True),
                _item(diff(actual, simulated, diff_fmt), tone_color(delta), True),
            )
            for c, item in enumerate(values):
                self.compare_table.setItem(r, c, item)
        self.compare_table.resizeColumnsToContents()

    def _render_whatif_chart(self, report):
        chart = self.whatif_chart
        chart.clear()
        if not report.actual.equity_curve:
            empty_state(chart, "沒有可比較的交易")
            return
        set_title(chart, "累計淨損益：實際 vs 模擬")
        dates, (actual, simulated) = _aligned_curves(
            [report.actual.equity_curve, report.simulated.equity_curve]
        )
        xs = list(range(len(dates)))
        line(chart, xs, actual, "實際", color=series_color("實際", 0))
        line(chart, xs, simulated, "模擬", color=series_color("模擬", 1))
        ref_line(chart, y=0)
        set_date_ticks(chart, dates)
        chart.getPlotItem().getViewBox().autoRange()

    def _render_whatif_rows(self, report):
        rows = report.rows
        self.whatif_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            t, sim = row.trade, row.sim
            values = [
                _item(t.symbol), _item("多" if t.side == "LONG" else "空"), _item(t.entry_date),
                _item(f"{t.avg_entry:,.2f}", align_right=True), _item(t.exit_date),
                _item(_money(t.net_pnl), tone_color(t.net_pnl), True),
            ]
            if sim is None:
                values += [_item("—"), _item("—"), _item("—"), _item("—"), _item("—")]
            else:
                values += [
                    _item(sim.exit_date), _item(f"{sim.exit_price:,.2f}", align_right=True),
                    _item(sim.reason), _item(_money(sim.net_pnl), tone_color(sim.net_pnl), True),
                    _item(_money(row.diff), tone_color(row.diff), True),
                ]
            values.append(_item(row.note))
            for c, item in enumerate(values):
                self.whatif_table.setItem(r, c, item)
        self.whatif_table.resizeColumnsToContents()

    # ---------- 匯入 ----------

    def open_import_dialog(self):
        dialog = FuturesImportDialog(
            self, app_paths.HISTORY_DB_PATH, on_imported=lambda _result: self.reload(),
            code_lookup=lookup_futures_code,
        )
        dialog.exec()
        self.reload_strategies()
