"""「部位紀錄」分頁（部位表格、新增／新增成交／編輯設定／收盤／取消、FinMind
詳細資訊區塊、交易設定與日誌）。

以 mixin 形式併入 tradingnote_gui.TradingNoteWindow；方法直接使用視窗的 self 狀態。

「部位」＝目前 OPEN 的交易（tradingnote_tradejournal.py 的 trades 資料表）；
開倉後股數／均價只能透過「新增成交」調整，不會再回頭編輯——這是舊版
positions.json／Position 跟新版 trades 合併後最大的語意差異，詳見
tradingnote_tradejournal.update_position_plan 的說明。"""

from datetime import date

from PySide6 import QtCore, QtGui, QtWidgets

from tradingnote_core import compute_pnl, find_position, lookup_price
from tradingnote_finmind import fetch_position_detail, load_position_detail_cache, save_position_detail_cache
from tradingnote_history import get_industry_map
from tradingnote_http import PriceFetchError
from tradingnote_journal import save_portfolio_snapshot
from tradingnote_tradejournal import (
    attach_tag,
    cancel_empty_position,
    close_trade,
    create_tag,
    detach_tag,
    get_trade,
    list_strategies,
    list_tags,
    list_trades,
    open_position,
    record_execution,
    trade_as_position,
    update_position_plan,
    update_trade_notes,
    upsert_trade_analytics,
)

from ui import app_paths
from ui.charts.detail import DetailChartPanel, _detail_summary_scroll, _render_detail_summary
from ui.dialogs.positions import (
    ExecutionFormDialog,
    PositionFormDialog,
    PositionPlanFormDialog,
    PriceLookupDialog,
)
from ui.format import _format_fetched_at, gain_loss_color
from ui.widgets import _clear_layout, _set_standard_icon, accent_button
from ui.workers import run_task_in_thread


COLUMNS = [
    ("id", "ID", 90),
    ("ticker", "代號", 60),
    ("name", "名稱", 100),
    ("industry", "族群", 90),
    ("concepts", "概念股", 160),
    ("shares", "股數", 70),
    ("entry_price", "成本價", 80),
    ("current_price", "現價", 80),
    ("equity_cost", "權益成本", 90),
    ("equity_value", "權益現值", 90),
    ("pnl", "損益", 90),
    ("pnl_pct", "損益%", 80),
    ("strategy", "策略", 90),
    ("status", "狀態", 60),
    ("note", "備註", 200),
    ("updated_at", "開倉日期", 90),
]

_EXECUTION_COLUMNS = ["買賣", "價格", "數量", "手續費", "滑價", "成交時間"]

_RATING_CHOICES = [("(未評分)", None), ("1", 1), ("2", 2), ("3", 3), ("4", 4), ("5", 5)]

_ANALYTICS_FIELDS = [
    ("mae_price", "MAE 價格"),
    ("mfe_price", "MFE 價格"),
    ("mae_amount", "最深浮虧金額"),
    ("mfe_amount", "最高浮盈金額"),
    ("mae_r", "MAE (R)"),
    ("mfe_r", "MFE (R)"),
    ("realized_r", "最終 R 值"),
    ("holding_duration_seconds", "持倉秒數"),
]

_TAG_CATEGORIES = [
    ("EMOTION", "心理"),
    ("EXECUTION", "執行"),
    ("MARKET_CONDITION", "市場狀態"),
]


