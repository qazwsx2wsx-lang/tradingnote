"""「個股」分頁（產業樹、搜尋、摘要卡與 StockDetailDialog 入口）。

以 mixin 形式併入 tradingnote_gui.TradingNoteWindow；方法直接使用視窗的 self 狀態。"""

from PySide6 import QtCore, QtWidgets

from tradingnote_finmind import FINMIND_HOURLY_LIMIT, get_call_count
from tradingnote_history import _change_pct, get_industry_directory
from tradingnote_http import PriceFetchError
from tradingnote_tasks import run_background_task
from tradingnote_technical import load_local_technical

from ui import app_paths
from ui.badges import _populate_badge_row, _stock_trend_badges
from ui.components.stat_card import StatCard
from ui.dialogs.stock_detail import StockDetailDialog
from ui.theme import COLOR_ACCENT, COLOR_LOSS, COLOR_MUTED
from ui.widgets import _clear_layout, _set_standard_icon, accent_button


class StocksTabMixin:
    """TradingNoteWindow 的「個股」分頁方法。"""

    # ---------- 個股頁 ----------

    def _build_stocks_tab(self):
        layout = QtWidgets.QVBoxLayout(self.stocks_tab)
        layout.setContentsMargins(10, 10, 10, 10)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.addWidget(QtWidgets.QLabel("搜尋"))
        self.stock_search_edit = QtWidgets.QLineEdit()
        self.stock_search_edit.setPlaceholderText("搜尋股票代號或名稱")
        self.stock_search_edit.setMinimumWidth(160)
        self.stock_search_edit.setMaximumWidth(320)
        self.stock_search_edit.addAction(
            self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogContentsView),
            QtWidgets.QLineEdit.LeadingPosition,
        )
        self._stock_filter_timer = QtCore.QTimer(self)
        self._stock_filter_timer.setSingleShot(True)
        self._stock_filter_timer.timeout.connect(
            lambda: self._filter_stocks_tree(self.stock_search_edit.text())
        )
        self.stock_search_edit.textChanged.connect(self._on_stock_search_text_changed)
        toolbar.addWidget(self.stock_search_edit)

        hint = QtWidgets.QLabel(
            "雙擊股票查詢本益比／法人買賣（上市透過 FinMind API，上櫃本益比／殖利率"
            "改查 TPEX 官方端點；法人買賣不分市場皆為 FinMind）"
        )
        hint.setProperty("muted", True)
        hint.setToolTip(hint.text())
        self.stock_search_hint = hint
        toolbar.addWidget(hint)
        toolbar.addStretch(1)

        self.finmind_count_label = QtWidgets.QLabel()
        self.finmind_count_label.setProperty("muted", True)
        toolbar.addWidget(self.finmind_count_label)
        layout.addLayout(toolbar)
        self.update_finmind_count_label()

        self.stocks_tree = QtWidgets.QTreeWidget()
        self.stocks_tree.setColumnCount(4)
        self.stocks_tree.setHeaderLabels(["名稱", "代號", "現價", "漲跌%"])
        self.stocks_tree.setColumnWidth(0, 160)
        self.stocks_tree.setColumnWidth(1, 80)
        self.stocks_tree.setColumnWidth(2, 90)
        self.stocks_tree.itemDoubleClicked.connect(self._on_stock_double_clicked)
        self.stocks_tree.currentItemChanged.connect(self._on_stock_selected)

        self.stock_preview = QtWidgets.QFrame()
        self.stock_preview.setProperty("stockHero", True)
        preview_layout = QtWidgets.QVBoxLayout(self.stock_preview)
        preview_layout.setContentsMargins(18, 18, 18, 18)
        preview_caption = QtWidgets.QLabel("個股摘要")
        preview_caption.setProperty("muted", True)
        self.stock_preview_title = QtWidgets.QLabel("選取股票查看摘要")
        self.stock_preview_title.setProperty("header", True)
        self.stock_preview_price = StatCard(bordered=False)
        self.stock_preview_meta = QtWidgets.QLabel("雙擊股票可開啟完整估值與法人資訊。")
        self.stock_preview_meta.setProperty("muted", True)
        self.stock_preview_meta.setWordWrap(True)
        self.stock_preview_button = accent_button("查看完整分析", self._open_selected_stock_detail)
        _set_standard_icon(
            self.stock_preview_button,
            QtWidgets.QStyle.SP_FileDialogInfoView,
            "開啟完整個股分析",
        )
        self.stock_preview_button.setEnabled(False)
        self.stock_preview_trend_row = QtWidgets.QHBoxLayout()
        self.stock_preview_trend_row.setSpacing(6)
        preview_layout.addWidget(preview_caption)
        preview_layout.addWidget(self.stock_preview_title)
        preview_layout.addSpacing(8)
        preview_layout.addWidget(self.stock_preview_price)
        preview_layout.addLayout(self.stock_preview_trend_row)
        preview_layout.addWidget(self.stock_preview_meta)
        preview_layout.addStretch(1)
        preview_layout.addWidget(self.stock_preview_button)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.addWidget(self.stocks_tree)
        splitter.addWidget(self.stock_preview)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([850, 260])
        layout.addWidget(splitter, 1)

    def refresh_stocks_tab(self):
        try:
            directory = get_industry_directory(app_paths.HISTORY_DB_PATH)
        except PriceFetchError as e:
            QtWidgets.QMessageBox.warning(self, "錯誤", f"無法取得產業分類資料：{e}")
            return

        groups = {}
        for ticker, info in directory.items():
            groups.setdefault(info["industry"] or "未分類", []).append((ticker, info))

        expanded = {
            self.stocks_tree.topLevelItem(i).text(0).split("（")[0]
            for i in range(self.stocks_tree.topLevelItemCount())
            if self.stocks_tree.topLevelItem(i).isExpanded()
        }

        self.stocks_tree.setUpdatesEnabled(False)
        try:
            self.stocks_tree.clear()
            for industry in sorted(groups):
                tickers = groups[industry]
                industry_item = QtWidgets.QTreeWidgetItem([f"{industry}（{len(tickers)}）"])
                self.stocks_tree.addTopLevelItem(industry_item)
                industry_item.setExpanded(industry in expanded)

                for ticker, info in sorted(tickers, key=lambda t: t[0]):
                    price = self.snapshot.get(ticker)
                    if price is not None and price.close is not None:
                        close_text = f"{price.close:.2f}"
                        change_pct = _change_pct(price)
                        change_text = f"{change_pct:+.2f}%" if change_pct is not None else "-"
                    else:
                        close_text = "-"
                        change_text = "-"
                    name = (price.name if price and price.name else None) or info["name"] or "-"

                    # 欄位順序對齊 header：名稱／代號／現價／漲跌%
                    child = QtWidgets.QTreeWidgetItem([name, ticker, close_text, change_text])
                    child.setData(0, QtCore.Qt.UserRole, ticker)
                    # UserRole + 1 存市場別（TWSE／TPEX），雙擊查詢時用來決定本益比／
                    # 殖利率要查 FinMind 還是 TPEX 官方端點（見 StockDetailDialog）。
                    child.setData(0, QtCore.Qt.UserRole + 1, info["market"])
                    child.setData(0, QtCore.Qt.UserRole + 2, industry)
                    industry_item.addChild(child)
        finally:
            self.stocks_tree.setUpdatesEnabled(True)

        self._filter_stocks_tree(self.stock_search_edit.text())

    def _on_stock_search_text_changed(self, _text):
        """打字時 debounce（150ms）才真的過濾，避免每個按鍵都對約1700個項目
        逐一 setHidden 造成明顯卡頓；停止輸入後才觸發 _filter_stocks_tree。"""
        self._stock_filter_timer.start(150)

    def _filter_stocks_tree(self, text):
        """依代號／名稱關鍵字（不分大小寫、子字串比對）過濾樹狀列表：符合的個股
        顯示，其餘隱藏；產業分組列則依底下是否還有符合的個股決定顯示／隱藏，
        有輸入關鍵字時自動展開有符合結果的分組。"""
        keyword = text.strip().lower()
        self.stocks_tree.setUpdatesEnabled(False)
        try:
            for i in range(self.stocks_tree.topLevelItemCount()):
                industry_item = self.stocks_tree.topLevelItem(i)
                visible_count = 0
                for j in range(industry_item.childCount()):
                    child = industry_item.child(j)
                    matched = (
                        not keyword
                        or keyword in child.text(0).lower()
                        or keyword in child.text(1).lower()
                    )
                    child.setHidden(not matched)
                    if matched:
                        visible_count += 1
                industry_item.setHidden(visible_count == 0)
                if keyword:
                    industry_item.setExpanded(visible_count > 0)
        finally:
            self.stocks_tree.setUpdatesEnabled(True)

    def update_finmind_count_label(self):
        """更新「個股」頁的 FinMind 用量提醒；免費額度是
        600 次／小時，接近或超過時把文字變色提醒，避免使用者查到一半才發現被 FinMind 擋掉。"""
        count = get_call_count()
        text = f"FinMind API 過去 1 小時：{count} / {FINMIND_HOURLY_LIMIT} 次"
        if count >= FINMIND_HOURLY_LIMIT:
            text += "　已達免費額度上限，查詢可能會失敗"
            color = COLOR_LOSS
        elif count >= FINMIND_HOURLY_LIMIT * 0.8:
            text += "　接近上限，請留意"
            color = COLOR_ACCENT
        else:
            color = COLOR_MUTED

        label = getattr(self, "finmind_count_label", None)
        if label is not None:
            label.setText(text)
            label.setStyleSheet(f"color: {color};")

    def _on_stock_double_clicked(self, item, _column):
        ticker = item.data(0, QtCore.Qt.UserRole)
        if not ticker:
            return  # 點到的是產業分組列，不是個股
        name = item.text(0)
        market = item.data(0, QtCore.Qt.UserRole + 1)
        token = self.settings.get("finmind_token", "")
        StockDetailDialog(self, ticker, name, token, market=market).exec()

    def _on_stock_selected(self, item, _previous=None):
        ticker = item.data(0, QtCore.Qt.UserRole) if item is not None else None
        if not ticker:
            self.stock_preview_title.setText("選取股票查看摘要")
            self.stock_preview_price.set_value("—")
            self.stock_preview_meta.setText("雙擊股票可開啟完整估值與法人資訊。")
            self.stock_preview_button.setEnabled(False)
            _clear_layout(self.stock_preview_trend_row)
            self._stock_trend_timer.stop()
            self._stock_trend_expected_ticker = None
            return
        price = self.snapshot.get(ticker)
        name = item.text(0)
        market = item.data(0, QtCore.Qt.UserRole + 1) or "—"
        industry = item.data(0, QtCore.Qt.UserRole + 2) or "未分類"
        self.stock_preview_title.setText(f"{name}　{ticker}")
        # load_local_technical 內含 SQLite 查詢＋全歷史技術指標運算，改成
        # debounce（120ms，方向鍵快速瀏覽時不必每格都觸發）＋背景執行緒
        # （見 _load_stock_trend_badges），不在選取事件裡同步卡住主執行緒。
        self._stock_trend_expected_ticker = ticker
        self._stock_trend_timer.start(120)
        if price is not None and price.close is not None:
            change_pct = _change_pct(price)
            change_text = f"{change_pct:+.2f}%" if change_pct is not None else "—"
            status = None if change_pct is None else ("positive" if change_pct >= 0 else "negative")
            self.stock_preview_price.set_value(
                f"{price.close:,.2f}　{change_text}", status=status
            )
            volume_text = f"{(price.volume or 0) / 1000:,.0f} 張"
            self.stock_preview_meta.setText(
                f"{market} · {industry}\n成交量 {volume_text}\n資料日期 {price.date or '—'}"
            )
        else:
            self.stock_preview_price.set_value("暫無行情")
            self.stock_preview_meta.setText(f"{market} · {industry}")
        self.stock_preview_button.setEnabled(True)

    def _load_stock_trend_badges(self):
        """_on_stock_selected debounce 後實際觸發：背景執行緒查技術指標
        （TTLCache 依 ticker 快取 5 分鐘，同一檔位重選不必重算），完成時若
        使用者已經選到別的股票就丟棄，避免舊結果蓋掉新選取的畫面。"""
        ticker = self._stock_trend_expected_ticker
        if not ticker:
            return

        def work(_cancel_event, _emit_progress):
            return self._stock_technical_cache.get_or_fetch(
                ticker, lambda: load_local_technical(app_paths.HISTORY_DB_PATH, ticker)
            )

        def on_done(technical):
            if ticker != self._stock_trend_expected_ticker:
                return
            _populate_badge_row(
                self.stock_preview_trend_row,
                _stock_trend_badges(technical),
                empty_text="歷史資料不足，暫無法判斷趨勢／動能／量能。",
            )

        def on_error(_message):
            if ticker != self._stock_trend_expected_ticker:
                return
            _clear_layout(self.stock_preview_trend_row)

        # 一定要留住回傳值：BackgroundTask 沒有其他地方持有參照的話，
        # Python 會在背景執行緒做完之前就把它回收，callback 永遠不會被呼叫
        # （其餘既有的 run_background_task／run_task_in_thread 呼叫點都遵守
        # 這個慣例，見 tradingnote_gui.py 內其他 self._xxx_timer = ... 賦值）。
        self._stock_trend_task = run_background_task(self, work, on_done, on_error)

    def _open_selected_stock_detail(self):
        item = self.stocks_tree.currentItem()
        if item is not None:
            self._on_stock_double_clicked(item, 0)
