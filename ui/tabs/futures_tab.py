"""「期貨行情」分頁（TAIFEX 盤後行情＋大額交易人）。

以 mixin 形式併入 tradingnote_gui.TradingNoteWindow；方法直接使用視窗的 self 狀態。"""

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from tradingnote_http import PriceFetchError
from tradingnote_taifex import (
    DEFAULT_FUTURES_PRODUCTS,
    LARGE_TRADERS_HISTORY_ALL_CONTRACTS_MONTH,
    build_large_traders_name_map,
    build_ssf_map,
    get_cached_daily_futures_report,
    get_cached_large_traders_futures_report,
    get_cached_ssf_list,
    get_futures_snapshot,
    get_large_traders_history_series,
    group_large_traders_all,
    large_traders_code_for_product,
    list_all_products,
)
from tradingnote_tasks import run_background_task

from ui import app_paths
from ui.dialogs.futures import (
    FuturesLargeTradersDialog,
    LARGE_TRADERS_TABLE_COLUMNS,
    _large_traders_summary_entry,
    _populate_large_traders_table,
    _populate_large_traders_trend,
)
from ui.format import _futures_snapshot_date, gain_loss_color
from ui.theme import COLOR_GAIN, COLOR_LOSS, COLOR_SURFACE
from ui.workers import run_task_in_thread


