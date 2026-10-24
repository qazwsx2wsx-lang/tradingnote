"""啟動時的一次性搬遷視窗：把舊版 positions.json 的部位逐筆搬進 trades
資料表（見 tradingnote_tradejournal.migrate_legacy_position）。trades.
initial_stop_loss 是必填欄位，但舊部位從來沒有停損價，所以這裡逐筆請使用者
補填；全部成功才會把 positions.json 改名為 .bak，任何一筆失敗都保留原檔，
下次啟動會再次詢問。"""

from PySide6 import QtWidgets

from ui.widgets import DialogBase, accent_button

_COLUMNS = ["代號", "名稱", "股數", "成本價", "進場日期", "初始停損價 (必填)"]


class LegacyPositionMigrationDialog(DialogBase):
    """on_submit(rows) 收到 list[(Position, stop_loss)]，負責實際寫入
    trades 與把 positions.json 改名——這個對話框本身只負責收集輸入，
    跟本專案其他對話框的 on_submit 慣例一致。"""

    def __init__(self, parent, on_submit, legacy_positions):
        super().__init__(parent)
        self.setWindowTitle("搬遷舊版部位資料")
        self.on_submit = on_submit
        self.legacy_positions = legacy_positions

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)

        intro = QtWidgets.QLabel(
            "偵測到舊版「部位紀錄」資料（positions.json）。新版把部位跟交易紀錄"
            "合併，每筆部位都需要一個初始停損價，請為以下每筆部位補填後按「確認"
            "搬移」。搬移成功後，舊檔會改名為 positions.json.bak 並停用。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QtWidgets.QTableWidget(len(legacy_positions), len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.stop_loss_edits = []
        for row, position in enumerate(legacy_positions):
            values = (
                position.ticker, position.name or "-", str(position.shares),
                f"{position.entry_price:.2f}", position.entry_date,
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QtWidgets.QTableWidgetItem(value))
            edit = QtWidgets.QLineEdit()
            self.table.setCellWidget(row, len(_COLUMNS) - 1, edit)
            self.stop_loss_edits.append(edit)
        layout.addWidget(self.table, 1)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        later_btn = QtWidgets.QPushButton("稍後再搬遷")
        later_btn.clicked.connect(self.reject)
        btn_row.addWidget(later_btn)
        confirm_btn = accent_button("確認搬移", self._submit)
        btn_row.addWidget(confirm_btn)
        layout.addLayout(btn_row)

        self.resize(680, 360)

    def _submit(self):
        stop_losses = []
        for position, edit in zip(self.legacy_positions, self.stop_loss_edits):
            try:
                value = float(edit.text())
                if value <= 0:
                    raise ValueError
            except ValueError:
                QtWidgets.QMessageBox.critical(
                    self, "錯誤", f"{position.ticker} 的初始停損價必須是大於 0 的數字。"
                )
                return
            stop_losses.append(value)

        try:
            self.on_submit(list(zip(self.legacy_positions, stop_losses)))
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self, "搬遷失敗",
                f"搬遷過程發生錯誤，尚未標記為已完成，下次啟動會再次詢問：\n{e}",
            )
            return
        self.accept()
