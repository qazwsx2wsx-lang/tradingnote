"""只允許選有歷史資料交易日的日曆元件與日期選擇對話框。"""

from PySide6 import QtCore, QtGui, QtWidgets

from ui.theme import COLOR_MUTED
from ui.widgets import DialogBase


class TradingCalendarWidget(QtWidgets.QCalendarWidget):
    """只允許選取「有歷史資料」的日期（valid_dates_iso，來自
    tradingnote_history.get_available_dates）：沒資料的日期文字反白（灰階），
    點下去也不會真的被選取——QCalendarWidget 本身沒有「單一日期停用」的原生
    API（setMinimumDate／setMaximumDate 只能框住整段區間頭尾），所以改成監聽
    clicked(QDate) 訊號，點到不在 valid_dates_iso 裡的日期時，把選取狀態復原回
    上一個有效日期，點到有效日期才會真的送出 dateChosen 訊號。"""

    dateChosen = QtCore.Signal(str)  # 選到有效日期時 emit ISO 字串（yyyy-MM-dd）

    def __init__(self, valid_dates_iso, parent=None):
        super().__init__(parent)
        self.setGridVisible(True)
        self.setVerticalHeaderFormat(QtWidgets.QCalendarWidget.NoVerticalHeader)

        valid_qdates = sorted(
            QtCore.QDate.fromString(d, "yyyy-MM-dd") for d in valid_dates_iso
        )
        self._valid_set = set(valid_qdates)
        self._last_valid = valid_qdates[-1] if valid_qdates else QtCore.QDate.currentDate()

        if valid_qdates:
            self.setMinimumDate(valid_qdates[0])
            self.setMaximumDate(valid_qdates[-1])
            muted_format = QtGui.QTextCharFormat()
            muted_format.setForeground(QtGui.QColor(COLOR_MUTED))
            d = valid_qdates[0]
            while d <= valid_qdates[-1]:
                if d not in self._valid_set:
                    self.setDateTextFormat(d, muted_format)
                d = d.addDays(1)
            self.setSelectedDate(self._last_valid)

        self.clicked.connect(self._on_clicked)

    def _on_clicked(self, qdate):
        if qdate in self._valid_set:
            self._last_valid = qdate
            self.dateChosen.emit(qdate.toString("yyyy-MM-dd"))
        else:
            # 灰階（沒有資料）的日期：復原成上一個有效選取，等同「不能選」。
            self.setSelectedDate(self._last_valid)


class TradingDateDialog(DialogBase):
    """資金流向頁「流向天數」／「資料區間」的日曆式起始日期選擇器，取代原本直接
    輸入天數的 QSpinBox。結束日固定是今天／最新資料（跟 compute_industry_flow 的
    avg_days 語意一致，永遠是「今天以前 N 個交易日」），使用者只選起始日；點到有
    資料的日期立刻套用並關閉視窗，不需要另外按確定。"""

    def __init__(self, parent, valid_dates_iso, title):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.selected_date = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)

        if not valid_dates_iso:
            layout.addWidget(QtWidgets.QLabel("尚無足夠歷史資料可選擇日期，請先回補歷史資料。"))
        else:
            calendar = TradingCalendarWidget(valid_dates_iso, self)
            calendar.dateChosen.connect(self._on_date_chosen)
            layout.addWidget(calendar)
            hint = QtWidgets.QLabel("反白（灰階）日期沒有歷史資料，無法選取；區間結束日固定是今天／最新資料。")
            hint.setProperty("muted", True)
            hint.setWordWrap(True)
            layout.addWidget(hint)

        close_btn = QtWidgets.QPushButton("關閉")
        close_btn.clicked.connect(self.reject)
        layout.addWidget(close_btn, alignment=QtCore.Qt.AlignRight)

    def _on_date_chosen(self, date_iso):
        self.selected_date = date_iso
        self.accept()
