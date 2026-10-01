"""「交易週誌」分頁（週曆卡片、每日日誌編輯與自動保存）。

以 mixin 形式併入 tradingnote_gui.TradingNoteWindow；方法直接使用視窗的 self 狀態。"""

from datetime import date, timedelta

from PySide6 import QtCore, QtGui, QtWidgets

from tradingnote_journal import load_week, save_journal_entry, save_portfolio_snapshot

from ui import app_paths
from ui.format import gain_loss_color
from ui.theme import (
    COLOR_ACCENT,
    COLOR_BORDER,
    COLOR_DISABLED_BG,
    COLOR_DISABLED_TEXT,
    COLOR_GAIN,
    COLOR_LOSS,
    COLOR_MUTED,
    COLOR_SELECTED_BG,
    COLOR_SURFACE,
)
from ui.widgets import _set_standard_icon, accent_button


class JournalTabMixin:
    """TradingNoteWindow 的「交易週誌」分頁方法。"""

    # ---------- 交易週誌 ----------

    def _build_journal_tab(self):
        layout = QtWidgets.QVBoxLayout(self.journal_tab)
        self.journal_layout = layout
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        today = date.today()
        self.journal_week_start = today - timedelta(days=today.weekday())
        self.journal_selected_date = today.isoformat()
        self.journal_days = {}
        self.journal_dirty = False
        self._journal_loading = False

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setSpacing(6)
        self.journal_previous_button = QtWidgets.QPushButton(
            "上一週", clicked=self._journal_previous_week
        )
        _set_standard_icon(
            self.journal_previous_button, QtWidgets.QStyle.SP_ArrowLeft, "查看上一週"
        )
        toolbar.addWidget(self.journal_previous_button)
        self.journal_today_button = QtWidgets.QPushButton("回到本週", clicked=self._journal_current_week)
        _set_standard_icon(
            self.journal_today_button, QtWidgets.QStyle.SP_DirHomeIcon, "回到本週"
        )
        toolbar.addWidget(self.journal_today_button)
        self.journal_next_button = QtWidgets.QPushButton("下一週", clicked=self._journal_next_week)
        _set_standard_icon(
            self.journal_next_button, QtWidgets.QStyle.SP_ArrowRight, "查看下一週"
        )
        toolbar.addWidget(self.journal_next_button)
        toolbar.addSpacing(12)
        self.journal_week_label = QtWidgets.QLabel()
        self.journal_week_label.setProperty("header", True)
        toolbar.addWidget(self.journal_week_label)
        toolbar.addStretch(1)
        self.journal_jump_label = QtWidgets.QLabel("跳到日期")
        toolbar.addWidget(self.journal_jump_label)
        self.journal_date_edit = QtWidgets.QDateEdit()
        self.journal_date_edit.setCalendarPopup(True)
        self.journal_date_edit.setDisplayFormat("yyyy-MM-dd")
        self.journal_date_edit.setMaximumDate(QtCore.QDate.currentDate())
        self.journal_date_edit.setDate(QtCore.QDate.currentDate())
        self.journal_date_edit.dateChanged.connect(self._journal_jump_to_date)
        toolbar.addWidget(self.journal_date_edit)
        layout.addLayout(toolbar)

        hint = QtWidgets.QLabel(
            "每日金額變化＝股數 ×（當日收盤－前一交易日收盤）；週一通常與前週五比較。"
            "這是整日價格變化，不代表盤中實際成交損益。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        cards_widget = QtWidgets.QWidget()
        cards_layout = QtWidgets.QGridLayout(cards_widget)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(6)
        self.journal_card_group = QtWidgets.QButtonGroup(self)
        self.journal_card_group.setExclusive(True)
        self.journal_cards = []
        for index in range(7):
            card = QtWidgets.QPushButton()
            card.setCheckable(True)
            card.setMinimumHeight(164)
            card.setMinimumWidth(92)
            card.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
            card.clicked.connect(
                lambda _checked, day_index=index: self._select_journal_day_index(day_index)
            )
            self.journal_card_group.addButton(card, index)
            self.journal_cards.append(card)
            cards_layout.addWidget(card, 0, index)
            cards_layout.setColumnStretch(index, 1)
        layout.addWidget(cards_widget)

        detail_splitter = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        holdings_panel = QtWidgets.QWidget()
        holdings_layout = QtWidgets.QVBoxLayout(holdings_panel)
        holdings_layout.setContentsMargins(0, 0, 6, 0)
        self.journal_detail_title = QtWidgets.QLabel()
        self.journal_detail_title.setProperty("header", True)
        holdings_layout.addWidget(self.journal_detail_title)
        self.journal_source_label = QtWidgets.QLabel()
        self.journal_source_label.setProperty("muted", True)
        self.journal_source_label.setWordWrap(True)
        holdings_layout.addWidget(self.journal_source_label)
        self.journal_holdings_table = QtWidgets.QTableWidget(0, 6)
        self.journal_holdings_table.setHorizontalHeaderLabels(
            ["代號", "名稱", "股數", "收盤", "漲跌%", "金額變化"]
        )
        self.journal_holdings_table.verticalHeader().setVisible(False)
        self.journal_holdings_table.setAlternatingRowColors(True)
        self.journal_holdings_table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        journal_header = self.journal_holdings_table.horizontalHeader()
        journal_header.setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        journal_header.setMinimumSectionSize(54)
        holdings_layout.addWidget(self.journal_holdings_table, 1)

        editor_panel = QtWidgets.QWidget()
        editor_layout = QtWidgets.QVBoxLayout(editor_panel)
        editor_layout.setContentsMargins(6, 0, 0, 0)
        editor_header = QtWidgets.QLabel("交易日誌")
        editor_header.setProperty("header", True)
        editor_layout.addWidget(editor_header)
        editor_hint = QtWidgets.QLabel("自由記錄交易心情、犯錯的地方與當日體悟。")
        editor_hint.setProperty("muted", True)
        editor_layout.addWidget(editor_hint)
        self.journal_note_edit = QtWidgets.QPlainTextEdit()
        self.journal_note_edit.setPlaceholderText(
            "今天做得如何？當下的心情是什麼？\n有哪些錯誤、值得保留的判斷或新的體悟？"
        )
        self.journal_note_edit.textChanged.connect(self._on_journal_note_changed)
        editor_layout.addWidget(self.journal_note_edit, 1)
        save_row = QtWidgets.QHBoxLayout()
        self.journal_save_status = QtWidgets.QLabel("")
        self.journal_save_status.setProperty("muted", True)
        save_row.addWidget(self.journal_save_status)
        save_row.addStretch(1)
        self.journal_save_button = accent_button("儲存日誌", self._save_journal_clicked)
        _set_standard_icon(
            self.journal_save_button, QtWidgets.QStyle.SP_DialogSaveButton, "儲存交易日誌 (Ctrl+S)"
        )
        save_row.addWidget(self.journal_save_button)
        editor_layout.addLayout(save_row)

        detail_splitter.addWidget(holdings_panel)
        detail_splitter.addWidget(editor_panel)
        detail_splitter.setStretchFactor(0, 3)
        detail_splitter.setStretchFactor(1, 2)
        layout.addWidget(detail_splitter, 1)

        save_shortcut = QtGui.QShortcut(QtGui.QKeySequence.Save, self.journal_tab)
        save_shortcut.activated.connect(self._save_journal_clicked)
        self._journal_save_shortcut = save_shortcut
        self._refresh_journal_week()

    @staticmethod
    def _journal_money(value):
        return "N/A" if value is None else f"{value:+,.0f}"

    def _refresh_journal_week(self):
        week = load_week(
            app_paths.HISTORY_DB_PATH,
            self.journal_week_start,
            self.positions,
        )
        self.journal_days = {day.date: day for day in week}
        end = self.journal_week_start + timedelta(days=6)
        if getattr(self, "_compact_layout", False):
            week_text = f"{self.journal_week_start:%m/%d}－{end:%m/%d}"
        else:
            week_text = f"{self.journal_week_start:%Y/%m/%d}－{end:%Y/%m/%d}"
        self.journal_week_label.setText(week_text)
        current_week = date.today() - timedelta(days=date.today().weekday())
        self.journal_next_button.setEnabled(self.journal_week_start < current_week)
        weekday_names = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")
        for index, (card, day) in enumerate(zip(self.journal_cards, week)):
            is_future = day.date > date.today().isoformat()
            card.setEnabled(not is_future)
            card.setProperty("journalDate", day.date)
            moves = sorted(
                (move for move in day.holdings if move.change_amount is not None),
                key=lambda move: abs(move.change_amount),
                reverse=True,
            )[:3]
            if is_future:
                summary = "未來日期"
            elif not day.market_open:
                summary = "休市／無行情"
            elif not day.holdings:
                summary = "無持股"
            else:
                prefix = "約 " if day.holding_source == "estimated" else ""
                summary = f"合計 {prefix}{self._journal_money(day.total_change_amount)}"
            lines = [weekday_names[index], day.date[5:], summary]
            lines.extend(
                f"{move.ticker} {move.change_pct:+.2f}% {move.change_amount:+,.0f}"
                for move in moves
            )
            first_note_line = next(
                (line.strip() for line in day.note.splitlines() if line.strip()), ""
            )
            if first_note_line:
                lines.append("✎ " + first_note_line[:22] + ("…" if len(first_note_line) > 22 else ""))
            card.setText("\n".join(lines))
            color = (
                COLOR_GAIN if day.total_change_amount is not None and day.total_change_amount > 0
                else COLOR_LOSS if day.total_change_amount is not None and day.total_change_amount < 0
                else COLOR_MUTED
            )
            checked = day.date == self.journal_selected_date
            card.setChecked(checked)
            card.setStyleSheet(
                "QPushButton { text-align:left; padding:9px; "
                f"font-size:{10 if getattr(self, '_compact_layout', False) else 11}px; "
                f"color:{color}; border:1px solid {COLOR_BORDER}; background:{COLOR_SURFACE}; }}"
                f"QPushButton:checked {{ border:2px solid {COLOR_ACCENT}; background:{COLOR_SELECTED_BG}; }}"
                f"QPushButton:disabled {{ background:{COLOR_DISABLED_BG}; color:{COLOR_DISABLED_TEXT}; }}"
            )
        if self.journal_selected_date not in self.journal_days:
            self.journal_selected_date = self.journal_week_start.isoformat()
        self._render_journal_day()

    def _render_journal_day(self):
        day = self.journal_days.get(self.journal_selected_date)
        if day is None:
            return
        selected_date = date.fromisoformat(day.date)
        self.journal_detail_title.setText(f"{day.date}　持股漲跌明細")
        if day.holding_source == "estimated":
            source = "估算持股：依目前持股與進場日回推，過去加減碼及已賣出股票可能未包含。"
        elif day.holding_source == "future":
            source = "未來日期不提供持股與日誌編輯。"
        elif day.snapshot_date == day.date:
            source = f"已記錄持股快照：{day.snapshot_date}"
        else:
            source = f"持股沿用最近快照：{day.snapshot_date or '尚無快照'}"
        if not day.market_open and day.holding_source != "future":
            source += "　｜　休市或尚無當日行情。"
        self.journal_source_label.setText(source)

        self.journal_holdings_table.setRowCount(len(day.holdings))
        for row, move in enumerate(day.holdings):
            values = (
                move.ticker,
                move.name or "-",
                f"{move.shares:,}",
                "N/A" if move.close is None else f"{move.close:.2f}",
                "N/A" if move.change_pct is None else f"{move.change_pct:+.2f}%",
                self._journal_money(move.change_amount),
            )
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if column in (0, 2, 3, 4, 5):
                    item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                if column in (4, 5) and move.change_amount is not None:
                    item.setForeground(
                        QtGui.QColor(gain_loss_color(move.change_amount))
                    )
                self.journal_holdings_table.setItem(row, column, item)

        self._journal_loading = True
        self.journal_note_edit.setPlainText(day.note)
        self._journal_loading = False
        editable = selected_date <= date.today()
        self.journal_note_edit.setEnabled(editable)
        self.journal_save_button.setEnabled(editable)
        self.journal_dirty = False
        self.journal_save_status.setText("已儲存" if day.note else "尚無日誌")

    def _select_journal_day_index(self, index):
        self._save_journal_if_dirty()
        self.journal_selected_date = (
            self.journal_week_start + timedelta(days=index)
        ).isoformat()
        self._refresh_journal_week()

    def _on_journal_note_changed(self):
        if self._journal_loading:
            return
        self.journal_dirty = True
        self.journal_save_status.setText("尚未儲存")

    def _save_journal_if_dirty(self):
        if not getattr(self, "journal_dirty", False):
            return
        if self.journal_selected_date > date.today().isoformat():
            return
        save_journal_entry(
            app_paths.HISTORY_DB_PATH,
            self.journal_selected_date,
            self.journal_note_edit.toPlainText(),
        )
        self.journal_dirty = False
        self.journal_save_status.setText("已儲存")

    def _save_journal_clicked(self):
        if self.journal_selected_date > date.today().isoformat():
            return
        if self.journal_selected_date == date.today().isoformat():
            save_portfolio_snapshot(app_paths.HISTORY_DB_PATH, self.journal_selected_date, self.positions)
        save_journal_entry(
            app_paths.HISTORY_DB_PATH,
            self.journal_selected_date,
            self.journal_note_edit.toPlainText(),
        )
        self.journal_dirty = False
        self._refresh_journal_week()
        self.journal_save_status.setText("已儲存")

    def _set_journal_week(self, target):
        self._save_journal_if_dirty()
        current_week = date.today() - timedelta(days=date.today().weekday())
        target = min(target, current_week)
        self.journal_week_start = target
        self.journal_selected_date = (
            date.today().isoformat() if target == current_week else target.isoformat()
        )
        blocker = QtCore.QSignalBlocker(self.journal_date_edit)
        self.journal_date_edit.setDate(
            QtCore.QDate.fromString(self.journal_selected_date, "yyyy-MM-dd")
        )
        del blocker
        self._refresh_journal_week()

    def _journal_previous_week(self):
        self._set_journal_week(self.journal_week_start - timedelta(days=7))

    def _journal_next_week(self):
        self._set_journal_week(self.journal_week_start + timedelta(days=7))

    def _journal_current_week(self):
        today = date.today()
        self._set_journal_week(today - timedelta(days=today.weekday()))

    def _journal_jump_to_date(self, qdate):
        target = date(qdate.year(), qdate.month(), qdate.day())
        self._save_journal_if_dirty()
        self.journal_week_start = target - timedelta(days=target.weekday())
        self.journal_selected_date = target.isoformat()
        self._refresh_journal_week()
