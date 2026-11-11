"""「即時報價」分頁：玉山 Market Data SDK 的監控清單（期貨／選擇權／股票）。

以 mixin 形式併入 tradingnote_gui.TradingNoteWindow。日盤／股票走 WebSocket 推播，
期貨夜盤走 5 秒 REST 輪詢（見 tradingnote_esun.QuoteSession）。SDK 的回呼來自非 Qt 執行緒，
一律透過 _QuoteBridge 的 signal 轉回主執行緒，再合併成最多每秒一次的重繪；
同一次重繪也會更新「部位紀錄」的現價／損益。"""

from datetime import datetime

from PySide6 import QtCore, QtGui, QtWidgets

import tradingnote_esun as esun

from ui.format import gain_loss_color
from ui.widgets import accent_button, show_error
from ui.workers import run_task_in_thread

_COLUMNS = ("代號", "名稱", "類型", "最新價", "漲跌", "漲跌%", "成交量", "夜盤價", "夜盤漲跌%")
_REDRAW_INTERVAL_MS = 1000


class _QuoteBridge(QtCore.QObject):
    updated = QtCore.Signal(str)


def _fmt(value, spec="{}"):
    if value is None:
        return "-"
    try:
        return spec.format(value)
    except (TypeError, ValueError):
        return str(value)


def _signed_color(value):
    try:
        return gain_loss_color(float(value))
    except (TypeError, ValueError):
        return None


