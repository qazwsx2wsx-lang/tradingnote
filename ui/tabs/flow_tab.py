"""「資金流向分析」分頁（泡泡圖、法人方向、資金動向清單、個股資金流入、量比異常）。

以 mixin 形式併入 tradingnote_gui.TradingNoteWindow；方法直接使用視窗的 self 狀態。"""

from datetime import date

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from tradingnote_concepts import CLASSIFICATION_INDUSTRY, CLASSIFICATION_LABELS, CLASSIFICATION_VALUE_CHAIN_LEAF
from tradingnote_flow import FlowPeriod
from tradingnote_history import VOLUME_RATIO_TIERS, get_available_dates, get_data_revision
from tradingnote_tasks import run_background_task

from ui import app_paths
from ui.badges import _flow_momentum_insight
from ui.charts.flow_chart import (
    FlowChartWidget,
    _category_color,
    _populate_institutional_direction_chart,
    populate_flow_chart,
)
from ui.components.insight_card import InsightCard
from ui.components.section_card import SectionCard
from ui.components.signal_badge import SignalBadge
from ui.components.stat_card import StatCard
from ui.dialogs.stock_detail import IndustryTopStocksDialog, StockDetailDialog
from ui.dialogs.trading_date import TradingDateDialog
from ui.format import gain_loss_color
from ui.theme import (
    COLOR_ACCENT,
    COLOR_CARD_BG,
    COLOR_LOSS,
    COLOR_MUTED,
)
from ui.widgets import _color_swatch_icon, _set_standard_icon