class FuturesTabMixin:
    """TradingNoteWindow 的「期貨行情」分頁方法。"""

    # ---------- 期貨頁（TAIFEX 官方盤後行情） ----------
    # 原本串接 Fugle 的 data-futopt 即時行情，但查證 developer.fugle.tw 定價文件
    # 後發現期貨/選擇權完全不在免費方案內（連歷史/盤後資料都沒有，只有 intraday
    # 且需要付費 Developer 方案）。改用 TAIFEX 官方公開、免金鑰的「期貨每日交易
    # 行情」，只反映盤後（EOD）資訊，不是即時報價——跟 TWSE/TPEX 股票走 EOD 的既有
    # 原則一致，見 tradingnote_taifex.py。一天只更新一次，不需要背景輪詢計時器，
    # 隨「重新整理」（見 _apply_refresh_result）跟股票資料一起刷新即可。

    FUTURES_COLUMNS = [
        "商品", "標的", "契約月份", "時段", "最後成交", "漲跌", "漲跌%",
        "結算價", "成交量", "未沖銷契約數",
        "大額前10買", "大額前10賣", "大額前10淨", "大額買佔比",
    ]

    FUTURES_SESSION_ORDER = ["一般", "盤後"]

    def _build_futures_tab(self):
        layout = QtWidgets.QVBoxLayout(self.futures_tab)
        layout.setContentsMargins(10, 10, 10, 10)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(QtWidgets.QLabel("搜尋"))
        self.futures_search_edit = QtWidgets.QLineEdit()
        self.futures_search_edit.setPlaceholderText("輸入代號／名稱，如 TX／台積電／2330／布蘭特")
        self.futures_search_edit.setMinimumWidth(170)
        self.futures_search_edit.setMaximumWidth(320)
        self.futures_search_edit.addAction(
            self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogContentsView),
            QtWidgets.QLineEdit.LeadingPosition,
        )
        self.futures_search_edit.textChanged.connect(self._filter_futures_table)
        toolbar.addWidget(self.futures_search_edit)

        hint = QtWidgets.QLabel("盤後行情 · 點選商品查看大額交易人部位，雙擊查看完整明細。")
        hint.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        hint.setToolTip("可搜尋契約代碼、股票代號或名稱。大額前10欄位為近月所有交易人部位；一般為日盤，盤後為夜盤。資料每日更新，可至設定重新整理。")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        toolbar.addWidget(hint)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.futures_status_label = QtWidgets.QLabel("")
        self.futures_status_label.setProperty("muted", True)
        layout.addWidget(self.futures_status_label)

        self.futures_table = QtWidgets.QTableWidget()
        self.futures_table.setColumnCount(len(self.FUTURES_COLUMNS))
        self.futures_table.setHorizontalHeaderLabels(self.FUTURES_COLUMNS)
        self.futures_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.futures_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.futures_table.cellDoubleClicked.connect(self._on_futures_row_double_clicked)
        self.futures_table.itemSelectionChanged.connect(self._on_futures_row_selected)
        for col, width in enumerate(
            [70, 130, 90, 60, 90, 80, 70, 90, 90, 110, 95, 95, 95, 90]
        ):
            self.futures_table.setColumnWidth(col, width)

        # 表格（上）＋大額交易人未沖銷部位明細面板（下），用 QSplitter 分隔可調高度，
        # 跟「部位紀錄」頁「表格＋詳細區塊」的排版一致。
        splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        splitter.addWidget(self.futures_table)
        splitter.addWidget(self._build_futures_lt_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)

        self._futures_snapshot = {}
        self._futures_all_products = list(DEFAULT_FUTURES_PRODUCTS)
        self._futures_large_traders_rows = []
        self._futures_lt_by_code = {}
        self._futures_ssf_map = {}
        self._futures_name_map = {}
        # 大額交易人歷史趨勢圖查詢（SQLite）debounce＋背景執行緒狀態，見
        # _load_futures_trend；明細表格（_populate_large_traders_table）走的是
        # 重新整理時已快取的 self._futures_large_traders_rows，不查 DB，維持同步即可。
        self._futures_trend_timer = QtCore.QTimer(self)
        self._futures_trend_timer.setSingleShot(True)
        self._futures_trend_timer.timeout.connect(self._load_futures_trend)
        self._futures_trend_expected_code = None
        self._rebuild_futures_table()

    def _build_futures_lt_panel(self):
        """「期貨」頁表格下方的常駐明細面板：點選某商品即顯示該商品完整的大額
        交易人未沖銷部位（各到期月份 × 所有交易人／特定法人），跟雙擊彈窗
        （FuturesLargeTradersDialog）共用同一份填表邏輯 _populate_large_traders_table。"""
        container = QtWidgets.QWidget()
        panel_layout = QtWidgets.QVBoxLayout(container)
        panel_layout.setContentsMargins(0, 6, 0, 0)

        self.futures_lt_detail_label = QtWidgets.QLabel(
            "點選上方商品，這裡顯示其大額交易人未沖銷部位（各到期月份 × "
            "所有交易人／特定法人）。"
        )
        self.futures_lt_detail_label.setWordWrap(True)
        self.futures_lt_detail_label.setProperty("muted", True)
        panel_layout.addWidget(self.futures_lt_detail_label)

        self.futures_lt_detail_table = QtWidgets.QTableWidget(
            0, len(LARGE_TRADERS_TABLE_COLUMNS)
        )
        self.futures_lt_detail_table.setHorizontalHeaderLabels(LARGE_TRADERS_TABLE_COLUMNS)
        self.futures_lt_detail_table.verticalHeader().setVisible(False)
        self.futures_lt_detail_table.setAlternatingRowColors(True)
        self.futures_lt_detail_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.futures_lt_detail_table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)

        # 趨勢圖：選取商品的前10大買/賣方未沖銷部位逐日走勢（歷史由背景回補累積，
        # 見 _sync_large_traders_history）。放在明細表右邊，用 QSplitter 可調寬度。
        self.futures_lt_trend_chart = pg.PlotWidget()
        self.futures_lt_trend_chart.setBackground(COLOR_SURFACE)
        self.futures_lt_trend_chart.showGrid(x=True, y=True, alpha=0.08)
        self.futures_lt_trend_chart.addLegend()
        self.futures_lt_trend_chart.setMinimumHeight(200)

        inner = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        inner.addWidget(self.futures_lt_detail_table)
        inner.addWidget(self.futures_lt_trend_chart)
        inner.setStretchFactor(0, 3)
        inner.setStretchFactor(1, 2)
        panel_layout.addWidget(inner)
        return container

    def _futures_display_name(self, product):
        """商品在「期貨」頁顯示用的名稱：股票期貨用「股票簡稱 代號」（台積電 2330），
        其餘（指數／商品期貨）用大額端點的中文契約名（布蘭特原油期貨），都沒有則空字串。"""
        ssf = self._futures_ssf_map.get(product)
        if ssf:
            return f"{ssf['stock_name'] or ''} {ssf['stock_code'] or ''}".strip()
        return self._futures_name_map.get(product) or ""

    def _futures_groups_for_product(self, product):
        """回傳某日盤商品在大額端點對應的分組（自動換算去尾 F 的代碼）；查無回空清單。"""
        lt_code = large_traders_code_for_product(product, self._futures_ssf_map)
        return self._futures_lt_by_code.get(lt_code, [])

    def _on_futures_row_selected(self):
        selected = self.futures_table.selectionModel().selectedRows()
        if not selected:
            return
        row = selected[0].row()
        if row < 0 or row >= len(self._futures_rows):
            return
        product, _session = self._futures_rows[row]
        groups = self._futures_groups_for_product(product)
        name = self._futures_display_name(product)
        header = f"{product} {name}".strip()
        if groups:
            self.futures_lt_detail_label.setText(f"{header}　大額交易人未沖銷部位（單位：口數）")
        else:
            self.futures_lt_detail_label.setText(
                f"{header}：TAIFEX 未單獨提供此商品的大額交易人未沖銷部位。"
            )
        _populate_large_traders_table(self.futures_lt_detail_table, groups)

        # 趨勢圖用歷史表（大額端點代碼、所有契約合計、所有交易人）：SQLite 查詢
        # debounce（100ms）＋背景執行緒（見 _load_futures_trend），避免快速切換
        # 列表時同步查詢卡住主執行緒。
        lt_code = large_traders_code_for_product(product, self._futures_ssf_map)
        self._futures_trend_expected_code = lt_code
        self._futures_trend_timer.start(100)

    def _load_futures_trend(self):
        lt_code = self._futures_trend_expected_code
        if not lt_code:
            return

        def work(_cancel_event, _emit_progress):
            return get_large_traders_history_series(
                app_paths.HISTORY_DB_PATH, lt_code, LARGE_TRADERS_HISTORY_ALL_CONTRACTS_MONTH, "0"
            )

        def on_done(trend):
            if lt_code != self._futures_trend_expected_code:
                return  # 使用者已經選到別的商品，丟棄這份過期結果
            _populate_large_traders_trend(self.futures_lt_trend_chart, trend)

        def on_error(_message):
            if lt_code != self._futures_trend_expected_code:
                return
            self.futures_lt_trend_chart.clear()

        # 見 _load_stock_trend_badges 同樣的註解：一定要留住回傳值，否則
        # BackgroundTask 可能在背景執行緒做完之前就被回收，callback 不會被呼叫。
        self._futures_trend_task = run_background_task(self, work, on_done, on_error)

    def _on_futures_row_double_clicked(self, row, _col):
        """雙擊某商品列 → 彈出該商品的大額交易人未沖銷部位（用「期貨」頁重新
        整理時已抓好、快取在 self._futures_large_traders_rows 的全市場清單就地
        篩出，不另打 API）。尚未抓到大額資料時提示稍後重試。"""
        if row < 0 or row >= len(self._futures_rows):
            return
        product, _session = self._futures_rows[row]
        # 股票期貨在大額端點的代碼是去掉結尾 F（CDF→CD），指數／商品期貨沿用原碼；
        # 換算＋查分組都在 _futures_groups_for_product 裡處理（用重新整理時已建好的
        # self._futures_lt_by_code，不另打 API）。
        groups = self._futures_groups_for_product(product)
        if not groups:
            # 區分兩種空：整份大額資料還沒載入（重新整理可解決）vs. 已載入但
            # TAIFEX 這支端點本來就沒有這個商品（只涵蓋部分主要契約，例如小型
            # 臺指 MTX 併入臺股期貨 TX 統計、不單獨列出，重新整理也不會有）。
            if not self._futures_large_traders_rows:
                message = (
                    "大額交易人未沖銷部位資料尚未載入，請在「設定」頁按"
                    "「重新整理所有資料」後再試。"
                )
            else:
                message = (
                    f"TAIFEX 未單獨提供 {product} 的大額交易人未沖銷部位統計"
                    "（此端點僅涵蓋部分主要契約；小型臺指 MTX 等併入對應大型契約"
                    "如臺股期貨 TX 一併統計）。"
                )
            QtWidgets.QMessageBox.information(self, "大額交易人未沖銷部位", message)
            return
        FuturesLargeTradersDialog(self, product, groups).exec()

    def _rebuild_futures_table(self):
        rows = [
            (product, session)
            for product in self._futures_all_products
            for session in self.FUTURES_SESSION_ORDER
            if session in self._futures_snapshot.get(product, {})
        ]
        self._futures_rows = rows
        self.futures_table.setRowCount(len(rows))
        for index in range(len(rows)):
            self._paint_futures_row(index)
        self._filter_futures_table(self.futures_search_edit.text())

    def _paint_futures_row(self, index):
        product, session = self._futures_rows[index]
        data = self._futures_snapshot[product][session]

        contract_month = data.get("contract_month") or "-"
        last = data.get("last")
        last_text = f"{last:,.0f}" if last is not None else "-"
        change = data.get("change")
        change_text = f"{change:+,.0f}" if change is not None else "-"
        settlement = data.get("settlement_price")
        settlement_text = f"{settlement:,.0f}" if settlement is not None else "-"
        volume = data.get("volume")
        volume_text = f"{volume:,}" if volume is not None else "-"
        open_interest = data.get("open_interest")
        oi_text = f"{open_interest:,}" if open_interest is not None else "-"
        change_pct = data.get("change_pct")
        change_pct_text = f"{change_pct:+.2f}%" if change_pct is not None else "-"

        color = COLOR_GAIN if (change is not None and change >= 0) else (
            COLOR_LOSS if change is not None else None
        )

        underlying_text = self._futures_display_name(product) or "-"

        # 大額交易人摘要欄（近月「所有交易人」前10大未沖銷部位）。
        summary = _large_traders_summary_entry(
            self._futures_groups_for_product(product), data.get("contract_month")
        )
        if summary:
            t10_buy, t10_sell, market_oi = (
                summary["top10_buy"], summary["top10_sell"], summary["market_oi"],
            )
            lt_net = (t10_buy or 0) - (t10_sell or 0)
            lt_buy_text = f"{t10_buy:,}" if t10_buy is not None else "-"
            lt_sell_text = f"{t10_sell:,}" if t10_sell is not None else "-"
            lt_net_text = f"{lt_net:+,}"
            lt_share_text = f"{(t10_buy or 0) / market_oi * 100:.1f}%" if market_oi else "-"
        else:
            lt_net = None
            lt_buy_text = lt_sell_text = lt_net_text = lt_share_text = "-"

        values = [
            product, underlying_text, contract_month, session,
            last_text, change_text, change_pct_text,
            settlement_text, volume_text, oi_text,
            lt_buy_text, lt_sell_text, lt_net_text, lt_share_text,
        ]
        for col, text in enumerate(values):
            item = QtWidgets.QTableWidgetItem(text)
            if col in (4, 5) and color:
                item.setForeground(QtGui.QColor(color))
            if col == 12 and lt_net is not None:  # 大額前10淨：正紅負綠
                item.setForeground(QtGui.QColor(gain_loss_color(lt_net)))
            self.futures_table.setItem(index, col, item)

    def refresh_futures_tab(self, force_refresh=False):
        def fetch():
            rows = get_cached_daily_futures_report(app_paths.FUTURES_CACHE_PATH, force_refresh=force_refresh)
            all_products = list_all_products(rows)
            # 大額交易人未沖銷部位是另一支 TAIFEX 端點，抓失敗（且無舊快取）時
            # 不該讓整個期貨盤後表格跟著壞掉——退回空清單，雙擊時再提示稍後重試。
            # 這裡多回傳一個 bool 記錄「是不是抓失敗才空的」，讓 on_done 能在
            # 畫面上警示，不然使用者只會看到搜尋／大額部位悄悄失效，找不到原因。
            try:
                large_traders_rows = get_cached_large_traders_futures_report(
                    app_paths.FUTURES_LARGE_TRADERS_CACHE_PATH, force_refresh=force_refresh
                )
                large_traders_failed = False
            except PriceFetchError:
                large_traders_rows = []
                large_traders_failed = True
            # 股票期貨標的清單也是輔助資訊，抓失敗不該讓主行情表跟著壞——退回空 map，
            # 個股期貨那幾列的「標的」欄暫時顯示 "-"、仍可用契約代碼搜尋，但用股票
            # 代號／名稱搜尋會找不到任何股票期貨列，同樣需要警示原因。
            try:
                ssf_map = build_ssf_map(
                    get_cached_ssf_list(app_paths.FUTURES_SSF_CACHE_PATH, force_refresh=force_refresh)
                )
                ssf_failed = False
            except PriceFetchError:
                ssf_map = {}
                ssf_failed = True
            return (
                all_products,
                get_futures_snapshot(all_products, rows=rows),
                large_traders_rows,
                ssf_map,
                large_traders_failed,
                ssf_failed,
            )

        def on_done(result):
            (
                all_products,
                snapshot,
                large_traders_rows,
                ssf_map,
                large_traders_failed,
                ssf_failed,
            ) = result
            self._futures_all_products = all_products
            self._futures_snapshot = snapshot
            self._futures_large_traders_rows = large_traders_rows
            self._futures_lt_by_code = group_large_traders_all(large_traders_rows)
            self._futures_ssf_map = ssf_map
            # 非股票期貨的中文名（供顯示／搜尋）：用大額端點的 ContractName，key 換算
            # 成日盤商品代碼。股票期貨的名稱另外走 ssf_map（_futures_display_name 處理）。
            lt_name_map = build_large_traders_name_map(large_traders_rows)
            self._futures_name_map = {
                product: lt_name_map[code]
                for product in all_products
                for code in [large_traders_code_for_product(product, ssf_map)]
                if code in lt_name_map
            }
            data_date = _futures_snapshot_date(snapshot)
            status_text = f"資料日期：{data_date}" if data_date else ""
            warnings = []
            if ssf_failed:
                warnings.append("股票期貨標的清單載入失敗，暫時無法用股票代號／名稱搜尋股票期貨")
            if large_traders_failed:
                warnings.append("大額交易人未沖銷部位載入失敗，暫無資料")
            if warnings:
                status_text = (f"{status_text}　" if status_text else "") + "⚠ " + "；".join(warnings)
            self.futures_status_label.setText(status_text)
            self._rebuild_futures_table()
            self._refresh_data_freshness_label()

        def on_error(message):
            self.futures_status_label.setText(f"期貨盤後資訊取得失敗：{message}")

        self._futures_task_timer = run_task_in_thread(self, fetch, on_done, on_error)

    def _filter_futures_table(self, text):
        """依關鍵字（不分大小寫、子字串比對）篩選期貨表格：**關鍵字為空時顯示
        全部商品**，輸入關鍵字後只留符合的列。比對字串含契約代碼（TX／CDF）、股票
        期貨標的代號（2330）與名稱（台積電，見 self._futures_ssf_map），以及大額
        端點提供的中文契約名（布蘭特原油期貨，見 self._futures_name_map）。"""
        keyword = text.strip().lower()
        for index, (product, _session) in enumerate(self._futures_rows):
            if not keyword:
                self.futures_table.setRowHidden(index, False)
                continue
            haystack = product.lower()
            ssf = self._futures_ssf_map.get(product)
            if ssf:
                haystack += f" {ssf['stock_code'] or ''} {(ssf['stock_name'] or '').lower()}"
            name = self._futures_name_map.get(product)
            if name:
                haystack += f" {name.lower()}"
            self.futures_table.setRowHidden(index, keyword not in haystack)