class QuotesTabMixin:
    """TradingNoteWindow 的「即時報價」分頁方法。"""

    def _build_quotes_tab(self):
        self._quotes_session = None
        self._quotes_connecting = False
        self._quotes_tasks = []
        self._quotes_dirty = False
        self._quotes_bridge = _QuoteBridge()
        self._quotes_bridge.updated.connect(self._on_quote_updated)
        self._quotes_redraw_timer = QtCore.QTimer(self)
        self._quotes_redraw_timer.setSingleShot(True)
        self._quotes_redraw_timer.timeout.connect(self._redraw_quotes)

        layout = QtWidgets.QVBoxLayout(self.quotes_tab)
        layout.setContentsMargins(10, 10, 10, 10)

        toolbar = QtWidgets.QHBoxLayout()
        self.quotes_connect_button = accent_button("連線", self._toggle_quotes_connection)
        toolbar.addWidget(self.quotes_connect_button)
        self.quotes_input = QtWidgets.QLineEdit()
        self.quotes_input.setPlaceholderText("代碼（可多個，空格分隔，如 TXFK6 2330）或中文關鍵字搜尋期貨")
        self.quotes_input.returnPressed.connect(self._submit_quotes_input)
        toolbar.addWidget(self.quotes_input, 1)
        self.quotes_add_button = QtWidgets.QPushButton("加入／搜尋", clicked=self._submit_quotes_input)
        toolbar.addWidget(self.quotes_add_button)
        self.quotes_remove_button = QtWidgets.QPushButton("移除選取", clicked=self._remove_selected_quotes)
        toolbar.addWidget(self.quotes_remove_button)
        layout.addLayout(toolbar)

        self.quotes_status_label = QtWidgets.QLabel("")
        self.quotes_status_label.setProperty("muted", True)
        self.quotes_status_label.setWordWrap(True)
        layout.addWidget(self.quotes_status_label)

        self.quotes_table = QtWidgets.QTableWidget(0, len(_COLUMNS))
        self.quotes_table.setHorizontalHeaderLabels(_COLUMNS)
        self.quotes_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.quotes_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.quotes_table.verticalHeader().setVisible(False)
        self.quotes_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.quotes_table, 1)

        self.quotes_search_table = QtWidgets.QTableWidget(0, 4)
        self.quotes_search_table.setHorizontalHeaderLabels(("代碼", "名稱", "類型", "到期日"))
        self.quotes_search_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.quotes_search_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.quotes_search_table.verticalHeader().setVisible(False)
        self.quotes_search_table.horizontalHeader().setStretchLastSection(True)
        self.quotes_search_table.setMaximumHeight(160)
        self.quotes_search_table.setToolTip("雙擊搜尋結果加入監控")
        self.quotes_search_table.cellDoubleClicked.connect(self._add_search_result)
        self.quotes_search_table.setVisible(False)
        layout.addWidget(self.quotes_search_table)

        self._refresh_quotes_controls()

    # ---------- 連線 ----------

    def _quotes_run(self, work, on_done, on_error=None):
        """在背景執行緒跑會阻塞的 SDK 呼叫；保留參考避免被回收。"""
        task = run_task_in_thread(self, work, on_done, on_error or self._on_quotes_error)
        self._quotes_tasks.append(task)
        return task

    def _on_quotes_error(self, message):
        self.quotes_status_label.setText(f"⚠ {message}")

    def _refresh_quotes_controls(self):
        connected = self._quotes_session is not None
        busy = self._quotes_connecting
        self.quotes_connect_button.setText("中斷連線" if connected else "連線")
        self.quotes_connect_button.setEnabled(not busy)
        for widget in (self.quotes_input, self.quotes_add_button, self.quotes_remove_button):
            widget.setEnabled(connected)

    def _toggle_quotes_connection(self):
        if self._quotes_session is not None:
            self._disconnect_quotes()
            return
        if self._quotes_connecting:
            return
        settings = dict(self.settings)
        self._quotes_connecting = True
        self.quotes_status_label.setText("登入玉山中...")
        self._refresh_quotes_controls()

        def work():
            return esun.connect_session(settings, on_update=self._quotes_bridge.updated.emit)

        def on_done(session):
            self._quotes_connecting = False
            self._quotes_session = session
            self.quotes_status_label.setText(
                "已連線。日盤／股票為 WebSocket 即時推播，期貨夜盤每 5 秒輪詢。"
            )
            self._refresh_quotes_controls()
            self._sync_position_quote_subscriptions()

        def on_error(message):
            self._quotes_connecting = False
            self._refresh_quotes_controls()
            self.quotes_status_label.setText(f"⚠ 連線失敗：{message}")

        self._quotes_run(work, on_done, on_error)

    def _disconnect_quotes(self):
        session, self._quotes_session = self._quotes_session, None
        if session is not None:
            self._quotes_run(session.stop, lambda _result: None)
        self.quotes_table.setRowCount(0)
        self.quotes_status_label.setText("已中斷連線。")
        self._refresh_quotes_controls()
        # 現價改回快照收盤價
        if hasattr(self, "_refresh_position_live_prices"):
            self._refresh_position_live_prices()

    def _shutdown_quotes(self):
        """視窗關閉時停止 WebSocket 與輪詢執行緒。"""
        session, self._quotes_session = getattr(self, "_quotes_session", None), None
        if session is not None:
            session.stop()

    # ---------- 監控清單 ----------

    def _submit_quotes_input(self):
        session = self._quotes_session
        raw = self.quotes_input.text().strip()
        if session is None or not raw:
            return
        self.quotes_input.clear()

        if any("一" <= ch <= "鿿" for ch in raw):
            self._search_quotes(session, raw)
            return
        for token in raw.upper().split():
            if token.startswith("-"):
                self._remove_quote(token[1:])
            else:
                self._add_quote(token)

    def _add_quote(self, symbol, kind=None, quiet=False):
        session = self._quotes_session
        if session is None:
            return

        def work():
            return session.add(symbol, kind)

        def on_done(result):
            _kind, error = result
            if error and not quiet:
                self.quotes_status_label.setText(f"⚠ {error}")
            self._queue_quotes_redraw()

        self._quotes_run(work, on_done)

    def _remove_quote(self, symbol):
        session = self._quotes_session
        if session is None:
            return
        self._quotes_run(
            lambda: session.remove(symbol), lambda _removed: self._queue_quotes_redraw()
        )

    def _remove_selected_quotes(self):
        symbols = {
            self.quotes_table.item(index.row(), 0).text()
            for index in self.quotes_table.selectionModel().selectedRows()
        }
        for symbol in symbols:
            self._remove_quote(symbol)

    def _search_quotes(self, session, keyword):
        self.quotes_status_label.setText(f"搜尋「{keyword}」中...")

        def on_done(results):
            self._search_results = results[:30]
            self.quotes_search_table.setRowCount(len(self._search_results))
            for row, values in enumerate(self._search_results):
                for col, value in enumerate(values):
                    self.quotes_search_table.setItem(row, col, QtWidgets.QTableWidgetItem(str(value)))
            self.quotes_search_table.setVisible(bool(self._search_results))
            self.quotes_status_label.setText(
                f"找到 {len(results)} 筆（顯示前 30 筆），雙擊加入監控。"
                if results else "找不到符合的商品名稱。"
            )

        self._quotes_run(lambda: session.search_keyword(keyword), on_done)

    def _add_search_result(self, row, _column):
        results = getattr(self, "_search_results", [])
        if 0 <= row < len(results):
            self._add_quote(results[row][0], esun.KIND_FUTOPT)

    def _sync_position_quote_subscriptions(self):
        """連線中時，把目前 OPEN 部位的代號都加入監控（已在清單的會被略過）。"""
        session = getattr(self, "_quotes_session", None)
        if session is None:
            return
        known = set(session.snapshot())
        for pos in getattr(self, "positions", []):
            trade = getattr(self, "_position_trades", {}).get(pos.id)
            symbol = pos.ticker.strip().upper()
            if trade is not None and trade.status == "OPEN" and symbol not in known:
                known.add(symbol)
                self._add_quote(symbol, quiet=True)

    # ---------- 畫面 ----------

    def _on_quote_updated(self, _symbol):
        self._queue_quotes_redraw()

    def _queue_quotes_redraw(self):
        self._quotes_dirty = True
        if not self._quotes_redraw_timer.isActive():
            self._quotes_redraw_timer.start(_REDRAW_INTERVAL_MS)

    def _redraw_quotes(self):
        self._quotes_dirty = False
        session = self._quotes_session
        if session is None:
            return
        state = session.snapshot()
        show_night = esun.should_show_night_session()
        day_status = esun.day_session_status()
        self.quotes_table.setRowCount(len(state))
        for row, (symbol, entry) in enumerate(state.items()):
            day = esun.quote_fields(entry["day"])
            is_futopt = entry["kind"] == esun.KIND_FUTOPT
            night = esun.quote_fields(entry["ah"]) if is_futopt and show_night else {"ok": False}
            if day["ok"]:
                name, last = day["name"], _fmt(day["last"])
                change, pct = _fmt(day["change"]), _fmt(day["pct"], "{}%")
                volume = _fmt(day["volume"], "{:,}")
                color = _signed_color(day["change"])
            else:
                name, last, change, pct, volume, color = "-", "-", "-", "-", "-", None
            kind_label = f"期貨/選擇權 [{day_status}]" if is_futopt else "股票"
            cells = [
                symbol, name, kind_label, last, change, pct, volume,
                _fmt(night.get("last")) if night["ok"] else "-",
                _fmt(night.get("pct"), "{}%") if night["ok"] else "-",
            ]
            for col, text in enumerate(cells):
                item = QtWidgets.QTableWidgetItem(text)
                if col in (3, 4, 5) and color:
                    item.setForeground(QtGui.QColor(color))
                if col == 8 and night["ok"] and _signed_color(night.get("change")):
                    item.setForeground(QtGui.QColor(_signed_color(night.get("change"))))
                if col >= 3:
                    item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                self.quotes_table.setItem(row, col, item)
        self.quotes_status_label.setToolTip(f"最後更新 {datetime.now():%H:%M:%S}")
        if hasattr(self, "_refresh_position_live_prices"):
            self._refresh_position_live_prices()