class FlowTabMixin:
    """TradingNoteWindow 的「資金流向分析」分頁方法。"""

    # ---------- 資金流向分析頁 ----------

    def _build_flow_tab(self):
        # 外層保留一層 QScrollArea 作為小視窗／高 DPI 的最後防線；主要內容改用
        # 「分類按鍵＋單一內容頁」呈現，避免泡泡圖、產業清單、個股清單與爆量
        # 清單同時垂直堆疊，讓資金流向頁不必長距離捲動。
        outer_layout = QtWidgets.QVBoxLayout(self.flow_tab)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        outer_layout.addWidget(scroll)

        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(10, 10, 10, 10)

        self.flow_period = FlowPeriod(
            "0001-01-01", 5
        )

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(QtWidgets.QLabel("流向區間："))
        self.flow_date_button = QtWidgets.QPushButton()
        _set_standard_icon(
            self.flow_date_button,
            QtWidgets.QStyle.SP_FileDialogDetailedView,
            "選擇流向區間",
        )
        self.flow_date_button.clicked.connect(self._open_flow_date_picker)
        toolbar.addWidget(self.flow_date_button)

        toolbar.addSpacing(16)
        toolbar.addWidget(QtWidgets.QLabel("分類方式："))
        self.flow_classification_combo = QtWidgets.QComboBox()
        for classification_mode, label in CLASSIFICATION_LABELS.items():
            if classification_mode == CLASSIFICATION_VALUE_CHAIN_LEAF:
                available = bool(self._classification_catalog.value_chain_scopes)
            else:
                available = bool(
                    self._classification_catalog.groups(classification_mode)
                )
            if classification_mode != CLASSIFICATION_INDUSTRY and not available:
                continue
            self.flow_classification_combo.addItem(label, classification_mode)
        self.flow_classification_combo.currentIndexChanged.connect(
            self._on_flow_classification_changed
        )
        toolbar.addWidget(self.flow_classification_combo)

        self.flow_classification_scope_label = QtWidgets.QLabel("主鏈：")
        self.flow_classification_scope_combo = QtWidgets.QComboBox()
        for scope in self._classification_catalog.value_chain_scopes:
            self.flow_classification_scope_combo.addItem(scope, scope)
        self.flow_classification_scope_combo.currentIndexChanged.connect(
            lambda _: self.refresh_flow_tab()
        )
        self.flow_classification_scope_label.setVisible(False)
        self.flow_classification_scope_combo.setVisible(False)
        toolbar.addWidget(self.flow_classification_scope_label)
        toolbar.addWidget(self.flow_classification_scope_combo)

        toolbar.addSpacing(16)
        toolbar.addWidget(QtWidgets.QLabel("泡泡圖模式："))
        self.flow_bubble_mode_combo = QtWidgets.QComboBox()
        self.flow_bubble_mode_combo.addItem("動能象限（漲跌% × 量比）", "momentum")
        self.flow_bubble_mode_combo.addItem("估值象限（PER/PBR × 資金熱度）", "valuation")
        self.flow_bubble_mode_combo.addItem("主力同步買超（三大法人方向一致）", "institutional_sync")
        self.flow_bubble_mode_combo.currentIndexChanged.connect(self._on_flow_bubble_mode_changed)
        toolbar.addWidget(self.flow_bubble_mode_combo)

        self.flow_valuation_metric_combo = QtWidgets.QComboBox()
        self.flow_valuation_metric_combo.addItem("本益比 PER", "per")
        self.flow_valuation_metric_combo.addItem("股價淨值比 PBR", "pbr")
        self.flow_valuation_metric_combo.currentIndexChanged.connect(lambda _: self.refresh_flow_tab())
        self.flow_valuation_metric_combo.setVisible(False)
        toolbar.addWidget(self.flow_valuation_metric_combo)

        toolbar.addStretch(1)
        layout.addLayout(toolbar)
        self._update_flow_date_button()

        section_bar = QtWidgets.QHBoxLayout()
        section_bar.setSpacing(6)
        section_bar.addWidget(QtWidgets.QLabel("顯示分類："))
        self.flow_section_group = QtWidgets.QButtonGroup(self)
        self.flow_section_group.setExclusive(True)
        self.flow_section_buttons = []

        self.flow_section_stack = QtWidgets.QStackedWidget()
        self.flow_section_stack.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding
        )

        sections = (
            ("族群泡泡圖", self._build_flow_chart_section),
            ("法人方向", self._build_institutional_direction_section),
            ("族群熱度排行", self._build_flow_list_section),
            ("資金流入前 50", self._build_stock_capital_flow_section),
            ("個股爆量", self._build_volume_outliers_section),
            ("法人資金流去哪？", self._build_institutional_flow_page_section),
        )
        for index, (label, builder) in enumerate(sections):
            button = QtWidgets.QPushButton(label)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setProperty("flowSectionButton", True)
            button.clicked.connect(
                lambda _checked, page_index=index: self.flow_section_stack.setCurrentIndex(
                    page_index
                )
            )
            self.flow_section_group.addButton(button, index)
            self.flow_section_buttons.append(button)
            section_bar.addWidget(button)

            page = QtWidgets.QWidget()
            page_layout = QtWidgets.QVBoxLayout(page)
            page_layout.setContentsMargins(0, 8, 0, 0)
            builder(page_layout)
            self.flow_section_stack.addWidget(page)

        section_bar.addStretch(1)
        layout.addLayout(section_bar)
        layout.addWidget(self.flow_section_stack, 1)
        self.flow_section_buttons[0].setChecked(True)

    def _build_institutional_flow_page_section(self, layout):
        # 原本是左側導覽的獨立頁，整頁原樣嵌進來（自帶工具列／捲動區）。
        # 外層已是 QScrollArea，內頁給足最小高度才不會被壓成一條。
        self.institutional_flow_tab.setMinimumHeight(780)
        layout.addWidget(self.institutional_flow_tab, 1)

    def _build_institutional_direction_section(self, layout):
        card = SectionCard(
            title="三大法人族群調整方向",
            description=(
                "每張圖獨立顯示該法人的前八大族群：右側為買超、左側為賣超。"
                "方向採交易所實際淨買賣股數，淨額以當日收盤價估算。"
            ),
        )
        self.institutional_direction_date_label = QtWidgets.QLabel("資料尚未載入")
        self.institutional_direction_date_label.setProperty("muted", True)
        card.header_row.addWidget(self.institutional_direction_date_label)

        chart_row = QtWidgets.QHBoxLayout()
        chart_row.setSpacing(8)
        self.institutional_direction_charts = []
        for _title in ("外資", "投信", "自營商"):
            chart = pg.PlotWidget()
            chart.setBackground(COLOR_CARD_BG)
            chart.showGrid(x=True, y=False, alpha=0.08)
            chart.setMinimumSize(180, 260)
            chart_row.addWidget(chart, 1)
            self.institutional_direction_charts.append(chart)
        card.body_layout.addLayout(chart_row, 1)
        layout.addWidget(card, 1)

    def _refresh_institutional_direction(self, dashboard):
        rows = dashboard.institutional_flow
        data_date = max((row.date for row in rows if row.date), default="—")
        covered = max((row.covered_stocks for row in rows), default=0)
        self.institutional_direction_date_label.setText(
            f"資料日 {data_date}・單一族群最多涵蓋 {covered} 檔"
        )
        for chart, (value_attr, title) in zip(
            self.institutional_direction_charts,
            (
                ("foreign_value", "外資"),
                ("trust_value", "投信"),
                ("dealer_value", "自營商"),
            ),
        ):
            _populate_institutional_direction_chart(chart, rows, value_attr, title)

    def _build_flow_chart_section(self, layout):
        self.flow_chart_header = QtWidgets.QLabel("官方產業資金流向圖")
        self.flow_chart_header.setProperty("header", True)
        layout.addWidget(self.flow_chart_header)
        summary_row = QtWidgets.QHBoxLayout()
        summary_row.setSpacing(8)
        self.flow_summary_cards = []
        for title in ("今日偏流入", "今日偏流出", "成交最活躍"):
            card = StatCard(title=title)
            summary_row.addWidget(card, 1)
            self.flow_summary_cards.append(card)
        layout.addLayout(summary_row)
        layout.addSpacing(8)

        self.flow_insight_card = InsightCard("資料載入中...", tone="neutral")
        layout.addWidget(self.flow_insight_card)
        layout.addSpacing(4)

        legend_row = QtWidgets.QHBoxLayout()
        legend_row.setSpacing(6)
        # 圖例徽章文字依泡泡圖模式而變（見 _on_flow_bubble_mode_changed）：
        # 動能/估值模式代表當日量價方向，主力同步買超模式代表三大法人一致性。
        self.flow_direction_badge_positive = SignalBadge("偏流入", tone="positive")
        self.flow_direction_badge_negative = SignalBadge("偏流出", tone="negative")
        self.flow_direction_badge_neutral = SignalBadge("中性", tone="neutral")
        legend_row.addWidget(self.flow_direction_badge_positive)
        legend_row.addWidget(self.flow_direction_badge_negative)
        legend_row.addWidget(self.flow_direction_badge_neutral)
        legend_row.addStretch(1)
        layout.addLayout(legend_row)
        layout.addSpacing(4)

        self.flow_chart_hint = QtWidgets.QLabel(
            "泡泡填色＝所屬分類（對照下方清單色塊）；外框顏色＝方向（見上方圖例）；"
            "標示成交佔比前五大族群，滑鼠移至泡泡查看數值。"
        )
        self.flow_chart_hint.setProperty("muted", True)
        self.flow_chart_hint.setWordWrap(True)
        layout.addWidget(self.flow_chart_hint)
        layout.addSpacing(8)
        self.flow_chart = FlowChartWidget()
        self.flow_chart.setMinimumHeight(420)
        layout.addWidget(self.flow_chart, 1)

    def _update_flow_date_button(self):
        period = self.flow_period.resolve(app_paths.HISTORY_DB_PATH, self.snapshot)
        start = period.actual_dates[0] if period.actual_dates else period.start_date
        self.flow_date_button.setText(
            f"{start} ～ {period.end_date} · 近 {period.trading_days} 日"
        )

        self.flow_date_button.setToolTip(
            f"實際有 {len(period.actual_dates)}／需要 {period.trading_days} 個交易日；點擊選日期"
        )

    def _on_flow_bubble_mode_changed(self, _index):
        is_valuation = self.flow_bubble_mode_combo.currentData() == "valuation"
        self.flow_valuation_metric_combo.setVisible(is_valuation)
        if self.flow_bubble_mode_combo.currentData() == "institutional_sync":
            self.flow_direction_badge_positive.set_text_and_tone("主力同步買超", "positive")
            self.flow_direction_badge_negative.set_text_and_tone("主力同步賣超", "negative")
            self.flow_direction_badge_neutral.set_text_and_tone("方向不一致", "neutral")
        else:
            self.flow_direction_badge_positive.set_text_and_tone("偏流入", "positive")
            self.flow_direction_badge_negative.set_text_and_tone("偏流出", "negative")
            self.flow_direction_badge_neutral.set_text_and_tone("中性", "neutral")
        self.refresh_flow_tab()

    def _flow_classification_mode(self):
        return self.flow_classification_combo.currentData() or CLASSIFICATION_INDUSTRY

    def _flow_classification_scope(self):
        if self._flow_classification_mode() != CLASSIFICATION_VALUE_CHAIN_LEAF:
            return None
        return self.flow_classification_scope_combo.currentData()

    def _on_flow_classification_changed(self, _index):
        show_scope = self._flow_classification_mode() == CLASSIFICATION_VALUE_CHAIN_LEAF
        self.flow_classification_scope_label.setVisible(show_scope)
        self.flow_classification_scope_combo.setVisible(show_scope)
        self.refresh_flow_tab()

    def _open_flow_date_picker(self):
        end_date = max((p.date for p in self.snapshot.values() if p.date), default=date.today().isoformat())
        valid_dates = sorted(set(d for d in get_available_dates(app_paths.HISTORY_DB_PATH) if d <= end_date)
                             | {p.date for p in self.snapshot.values() if p.date and p.date <= end_date})
        dialog = TradingDateDialog(self, valid_dates, "選擇流向區間起始日期")
        if dialog.exec() == QtWidgets.QDialog.Accepted and dialog.selected_date:
            days = max(1, sum(d >= dialog.selected_date for d in valid_dates))
            self.flow_period = FlowPeriod(dialog.selected_date, days)
            self._update_flow_date_button()
            self.refresh_flow_tab()

    def _refresh_flow_on_revision(self):
        if get_data_revision(app_paths.HISTORY_DB_PATH) != getattr(self, "_flow_data_revision", None):
            self.refresh_flow_tab()

    def refresh_flow_tab(self):
        """啟動（或排入佇列）一次背景資金流向分析；實際套用結果見
        _apply_flow_dashboard。呼叫這個方法本身不會卡住主執行緒。"""
        self._flow_refresh_pending_request = (
            self.snapshot,
            self.flow_period,
            self.flow_bubble_mode_combo.currentData(),
            self.flow_valuation_metric_combo.currentData(),
            self._flow_classification_mode(),
            self._flow_classification_scope(),
        )
        if self._flow_refresh_in_flight:
            return
        self._start_flow_refresh()

    def _start_flow_refresh(self):
        (
            snapshot,
            period,
            bubble_mode,
            valuation_metric,
            classification_mode,
            classification_scope,
        ) = self._flow_refresh_pending_request
        self._flow_refresh_pending_request = None
        self._flow_refresh_in_flight = True
        self._flow_data_revision = get_data_revision(app_paths.HISTORY_DB_PATH)

        def work(_cancel_event, _emit_progress):
            return self.flow_service.analyze(
                snapshot,
                period,
                bubble_mode=bubble_mode,
                valuation_metric=valuation_metric,
                classification_mode=classification_mode,
                classification_scope=classification_scope,
            )

        def on_done(dashboard):
            self._apply_flow_dashboard(dashboard, classification_mode, bubble_mode, valuation_metric)
            self._flow_refresh_in_flight = False
            if self._flow_refresh_pending_request is not None:
                self._start_flow_refresh()

        def on_error(message):
            self._flow_refresh_in_flight = False
            QtWidgets.QMessageBox.warning(self, "錯誤", f"資金流向分析失敗：{message}")
            if self._flow_refresh_pending_request is not None:
                self._start_flow_refresh()

        # 見 _load_stock_trend_badges 同樣的註解：一定要留住回傳值，否則
        # BackgroundTask 可能在背景執行緒做完之前就被回收，callback 不會被呼叫。
        self._flow_refresh_task = run_background_task(self, work, on_done, on_error)

    def _apply_flow_dashboard(self, dashboard, classification_mode, bubble_mode, valuation_metric):
        classification_label = CLASSIFICATION_LABELS[classification_mode]
        overlapping_groups = classification_mode != CLASSIFICATION_INDUSTRY
        period = dashboard.period
        self._display_flow_period = period
        self._update_flow_date_button()
        data_date = period.end_date or "資料日期未明"
        self.flow_chart_header.setText(
            f"{classification_label}資金流向圖　·　{data_date}"
        )
        if bubble_mode == "institutional_sync":
            self.flow_chart_hint.setText(
                "泡泡填色＝所屬分類（對照下方清單色塊）；外框顏色＝三大法人同步方向"
                "（見上方圖例）。僅反映最新一筆三大法人資料，不受「流向區間」影響；"
                "滑鼠移至泡泡可看外資／投信／自營商個別金額。"
            )
        elif overlapping_groups:
            self.flow_chart_hint.setText(
                "泡泡填色＝所屬分類（對照下方清單色塊）；外框顏色＝當日量價方向"
                "（見上方圖例）；泡泡大小為成交涵蓋率。概念可重疊，各群組合計可能"
                "超過 100%。先看淡色象限判斷區間狀態，滑鼠移至泡泡可看完整數值。"
            )
        else:
            self.flow_chart_hint.setText(
                "泡泡填色＝所屬分類（對照下方清單色塊）；外框顏色＝當日量價方向"
                "（見上方圖例）；泡泡大小為成交佔比。先看淡色象限判斷區間狀態，"
                "滑鼠移至泡泡可看完整數值與象限解讀。"
            )
        if bubble_mode != "institutional_sync" and not period.sufficient:
            self.flow_chart_hint.setText(self.flow_chart_hint.text() +
                f" 實際有 {len(period.actual_dates)}／需要 {period.trading_days} 個交易日。")
        stale_count = sum(p.date != period.end_date for p in self.snapshot.values())
        if stale_count:
            self.flow_chart_hint.setText(self.flow_chart_hint.text() +
                f" {stale_count} 檔來源日期與截止日不同，未混用其價格計算。")
        populate_flow_chart(
            self.flow_chart,
            app_paths.HISTORY_DB_PATH,
            self.snapshot,
            avg_days=period.trading_days,
            on_industry_click=self._on_industry_bubble_clicked,
            mode=bubble_mode,
            valuation_metric=valuation_metric,
            dashboard=dashboard,
            classification_label=classification_label,
            overlapping_groups=overlapping_groups,
        )
        self._refresh_flow_summary(dashboard)
        self._refresh_institutional_direction(dashboard)
        self.refresh_flow_list(dashboard)
        self.refresh_stock_capital_flow_list(dashboard)
        self.refresh_volume_outliers_list(dashboard)

    def _refresh_flow_summary(self, dashboard):
        """用既有 dashboard 更新三張摘要卡，不觸發額外 DB 或網路工作。"""
        rows = [
            row
            for row in dashboard.industry_flow
            if row.daily_change_pct is not None
        ]
        inflow = [row for row in rows if row.daily_change_pct >= 0.05]
        outflow = [row for row in rows if row.daily_change_pct <= -0.05]
        active = max(
            (row for row in rows if row.turnover_ratio is not None),
            key=lambda row: row.turnover_ratio,
            default=None,
        )
        self.flow_summary_cards[0].set_value(f"{len(inflow)} 個族群", status="positive")
        self.flow_summary_cards[1].set_value(f"{len(outflow)} 個族群", status="negative")
        if active is None:
            self.flow_summary_cards[2].set_value("—")
        else:
            self.flow_summary_cards[2].set_value(
                f"{active.industry}　{active.turnover_ratio:.2f}×"
            )
        headline, detail, tone = _flow_momentum_insight(rows)
        self.flow_insight_card.set_content(headline, detail=detail, tone=tone)

    def _show_industry_top_stocks(self, industry):
        period = self.flow_period
        top_stocks = self.flow_service.get_group_top_stocks(
            self.snapshot,
            period,
            industry,
            classification_mode=self._flow_classification_mode(),
            classification_scope=self._flow_classification_scope(),
            top_n=30,
        )
        IndustryTopStocksDialog(self, industry, top_stocks, days=period.trading_days).exec()

    def _on_industry_bubble_clicked(self, industry):
        # 泡泡圖本身就是共用流向期間的視覺化，點擊彈出的成分股
        # 清單也該用同一個區間的累積成交金額，跟泡泡代表的資料口徑一致。
        self._show_industry_top_stocks(industry)

    def _on_flow_list_item_clicked(self, industry):
        # 所有資金流向小分類共用最上方的流向區間，避免泡泡圖、排行與成分股
        # 彈窗使用不同的日期口徑。
        self._show_industry_top_stocks(industry)

    # ---------- 資金動向清單（與泡泡圖共用最上方的流向區間） ----------

    def _build_flow_list_section(self, layout):
        header = QtWidgets.QLabel("資金動向清單")
        header.setProperty("header", True)
        layout.addWidget(header)
        layout.addSpacing(4)

        self.flow_list_hint = QtWidgets.QLabel(
            "資料區間與上方「流向區間」一致。分數為同批產業的"
            "相對百分位（0～100）：人氣＝今日資金比重、動能＝區間漲跌、量能＝量比；"
            "綜合熱度為三者平均，清單預設依綜合熱度排序。"
        )
        self.flow_list_hint.setProperty("muted", True)
        self.flow_list_hint.setWordWrap(True)
        layout.addWidget(self.flow_list_hint)
        layout.addSpacing(8)

        self.flow_list = QtWidgets.QTreeWidget()
        self.flow_list.setColumnCount(11)
        self.flow_list.setHeaderLabels(
            [
                "熱度排名",
                "分類",
                "檔數",
                "今日成交金額(億)",
                "今日資金比重%",
                "加權漲跌%",
                "量比",
                "人氣分數",
                "動能分數",
                "量能分數",
                "綜合熱度",
            ]
        )
        self.flow_list.setRootIsDecorated(False)
        self.flow_list.setAlternatingRowColors(True)
        # 由 refresh_flow_list 先排好綜合熱度；避免 Windows Qt 在建立格式化
        # 數值項目時再次觸發原生排序，這在部分環境會造成程序中止。
        self.flow_list.setSortingEnabled(False)
        self.flow_list.setMinimumHeight(240)
        self.flow_list.itemClicked.connect(
            lambda item, _col: self._on_flow_list_item_clicked(
                item.data(0, QtCore.Qt.UserRole)
            )
        )
        layout.addWidget(self.flow_list)

    def refresh_flow_list(self, dashboard=None):
        if dashboard is None:
            dashboard = self.flow_service.analyze(self.snapshot, self.flow_period)
        flow = dashboard.industry_flow
        overlapping_groups = dashboard.classification_mode != CLASSIFICATION_INDUSTRY
        classification_label = CLASSIFICATION_LABELS[dashboard.classification_mode]
        share_name = "成交涵蓋率" if overlapping_groups else "今日資金比重"
        self.flow_list.headerItem().setText(1, classification_label)
        self.flow_list.headerItem().setText(4, f"{share_name}%")
        overlap_note = (
            "概念可重疊，各群組涵蓋率合計可能超過 100%。"
            if overlapping_groups
            else ""
        )
        self.flow_list_hint.setText(
            f"資料區間與上方「流向區間」一致。{overlap_note}"
            f"分數為同批{classification_label}的相對百分位（0～100）："
            f"人氣＝{share_name}、動能＝區間漲跌、量能＝量比；"
            "綜合熱度為三者平均，清單預設依綜合熱度排序。"
        )
        # 進入畫面時直接把最能同時代表人氣／動能／量能的族群排在前面。
        flow = sorted(
            flow,
            key=lambda result: (
                result.composite_score is not None,
                result.composite_score if result.composite_score is not None else -1,
            ),
            reverse=True,
        )
        self.flow_list.clear()
        ranked_count = 0
        for f in flow:
            if f.avg_change_pct is None:
                change_text = f"實際有 {f.actual_days}／需要 {f.required_days} 個交易日（價格不足）"
            else:
                change_text = f"{f.avg_change_pct:+.2f}%"
            volume_text = f"{f.volume_ratio:.2f}" if f.volume_ratio is not None else f"實際有 {f.actual_volume_days}／需要 {f.required_days} 個歷史交易日（量比不足）"
            score_texts = [
                f"{score:.0f}" if score is not None else "資料不足"
                for score in (
                    f.popularity_score,
                    f.momentum_score,
                    f.volume_score,
                    f.composite_score,
                )
            ]
            if f.composite_score is not None:
                ranked_count += 1
                rank_text = str(ranked_count)
            else:
                rank_text = "-"

            item = QtWidgets.QTreeWidgetItem(
                [
                    rank_text,
                    f.industry,
                    str(f.stock_count),
                    f"{f.total_trading_value / 1e8:,.1f}",
                    f"{f.capital_share_pct:.2f}%",
                    change_text,
                    volume_text,
                    *score_texts,
                ]
            )
            item.setData(0, QtCore.Qt.UserRole, f.industry)
            # 分類欄加色塊 icon，跟泡泡圖共用 _category_color()，讓清單列跟
            # 泡泡圖的顏色可以直接對照，不新增欄位（避免牽動既有欄位索引）。
            item.setIcon(1, _color_swatch_icon(_category_color(f.industry)))
            for col in (0, 2, 3, 4, 5, 6, 7, 8, 9, 10):
                item.setTextAlignment(col, QtCore.Qt.AlignCenter)
            if f.avg_change_pct is not None:
                color = gain_loss_color(f.avg_change_pct)
                item.setForeground(5, QtGui.QColor(color))
            else:
                item.setForeground(5, QtGui.QColor(COLOR_MUTED))
                item.setForeground(6, QtGui.QColor(COLOR_MUTED))
            if f.composite_score is not None:
                item.setForeground(10, QtGui.QColor(COLOR_ACCENT))
            else:
                for col in (7, 8, 9, 10):
                    item.setForeground(col, QtGui.QColor(COLOR_MUTED))
            self.flow_list.addTopLevelItem(item)
        # 由 refresh_flow_list 先排好綜合熱度；避免 Windows Qt 在建立格式化
        # 數值項目時再次觸發原生排序，這在部分環境會造成程序中止。
        for col in range(self.flow_list.columnCount()):
            self.flow_list.resizeColumnToContents(col)

    # ---------- 個股資金流入前50名（以區間累積成交金額估算） ----------

    def _build_stock_capital_flow_section(self, layout):
        header = QtWidgets.QLabel("個股資金流入前 50 名")
        header.setProperty("header", True)
        layout.addWidget(header)
        layout.addSpacing(4)

        hint = QtWidgets.QLabel(
            "依上方資金動向清單的資料區間排序；「資金流入」以區間累積成交金額估算，"
            "不代表法人淨買超。雙擊股票可查看完整個股資訊。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addSpacing(8)

        self.stock_capital_flow_list = QtWidgets.QTreeWidget()
        self.stock_capital_flow_list.setColumnCount(9)
        self.stock_capital_flow_list.setHeaderLabels(
            [
                "排名",
                "代號",
                "名稱",
                "產業",
                "區間成交金額(億)",
                "今日成交金額(億)",
                "區間漲跌%",
                "量比",
                "全市場比重%",
            ]
        )
        self.stock_capital_flow_list.setRootIsDecorated(False)
        self.stock_capital_flow_list.setAlternatingRowColors(True)
        self.stock_capital_flow_list.setSortingEnabled(False)
        self.stock_capital_flow_list.setMinimumHeight(240)
        self.stock_capital_flow_list.itemDoubleClicked.connect(
            self._on_stock_capital_flow_double_clicked
        )
        layout.addWidget(self.stock_capital_flow_list)

    def refresh_stock_capital_flow_list(self, dashboard=None):
        if dashboard is None:
            dashboard = self.flow_service.analyze(self.snapshot, self.flow_period)
        stocks = dashboard.stock_capital_flow

        self.stock_capital_flow_list.setSortingEnabled(False)
        self.stock_capital_flow_list.clear()
        for rank, stock in enumerate(stocks, start=1):
            change_text = (
                f"{stock.change_pct:+.2f}%" if stock.change_pct is not None else f"實際有 {stock.actual_days}／需要 {stock.required_days} 個交易日（價格不足）"
            )
            volume_ratio_text = (
                f"{stock.volume_ratio:.2f}" if stock.volume_ratio is not None else "-"
            )
            item = QtWidgets.QTreeWidgetItem(
                [
                    str(rank),
                    stock.ticker,
                    stock.name or "-",
                    stock.industry,
                    f"{stock.trading_value / 1e8:,.2f}",
                    f"{stock.today_trading_value / 1e8:,.2f}",
                    change_text,
                    volume_ratio_text,
                    f"{stock.capital_share_pct:.2f}%",
                ]
            )
            item.setData(0, QtCore.Qt.UserRole, stock.ticker)
            item.setData(0, QtCore.Qt.UserRole + 1, stock.market)
            for col in (0, 1, 3, 4, 5, 6, 7, 8):
                item.setTextAlignment(col, QtCore.Qt.AlignCenter)
            if stock.change_pct is not None:
                color = gain_loss_color(stock.change_pct)
                item.setForeground(6, QtGui.QColor(color))
            self.stock_capital_flow_list.addTopLevelItem(item)

        for col in range(self.stock_capital_flow_list.columnCount()):
            self.stock_capital_flow_list.resizeColumnToContents(col)

    def _on_stock_capital_flow_double_clicked(self, item, _column):
        ticker = item.data(0, QtCore.Qt.UserRole)
        if not ticker:
            return
        name = item.text(2)
        market = item.data(0, QtCore.Qt.UserRole + 1)
        token = self.settings.get("finmind_token", "")
        StockDetailDialog(self, ticker, name, token, market=market).exec()

    # ---------- 個股量比異常清單（今日量 ÷ 近N日均量，逐檔股票各自比較自己的
    # 歷史均量，抓「個股」層級的爆量，不像資金動向清單是整個產業加總後的量比，
    # 單一檔股票爆量會被同產業其他股票稀釋掉） ----------

    def _build_volume_outliers_section(self, layout):
        header = QtWidgets.QLabel("個股量比異常清單")
        header.setProperty("header", True)
        layout.addWidget(header)
        layout.addSpacing(4)

        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("級距："))
        self.outlier_tier_combo = QtWidgets.QComboBox()
        self.outlier_tier_combo.addItem("全部", None)
        for tier in VOLUME_RATIO_TIERS:
            self.outlier_tier_combo.addItem(tier, tier)
        self.outlier_tier_combo.currentIndexChanged.connect(self._on_outlier_tier_filter_changed)
        controls.addWidget(self.outlier_tier_combo)
        controls.addStretch(1)
        layout.addLayout(controls)

        hint = QtWidgets.QLabel(
            "均量天數與上方「流向區間」一致；今日成交量 ÷ 近N日均量 ≥ 1.5 倍的個股，"
            "依比值分成 1.5～2倍／2～3倍／3倍以上；"
            "雙擊股票查詢本益比／殖利率／三大法人買賣超（同「個股」分頁，走 FinMind）。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addSpacing(8)

        self.outlier_tier_filter = None
        self.volume_outliers_list = QtWidgets.QTreeWidget()
        self.volume_outliers_list.setColumnCount(8)
        self.volume_outliers_list.setHeaderLabels(
            ["級距", "代號", "名稱", "族群", "現價", "漲跌%", "量比", "今日量(張)"]
        )
        self.volume_outliers_list.setRootIsDecorated(False)
        self.volume_outliers_list.setAlternatingRowColors(True)
        self.volume_outliers_list.setSortingEnabled(True)
        self.volume_outliers_list.setMinimumHeight(240)
        self.volume_outliers_list.itemDoubleClicked.connect(
            self._on_volume_outlier_double_clicked
        )
        layout.addWidget(self.volume_outliers_list)

    def _on_outlier_tier_filter_changed(self, _index):
        self.outlier_tier_filter = self.outlier_tier_combo.currentData()
        self.refresh_volume_outliers_list()

    def refresh_volume_outliers_list(self, dashboard=None):
        if dashboard is None:
            dashboard = self.flow_service.analyze(self.snapshot, self.flow_period)
        outliers = dashboard.volume_outliers
        if self.outlier_tier_filter:
            outliers = [o for o in outliers if o.tier == self.outlier_tier_filter]

        tier_colors = {
            "3倍以上": COLOR_LOSS,
            "2～3倍": COLOR_ACCENT,
            "1.5～2倍": COLOR_MUTED,
        }

        self.volume_outliers_list.setSortingEnabled(False)
        self.volume_outliers_list.clear()
        for o in outliers:
            change_text = f"{o.change_pct:+.2f}%" if o.change_pct is not None else "-"
            item = QtWidgets.QTreeWidgetItem(
                [
                    o.tier,
                    o.ticker,
                    o.name,
                    o.industry,
                    f"{o.close:.2f}" if o.close is not None else "-",
                    change_text,
                    f"{o.volume_ratio:.2f}",
                    f"{o.today_volume / 1000:,.0f}",
                ]
            )
            item.setData(0, QtCore.Qt.UserRole, o.ticker)
            item.setData(0, QtCore.Qt.UserRole + 1, o.market)
            for col in (1, 2, 3, 4, 5, 6, 7):
                item.setTextAlignment(col, QtCore.Qt.AlignCenter)
            item.setForeground(0, QtGui.QColor(tier_colors[o.tier]))
            if o.change_pct is not None:
                color = gain_loss_color(o.change_pct)
                item.setForeground(5, QtGui.QColor(color))
            self.volume_outliers_list.addTopLevelItem(item)
        self.volume_outliers_list.setSortingEnabled(True)
        self.volume_outliers_list.sortByColumn(6, QtCore.Qt.DescendingOrder)
        for col in range(self.volume_outliers_list.columnCount()):
            self.volume_outliers_list.resizeColumnToContents(col)

    def _on_volume_outlier_double_clicked(self, item, _column):
        ticker = item.data(0, QtCore.Qt.UserRole)
        if not ticker:
            return
        name = item.text(2)
        market = item.data(0, QtCore.Qt.UserRole + 1)
        token = self.settings.get("finmind_token", "")
        StockDetailDialog(self, ticker, name, token, market=market).exec()
