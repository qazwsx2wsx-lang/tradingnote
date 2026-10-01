"""「設定」分頁與資料維護動作（歷史回補、連續性檢查、大額交易人歷史同步）。

以 mixin 形式併入 tradingnote_gui.TradingNoteWindow；方法直接使用視窗的 self 狀態。"""

from PySide6 import QtCore, QtWidgets

from tradingnote_core import save_settings
from tradingnote_history import DEFAULT_BACKFILL_TARGET_DAYS, get_history_status
from tradingnote_taifex import backfill_large_traders_history

from ui import app_paths
from ui.dialogs.progress import BackfillDialog, TpexBackfillDialog
from ui.format import _format_history_status, _futures_snapshot_date, _snapshot_date
from ui.widgets import _set_standard_icon, accent_button
from ui.workers import run_backfill_in_thread, run_task_in_thread


class SettingsTabMixin:
    """TradingNoteWindow 的「設定」分頁與資料維護動作方法。"""

    def open_backfill_dialog(self):
        target_days = self.settings.get("backfill_target_days", DEFAULT_BACKFILL_TARGET_DAYS)

        def on_complete():
            self.refresh_flow_tab()
            self._refresh_history_status()

        dialog = BackfillDialog(self, target_days=target_days, on_complete=on_complete)
        dialog.exec()

    def open_tpex_backfill_dialog(self):
        token = self.settings.get("finmind_token", "")
        if not token:
            QtWidgets.QMessageBox.information(
                self,
                "提示",
                "請先在「設定」分頁填入 FinMind API Token 才能使用這個功能。",
            )
            return

        target_days = self.settings.get("backfill_target_days", DEFAULT_BACKFILL_TARGET_DAYS)

        def on_complete():
            self.refresh_flow_tab()
            self._refresh_history_status()
            self.update_finmind_count_label()

        dialog = TpexBackfillDialog(self, token, target_days=target_days, on_complete=on_complete)
        dialog.exec()

    def _sync_history_continuity(self):
        def on_progress(done, target):
            self.continuity_note = f"同步歷史資料中... {done}/{target} 天"
            self._update_status_bar()

        def on_done(done):
            had_progress = bool(self.continuity_note)
            self.continuity_note = ""
            self._update_status_bar()
            if had_progress:
                self.refresh_flow_tab()
                self._refresh_history_status()

        def on_error(_message):
            self.continuity_note = ""
            self._update_status_bar()

        target_days = self.settings.get("backfill_target_days", DEFAULT_BACKFILL_TARGET_DAYS)
        self._sync_timer = run_backfill_in_thread(
            self, target_days, on_progress, on_done, on_error
        )

    def _sync_large_traders_history(self):
        """啟動時在背景把最近「回補天數」天的期貨大額交易人未沖銷部位歷史補進
        large_traders_history（TAIFEX 網站歷史 CSV，見
        tradingnote_taifex.backfill_large_traders_history），供「期貨」頁趨勢圖用。
        跟 _sync_history_continuity 同一套設計：綁定同一顆 backfill_target_days、
        受同一個「每次啟動自動檢測」開關控制，資料已是最新時幾乎瞬間完成、狀態列
        不留痕跡；真的補到資料才在完成後刷新目前選取商品的趨勢圖。冪等、可安全
        中斷重跑（見 backfill_large_traders_history）。"""
        target_days = self.settings.get("backfill_target_days", DEFAULT_BACKFILL_TARGET_DAYS)

        def work():
            return backfill_large_traders_history(app_paths.HISTORY_DB_PATH, target_days)

        def on_done(result):
            had_note = bool(self.large_traders_note)
            self.large_traders_note = ""
            self._update_status_bar()
            # 真的補到新資料（或本來就在等）才刷新趨勢圖，避免無謂重畫。
            if result.get("rows") or had_note:
                self._on_futures_row_selected()

        def on_error(_message):
            self.large_traders_note = ""
            self._update_status_bar()

        self.large_traders_note = "期貨大額交易人歷史回補中..."
        self._update_status_bar()
        self._large_traders_sync_timer = run_task_in_thread(
            self, work, on_done, on_error
        )

    # ---------- 設定頁 ----------

    def _build_settings_tab(self):
        outer = QtWidgets.QVBoxLayout(self.settings_tab)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget()
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setAlignment(QtCore.Qt.AlignTop)

        header = QtWidgets.QLabel("資料庫")
        header.setProperty("header", True)
        layout.addWidget(header)
        layout.addSpacing(4)

        # 全部的「重新整理」統一到這裡：原本資金流向分析／部位紀錄／期貨三個分頁
        # 各有一顆重複的按鈕，其實都是呼叫同一個 force_refresh（期貨在
        # _apply_refresh_result 裡跟著一起刷新），拆開放在各分頁反而讓人以為
        # 是三個獨立的資料來源；集中在這裡＋下方即時顯示各類資料實際的最新日期，
        # 使用者才看得出「重新整理」到底有沒有真的抓到新資料。
        refresh_all_button = accent_button("重新整理所有資料", self.force_refresh)
        _set_standard_icon(
            refresh_all_button, QtWidgets.QStyle.SP_BrowserReload, "重新整理所有資料"
        )
        layout.addWidget(refresh_all_button, alignment=QtCore.Qt.AlignLeft)
        layout.addSpacing(4)

        self.data_freshness_label = QtWidgets.QLabel("")
        self.data_freshness_label.setProperty("muted", True)
        self.data_freshness_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self.data_freshness_label)
        self._refresh_data_freshness_label()
        layout.addSpacing(20)

        self.auto_check_box = QtWidgets.QCheckBox(
            "每次啟動自動檢測（TWSE 股價、期貨大額交易人未沖銷部位歷史有缺口時自動補齊）"
        )
        self.auto_check_box.setChecked(self.settings.get("auto_check_continuity", True))
        self.auto_check_box.toggled.connect(self._on_auto_check_toggle)
        layout.addWidget(self.auto_check_box)

        hint = QtWidgets.QLabel("關閉後，仍可用下方按鈕手動回補。")
        hint.setProperty("muted", True)
        layout.addWidget(hint)
        layout.addSpacing(10)

        backfill_days_row = QtWidgets.QHBoxLayout()
        backfill_days_row.addWidget(
            QtWidgets.QLabel("回補天數（上市 TWSE／上櫃 TPEX 股價、期貨大額交易人歷史共用）")
        )
        self.backfill_days_spin = QtWidgets.QSpinBox()
        self.backfill_days_spin.setRange(20, 500)
        self.backfill_days_spin.setSingleStep(10)
        self.backfill_days_spin.setSuffix(" 天")
        self.backfill_days_spin.setValue(
            self.settings.get("backfill_target_days", DEFAULT_BACKFILL_TARGET_DAYS)
        )
        self.backfill_days_spin.valueChanged.connect(self._on_backfill_days_changed)
        backfill_days_row.addWidget(self.backfill_days_spin)
        backfill_days_row.addStretch(1)
        layout.addLayout(backfill_days_row)

        backfill_days_hint = QtWidgets.QLabel(
            "上市（TWSE）按「回補歷史資料」，走官方免費端點，逐日回補。"
            "上櫃（TPEX）沒有官方免費歷史端點，改按「使用 FinMind 補上櫃缺口」，"
            "逐檔呼叫 FinMind（需先設定好 finmind_token）；免費額度 600 次／小時，"
            "全市場上櫃約 800 檔，一次通常補不完，額度用完會自動暫停並記錄進度，"
            "之後再按一次即可接續，不會重補。調整天數後，下次按按鈕或自動同步才會套用新天數。"
        )
        backfill_days_hint.setProperty("muted", True)
        backfill_days_hint.setWordWrap(True)
        layout.addWidget(backfill_days_hint)
        layout.addSpacing(10)

        backfill_buttons_row = QtWidgets.QHBoxLayout()
        backfill_buttons_row.addWidget(
            accent_button("回補歷史資料", self.open_backfill_dialog)
        )
        backfill_buttons_row.addWidget(
            QtWidgets.QPushButton("使用 FinMind 補上櫃缺口", clicked=self.open_tpex_backfill_dialog)
        )
        backfill_buttons_row.addStretch(1)
        layout.addLayout(backfill_buttons_row)

        layout.addSpacing(10)
        valuation_backfill_hint = QtWidgets.QLabel(
            "「資金流向分析」頁泡泡圖的「估值」模式（PER/PBR）只看每檔股票最新一筆"
            "本益比／淨值比，靠每次「重新整理」／啟動時自動存的估值快照即可，"
            "不需要另外回補歷史資料，也沒有對應的設定選項。"
        )
        valuation_backfill_hint.setProperty("muted", True)
        valuation_backfill_hint.setWordWrap(True)
        layout.addWidget(valuation_backfill_hint)

        layout.addSpacing(20)
        status_header = QtWidgets.QLabel("資料庫狀態")
        status_header.setProperty("header", True)
        layout.addWidget(status_header)
        layout.addSpacing(4)

        self.history_status_label = QtWidgets.QLabel("")
        self.history_status_label.setWordWrap(True)
        self.history_status_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self.history_status_label)
        self._refresh_history_status()

    def _on_auto_check_toggle(self, checked):
        self.settings["auto_check_continuity"] = checked
        save_settings(app_paths.SETTINGS_PATH, self.settings)

    def _on_backfill_days_changed(self, value):
        self.settings["backfill_target_days"] = value
        save_settings(app_paths.SETTINGS_PATH, self.settings)

    def _refresh_data_freshness_label(self):
        """顯示各類資料「實際擷取到的最新一筆資料日期」，不是「上次按重新整理的
        時間」——重新整理有可能因為連線失敗、或當天 TWSE/TPEX/TAIFEX 還沒發布新
        資料而拿到跟之前一樣的日期，這裡一律以資料本身的日期欄位為準（_snapshot_date
        取 PriceInfo.date 眾數、_futures_snapshot_date 取 TAIFEX 回傳的 Date 欄位）。"""
        stock_date = _snapshot_date(self.snapshot) or "尚無資料"
        futures_date = _futures_snapshot_date(getattr(self, "_futures_snapshot", {})) or "尚無資料"
        self.data_freshness_label.setText(
            f"台股即時報價（TWSE／TPEX）最新日期：{stock_date}\n"
            f"期貨盤後（TAIFEX 全商品）最新日期：{futures_date}"
        )

    def _refresh_history_status(self):
        """本地 SQLite 聚合查詢（COUNT／MIN／MAX，非逐列讀取），即使累積到 ~200 天、
        全市場資料量也只是毫秒等級，跟其他分頁的本地資料讀取一樣直接同步呼叫，
        不需要另外開背景執行緒。"""
        status = get_history_status(app_paths.HISTORY_DB_PATH)
        self.history_status_label.setText(_format_history_status(status))