class PositionsTabMixin:
    """TradingNoteWindow 的「部位紀錄」分頁方法。"""

    # ---------- 部位紀錄頁 ----------

    def _build_positions_tab(self):
        layout = QtWidgets.QVBoxLayout(self.positions_tab)
        layout.setContentsMargins(10, 10, 10, 10)

        self.position_plan_dirty = False
        self._position_plan_loading = False
        self.position_tag_buttons = {}
        self._position_trades = {}
        self.position_plan_selected_id = None

        toolbar = QtWidgets.QHBoxLayout()
        add_button = accent_button("新增部位", self.open_add_dialog)
        _set_standard_icon(add_button, QtWidgets.QStyle.SP_FileDialogNewFolder, "新增部位")
        toolbar.addWidget(add_button)
        add_execution_button = QtWidgets.QPushButton(
            "新增成交", clicked=self.open_add_execution_dialog
        )
        _set_standard_icon(add_execution_button, QtWidgets.QStyle.SP_ArrowUp, "新增成交（買進／賣出）")
        toolbar.addWidget(add_execution_button)
        edit_plan_button = QtWidgets.QPushButton("編輯設定", clicked=self.open_edit_plan_dialog)
        _set_standard_icon(edit_plan_button, QtWidgets.QStyle.SP_FileDialogDetailedView, "編輯策略／停損／停利／風險金額")
        toolbar.addWidget(edit_plan_button)
        close_button = QtWidgets.QPushButton("收盤", clicked=self.close_selected_position)
        _set_standard_icon(close_button, QtWidgets.QStyle.SP_DialogApplyButton, "將選取的部位設為已收盤")
        toolbar.addWidget(close_button)
        cancel_button = QtWidgets.QPushButton("取消交易", clicked=self.cancel_selected_position)
        _set_standard_icon(cancel_button, QtWidgets.QStyle.SP_DialogCancelButton, "取消選取、尚無成交記錄的部位")
        toolbar.addWidget(cancel_button)
        price_button = QtWidgets.QPushButton("查價", clicked=self.open_price_dialog)
        _set_standard_icon(price_button, QtWidgets.QStyle.SP_FileDialogInfoView, "查詢股價")
        toolbar.addWidget(price_button)
        toolbar.addStretch(1)
        self.position_history_checkbox = QtWidgets.QCheckBox("顯示歷史（已收盤／已取消）")
        self.position_history_checkbox.stateChanged.connect(lambda _state: self.refresh_table())
        toolbar.addWidget(self.position_history_checkbox)
        layout.addLayout(toolbar)

        keys = [key for key, _, _ in COLUMNS]
        labels = [label for _, label, _ in COLUMNS]
        self.table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(labels)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        for i, (key, _, width) in enumerate(COLUMNS):
            self.table.setColumnWidth(i, width)
        self._column_keys = keys
        self.table.itemSelectionChanged.connect(self._on_position_row_selected)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(self._build_position_detail_section())
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([180, 540])
        layout.addWidget(splitter)

    def _build_position_detail_section(self):
        """選取上方某筆部位時顯示的詳細資訊：兩個子分頁——「籌碼/技術」是原本
        族群／概念股＋FinMind 本益比／殖利率／三大法人／融資融券等背景查詢
        （見 _load_position_detail）；「交易設定與日誌」是這筆交易的逐筆成交、
        標籤、筆記／執行評分／復盤診斷（手動輸入），後者採自動保存。"""
        self.position_sub_tabs = QtWidgets.QTabWidget()
        self.position_sub_tabs.addTab(self._build_chip_detail_section(), "籌碼/技術")
        self.position_sub_tabs.addTab(self._build_plan_detail_section(), "交易設定與日誌")
        return self.position_sub_tabs

    def _build_chip_detail_section(self):
        container = QtWidgets.QWidget()
        outer_layout = QtWidgets.QVBoxLayout(container)
        outer_layout.setContentsMargins(0, 10, 0, 0)

        header = QtWidgets.QLabel("個股詳細資訊")
        header.setToolTip("選取上方部位，查看歷史股價、法人、融資融券及技術指標。")
        header.setProperty("header", True)
        outer_layout.addWidget(header)
        outer_layout.addSpacing(4)

        self.position_detail_label = QtWidgets.QLabel("尚未選取部位。")
        self.position_detail_label.setWordWrap(True)
        outer_layout.addWidget(_detail_summary_scroll(self.position_detail_label))

        self.position_panel = DetailChartPanel()
        outer_layout.addWidget(self.position_panel.tabs, 1)

        return container

    def _build_plan_detail_section(self):
        panel = QtWidgets.QWidget()
        panel_layout = QtWidgets.QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 10, 0, 0)

        self.position_plan_title = QtWidgets.QLabel("請先在上方選取一筆部位。")
        self.position_plan_title.setProperty("header", True)
        self.position_plan_title.setWordWrap(True)
        panel_layout.addWidget(self.position_plan_title)

        detail_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)

        # ---- 左側：逐筆成交明細＋標籤 ----
        executions_panel = QtWidgets.QWidget()
        executions_layout = QtWidgets.QVBoxLayout(executions_panel)
        executions_layout.setContentsMargins(0, 0, 6, 0)
        executions_layout.addWidget(QtWidgets.QLabel("逐筆成交"))
        self.position_execution_table = QtWidgets.QTableWidget(0, len(_EXECUTION_COLUMNS))
        self.position_execution_table.setHorizontalHeaderLabels(_EXECUTION_COLUMNS)
        self.position_execution_table.verticalHeader().setVisible(False)
        self.position_execution_table.setAlternatingRowColors(True)
        self.position_execution_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        executions_layout.addWidget(self.position_execution_table, 1)

        self.position_tags_group = QtWidgets.QGroupBox("標籤")
        tags_group_layout = QtWidgets.QVBoxLayout(self.position_tags_group)
        add_tag_row = QtWidgets.QHBoxLayout()
        add_tag_row.addStretch(1)
        add_tag_button = QtWidgets.QPushButton("+ 新增標籤", clicked=self._add_position_tag)
        add_tag_row.addWidget(add_tag_button)
        tags_group_layout.addLayout(add_tag_row)
        self.position_tags_layout = QtWidgets.QVBoxLayout()
        tags_group_layout.addLayout(self.position_tags_layout)
        executions_layout.addWidget(self.position_tags_group)

        # ---- 右側：筆記、執行評分、復盤診斷（手動輸入） ----
        editor_panel = QtWidgets.QWidget()
        editor_layout = QtWidgets.QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(6, 0, 0, 0)

        editor_layout.addWidget(QtWidgets.QLabel("交易筆記"))
        self.position_notes_edit = QtWidgets.QPlainTextEdit()
        self.position_notes_edit.setMaximumHeight(90)
        self.position_notes_edit.textChanged.connect(self._on_position_detail_field_changed)
        editor_layout.addWidget(self.position_notes_edit)

        rating_row = QtWidgets.QHBoxLayout()
        rating_row.addWidget(QtWidgets.QLabel("執行評分"))
        self.position_rating_combo = QtWidgets.QComboBox()
        for label, value in _RATING_CHOICES:
            self.position_rating_combo.addItem(label, value)
        self.position_rating_combo.currentIndexChanged.connect(self._on_position_detail_field_changed)
        rating_row.addWidget(self.position_rating_combo)
        rating_row.addStretch(1)
        editor_layout.addLayout(rating_row)

        editor_layout.addWidget(QtWidgets.QLabel("復盤診斷 (手動輸入)"))
        analytics_form = QtWidgets.QFormLayout()
        self.position_analytics_inputs = {}
        for key, label in _ANALYTICS_FIELDS:
            edit = QtWidgets.QLineEdit()
            edit.textChanged.connect(self._on_position_detail_field_changed)
            analytics_form.addRow(label, edit)
            self.position_analytics_inputs[key] = edit
        editor_layout.addLayout(analytics_form)

        save_row = QtWidgets.QHBoxLayout()
        self.position_detail_save_status = QtWidgets.QLabel("")
        self.position_detail_save_status.setProperty("muted", True)
        save_row.addWidget(self.position_detail_save_status)
        save_row.addStretch(1)
        save_button = accent_button("儲存筆記與診斷", self._save_position_detail_clicked)
        save_row.addWidget(save_button)
        editor_layout.addLayout(save_row)
        editor_layout.addStretch(1)

        detail_splitter.addWidget(executions_panel)
        detail_splitter.addWidget(editor_panel)
        detail_splitter.setStretchFactor(0, 3)
        detail_splitter.setStretchFactor(1, 2)
        panel_layout.addWidget(detail_splitter, 1)
        return panel

    def refresh_table(self):
        statuses = ("OPEN", "CLOSED", "CANCELLED") if self.position_history_checkbox.isChecked() else ("OPEN",)
        trades = []
        for status in statuses:
            trades.extend(list_trades(app_paths.HISTORY_DB_PATH, status=status))
        trades.sort(key=lambda t: t.opened_at, reverse=True)

        strategy_names = {s.strategy_id: s.name for s in list_strategies(app_paths.HISTORY_DB_PATH)}
        self._position_trades = {str(t.trade_id): t for t in trades}
        self.positions = [trade_as_position(t, self.snapshot) for t in trades]

        total_pnl = 0.0
        has_pnl = False
        self.table.setRowCount(len(self.positions))
        try:
            industry_map = get_industry_map(app_paths.HISTORY_DB_PATH)
        except PriceFetchError:
            # 族群只是輔助資訊，抓不到（例如離線、快取剛好過期又連不上網）不該
            # 讓整個部位表格連損益都顯示不出來，各列的「族群」直接落回「-」。
            industry_map = {}

        for row_index, (pos, trade) in enumerate(zip(self.positions, trades)):
            concepts = self._ticker_concept_map.get(pos.ticker) or []

            if trade.status == "OPEN":
                price = lookup_price(pos.ticker, self.snapshot)
                pnl = compute_pnl(pos, price)
                if price is not None and price.name and price.name != pos.name:
                    pos.name = price.name
                    pos.market = price.market
                equity_cost = pos.shares * pos.entry_price
                if pnl is None:
                    current_price, pnl_amt, pnl_pct = "N/A", "N/A", "N/A"
                    equity_value = "N/A"
                    pnl_color = pnl_pct_color = None
                else:
                    current_price = f"{pnl.current_price:.2f}"
                    pnl_amt = f"{pnl.unrealized_pnl:+.0f}"
                    pnl_pct = f"{pnl.pnl_pct:+.2f}%"
                    equity_value = f"{pnl.current_value:,.0f}"
                    pnl_color = pnl_pct_color = gain_loss_color(pnl.unrealized_pnl)
                    total_pnl += pnl.unrealized_pnl
                    has_pnl = True
            else:
                # 已收盤／已取消：不再即時估價，損益欄改顯示交易的已實現淨損益。
                equity_cost = pos.shares * pos.entry_price
                current_price, equity_value, pnl_pct = "-", "-", "-"
                pnl_amt = f"{trade.net_pnl:+,.0f}"
                pnl_color = gain_loss_color(trade.net_pnl)
                pnl_pct_color = None

            values = {
                "id": pos.id,
                "ticker": pos.ticker,
                "name": pos.name or "-",
                "industry": industry_map.get(pos.ticker) or "-",
                "concepts": "、".join(concepts) if concepts else "-",
                "shares": str(pos.shares),
                "entry_price": f"{pos.entry_price:.2f}",
                "current_price": current_price,
                "equity_cost": f"{equity_cost:,.0f}",
                "equity_value": equity_value,
                "pnl": pnl_amt,
                "pnl_pct": pnl_pct,
                "strategy": strategy_names.get(trade.strategy_id, "-"),
                "status": trade.status,
                "note": pos.note,
                "updated_at": pos.updated_at or "-",
            }
            colors = {"pnl": pnl_color, "pnl_pct": pnl_pct_color}
            for col, key in enumerate(self._column_keys):
                item = QtWidgets.QTableWidgetItem(values[key])
                if key in ("id", "ticker"):
                    item.setTextAlignment(QtCore.Qt.AlignCenter)
                if colors.get(key):
                    item.setForeground(QtGui.QColor(colors[key]))
                self.table.setItem(row_index, col, item)

        # 每日持股快照一律只收 OPEN 的部位，歷史勾選框不影響交易週誌的每日快照。
        open_positions_for_snapshot = [
            trade_as_position(t, self.snapshot) for t in trades if t.status == "OPEN"
        ]
        save_portfolio_snapshot(
            app_paths.HISTORY_DB_PATH, date.today().isoformat(), open_positions_for_snapshot
        )
        if hasattr(self, "journal_cards"):
            self._save_journal_if_dirty()
            self._refresh_journal_week()
        self._total_pnl = total_pnl if has_pnl else None
        self._update_status_bar()

    # ---------- 部位詳細資訊區塊：族群／概念股（本地）＋ FinMind 基本面／
    # 三大法人120日趨勢（背景查詢，見 tradingnote_finmind.py） ----------

    def _on_position_row_selected(self):
        self._save_position_detail_if_dirty()
        position_id = self._selected_position_id()
        if position_id is None:
            self.position_detail_label.setText("尚未選取部位。")
            self.position_panel.clear()
            self._render_position_plan_detail(None)
            return
        pos = find_position(self.positions, position_id)
        if pos is None:
            return
        self._load_position_detail(pos)
        self._render_position_plan_detail(position_id)

    def _load_position_detail(self, pos):
        try:
            industry = get_industry_map(app_paths.HISTORY_DB_PATH).get(pos.ticker) or "未分類"
        except PriceFetchError:
            industry = "未分類"
        concepts = self._ticker_concept_map.get(pos.ticker) or []
        concept_text = "、".join(concepts) if concepts else "（尚無分類，可編輯 concepts.json 新增）"
        header = f"{pos.ticker} {pos.name or ''}　族群：{industry}　概念股：{concept_text}"

        # 先看 position_detail_cache.json 有沒有這檔股票上次查到、永久存下來的
        # 結果：有就先顯示（不用等這次背景查詢），沒有才顯示「查詢中」空白狀態。
        # 不管有沒有快取，下面都照樣背景重打一次 FinMind 拿最新資料、查到就覆寫
        # 快取檔——快取讓「隨時可以取用」，不是拿來取代查新資料。
        cached = load_position_detail_cache(app_paths.POSITION_DETAIL_CACHE_PATH, pos.ticker)
        if cached is not None:
            note = f"（上次查詢：{_format_fetched_at(cached['fetched_at'])}，背景更新中...）"
            self._render_position_detail(header, cached, note)
        else:
            self.position_detail_label.setText(f"{header}\n\nFinMind 查詢中...")
            self.position_panel.clear()

        token = self.settings.get("finmind_token", "")
        market = pos.market

        def fetch():
            data = fetch_position_detail(pos.ticker, token, market=market)
            save_position_detail_cache(app_paths.POSITION_DETAIL_CACHE_PATH, pos.ticker, data)
            return data

        self._position_detail_timer = run_task_in_thread(
            self,
            fetch,
            lambda result: self._on_position_detail_done(pos, header, result),
            lambda message: self._on_position_detail_error(pos, header, message, cached),
        )

    def _is_current_detail_target(self, pos):
        return self._selected_position_id() == pos.id

    def _on_position_detail_done(self, pos, header, result):
        if not self._is_current_detail_target(pos):
            return  # 使用者查詢途中已切換到別筆部位，這份結果過期了，不套用
        self._render_position_detail(header, result)
        self.update_finmind_count_label()

    def _render_position_detail(self, header, data, note=None):
        """畫「部位詳細資訊」文字摘要（`_render_detail_summary`，跟「個股」頁
        StockDetailDialog 的「顯示完整籌碼面資訊」按鈕共用同一份）＋更新
        `self.position_panel` 的資料——分頁圖表延遲建立，見 DetailChartPanel。"""
        _render_detail_summary(self.position_detail_label, header, data, note)
        self.position_panel.set_data(data)

    def _on_position_detail_error(self, pos, header, message, cached=None):
        if not self._is_current_detail_target(pos):
            return
        # 有上次的快取結果就繼續顯示它（只是把「背景更新中」的提示換成「更新
        # 失敗」），不要因為這次背景重查失敗就把已經在畫面上的舊資料清空——
        # 這正是「另外存放，隨時可以取用」要解決的情境：沒 token／超額度／
        # 沒網路時，至少還能看上次查到的結果，不是一片空白。
        if cached is not None:
            note = (
                f"（背景更新失敗：{message}；顯示上次查詢結果 "
                f"{_format_fetched_at(cached['fetched_at'])}）"
            )
            self._render_position_detail(header, cached, note)
            self.update_finmind_count_label()
            return
        self.position_detail_label.setText(
            f"{header}\n\nFinMind 查詢失敗：{message}\n\n"
            "可能原因：FinMind token 未設定或已失效、已超過免費額度，或該股票暫無此資料。"
        )
        self.position_panel.clear()
        self.update_finmind_count_label()

    # ---------- 交易設定與日誌（逐筆成交／標籤／筆記／評分／復盤診斷） ----------

    def _render_position_plan_detail(self, position_id):
        # 存起來給 _save_position_detail_if_dirty／_toggle_position_tag 用，而不是
        # 讓它們臨時呼叫 _selected_position_id() 重讀表格——_on_position_row_selected
        # 呼叫 _save_position_detail_if_dirty() 的當下，表格選取已經換成「新」那一列，
        # 這時如果還去讀表格目前選的列，存檔就會寫錯筆（寫到新選的那筆，不是真正
        # 有未存變更的那筆）。
        self.position_plan_selected_id = position_id
        _clear_layout(self.position_tags_layout)
        self.position_tag_buttons = {}

        if position_id is None:
            self.position_plan_title.setText("請先在上方選取一筆部位。")
            self.position_execution_table.setRowCount(0)
            self._position_plan_loading = True
            self.position_notes_edit.setPlainText("")
            self.position_rating_combo.setCurrentIndex(0)
            for edit in self.position_analytics_inputs.values():
                edit.setText("")
            self._position_plan_loading = False
            self.position_plan_dirty = False
            self.position_detail_save_status.setText("")
            return

        trade_id = int(position_id)
        trade = get_trade(app_paths.HISTORY_DB_PATH, trade_id)
        if trade is None:
            self._render_position_plan_detail(None)
            return

        self.position_plan_title.setText(
            f"#{trade.trade_id}　{trade.symbol}　{trade.side}　{trade.status}"
            f"　目前數量 {trade.total_qty:g}　淨損益 {trade.net_pnl:+,.0f}"
        )

        self.position_execution_table.setRowCount(len(trade.executions))
        for row, execution in enumerate(trade.executions):
            values = (
                execution.action,
                f"{execution.price:.2f}",
                f"{execution.qty:g}",
                f"{execution.commission:.2f}",
                f"{execution.slippage:.2f}",
                execution.executed_at,
            )
            for column, value in enumerate(values):
                self.position_execution_table.setItem(row, column, QtWidgets.QTableWidgetItem(value))

        attached_tag_ids = {tag.tag_id for tag in trade.tags}
        for category, category_label in _TAG_CATEGORIES:
            category_tags = list_tags(app_paths.HISTORY_DB_PATH, category=category)
            if not category_tags:
                continue
            row_layout = QtWidgets.QHBoxLayout()
            row_layout.addWidget(QtWidgets.QLabel(category_label + ":"))
            for tag in category_tags:
                button = QtWidgets.QPushButton(tag.name)
                button.setCheckable(True)
                button.setChecked(tag.tag_id in attached_tag_ids)
                button.clicked.connect(
                    lambda checked, tag_id=tag.tag_id: self._toggle_position_tag(tag_id, checked)
                )
                self.position_tag_buttons[tag.tag_id] = button
                row_layout.addWidget(button)
            row_layout.addStretch(1)
            self.position_tags_layout.addLayout(row_layout)

        self._position_plan_loading = True
        self.position_notes_edit.setPlainText(trade.notes or "")
        rating_index = next(
            (i for i, (_label, value) in enumerate(_RATING_CHOICES)
             if value == trade.execution_rating),
            0,
        )
        self.position_rating_combo.setCurrentIndex(rating_index)
        analytics = trade.analytics
        for key, _label in _ANALYTICS_FIELDS:
            value = getattr(analytics, key, None) if analytics is not None else None
            self.position_analytics_inputs[key].setText("" if value is None else str(value))
        self._position_plan_loading = False
        self.position_plan_dirty = False
        self.position_detail_save_status.setText("已儲存")

    def _add_position_tag(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "新增標籤", "標籤名稱")
        if not ok or not name.strip():
            return
        category_labels = [label for _category, label in _TAG_CATEGORIES]
        category_label, ok = QtWidgets.QInputDialog.getItem(
            self, "新增標籤", "分類", category_labels, editable=False
        )
        if not ok:
            return
        category = next(
            category for category, label in _TAG_CATEGORIES if label == category_label
        )
        try:
            create_tag(app_paths.HISTORY_DB_PATH, name.strip(), category)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "錯誤", str(e))
            return
        self._render_position_plan_detail(self._selected_position_id())

    def _toggle_position_tag(self, tag_id, checked):
        position_id = self._selected_position_id()
        if position_id is None:
            return
        if checked:
            attach_tag(app_paths.HISTORY_DB_PATH, int(position_id), tag_id)
        else:
            detach_tag(app_paths.HISTORY_DB_PATH, int(position_id), tag_id)

    def _on_position_detail_field_changed(self, *_args):
        if self._position_plan_loading:
            return
        self.position_plan_dirty = True
        self.position_detail_save_status.setText("尚未儲存")

    def _save_position_detail_if_dirty(self):
        if not getattr(self, "position_plan_dirty", False):
            return
        position_id = getattr(self, "position_plan_selected_id", None)
        if position_id is None:
            return
        self._write_position_detail(int(position_id))
        self.position_plan_dirty = False
        self.position_detail_save_status.setText("已儲存")

    def _save_position_detail_clicked(self):
        position_id = getattr(self, "position_plan_selected_id", None)
        if position_id is None:
            return
        self._write_position_detail(int(position_id))
        self.position_plan_dirty = False
        self.position_detail_save_status.setText("已儲存")

    def _write_position_detail(self, trade_id):
        update_trade_notes(
            app_paths.HISTORY_DB_PATH,
            trade_id,
            notes=self.position_notes_edit.toPlainText() or None,
            execution_rating=self.position_rating_combo.currentData(),
        )
        analytics_values = {}
        for key, _label in _ANALYTICS_FIELDS:
            text = self.position_analytics_inputs[key].text().strip()
            if not text:
                analytics_values[key] = None
                continue
            try:
                analytics_values[key] = int(text) if key == "holding_duration_seconds" else float(text)
            except ValueError:
                analytics_values[key] = None
        upsert_trade_analytics(app_paths.HISTORY_DB_PATH, trade_id, **analytics_values)

    # ---------- 新增部位／新增成交／編輯設定／收盤／取消／查價 ----------

    def open_add_dialog(self):
        strategies = list_strategies(app_paths.HISTORY_DB_PATH)

        def on_submit(**kwargs):
            open_position(app_paths.HISTORY_DB_PATH, snapshot=self.snapshot, **kwargs)
            self.refresh_table()

        PositionFormDialog(self, on_submit, strategies, app_paths.HISTORY_DB_PATH).exec()

    def _selected_position_id(self):
        selected = self.table.selectionModel().selectedRows()
        if not selected:
            return None
        row = selected[0].row()
        return self.table.item(row, self._column_keys.index("id")).text()

    def _selected_open_trade(self, action_label):
        """新增成交／編輯設定／收盤／取消交易都只對目前 OPEN 的交易生效——
        _recalculate_rollup 只會把 OPEN 轉成 CLOSED，不會反向重新打開，對一筆
        已經 CLOSED／CANCELLED 的交易再寫入成交只會留下不一致的股數／狀態。"""
        position_id = self._selected_position_id()
        if position_id is None:
            QtWidgets.QMessageBox.information(self, "提示", f"請先選擇要{action_label}的部位。")
            return None
        trade = get_trade(app_paths.HISTORY_DB_PATH, int(position_id))
        if trade is None:
            QtWidgets.QMessageBox.warning(self, "錯誤", "找不到這筆部位，畫面可能已過期，請按重新整理。")
            return None
        if trade.status != "OPEN":
            QtWidgets.QMessageBox.information(
                self, "提示", f"這筆部位已經是「{trade.status}」狀態，無法{action_label}。"
            )
            return None
        return trade

    def open_add_execution_dialog(self):
        trade = self._selected_open_trade("新增成交")
        if trade is None:
            return

        def on_submit(**kwargs):
            record_execution(app_paths.HISTORY_DB_PATH, trade.trade_id, **kwargs)
            self.refresh_table()

        ExecutionFormDialog(self, on_submit).exec()

    def open_edit_plan_dialog(self):
        trade = self._selected_open_trade("編輯設定")
        if trade is None:
            return
        strategies = list_strategies(app_paths.HISTORY_DB_PATH)

        def on_submit(**kwargs):
            update_position_plan(app_paths.HISTORY_DB_PATH, trade.trade_id, **kwargs)
            self.refresh_table()

        PositionPlanFormDialog(self, on_submit, strategies, app_paths.HISTORY_DB_PATH, trade).exec()

    def close_selected_position(self):
        trade = self._selected_open_trade("收盤")
        if trade is None:
            return
        reply = QtWidgets.QMessageBox.question(
            self,
            "確認收盤",
            f"確定要將 {trade.symbol} 的這筆部位設為已收盤嗎？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        close_trade(app_paths.HISTORY_DB_PATH, trade.trade_id)
        self.refresh_table()

    def cancel_selected_position(self):
        trade = self._selected_open_trade("取消")
        if trade is None:
            return
        reply = QtWidgets.QMessageBox.question(
            self,
            "確認取消",
            f"確定要取消 {trade.symbol} 的這筆部位嗎？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return
        if not cancel_empty_position(app_paths.HISTORY_DB_PATH, trade.trade_id):
            QtWidgets.QMessageBox.information(
                self, "提示", "這筆部位已經有成交記錄，請改用「新增成交」把股數全部賣出。"
            )
            return
        self.refresh_table()

    def open_price_dialog(self):
        PriceLookupDialog(self, self.snapshot).exec()
