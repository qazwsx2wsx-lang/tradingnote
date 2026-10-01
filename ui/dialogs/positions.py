"""部位新增／編輯與單檔查價對話框。"""

from datetime import date

from PySide6 import QtWidgets

from tradingnote_core import lookup_price
from tradingnote_history import get_latest_ticker_record

from ui import app_paths
from ui.widgets import DialogBase, accent_button


class PositionFormDialog(DialogBase):
    """新增／編輯部位共用同一個表單。position=None 是新增模式（欄位空白，日期
    預設今天）；傳入現有 Position 就是編輯模式（欄位預先帶入目前的值，代號欄位
    唯讀——改代號等於換了一檔股票，語意上該用刪除+新增，不是編輯既有部位）。"""

    def __init__(self, parent, on_submit, position=None):
        super().__init__(parent)
        self.setWindowTitle("編輯部位" if position is not None else "新增部位")
        self.on_submit = on_submit

        form = QtWidgets.QFormLayout()
        form.setSpacing(8)

        fields = [
            ("代號", "ticker"),
            ("股數", "shares"),
            ("成本價", "entry_price"),
            ("日期 (YYYY-MM-DD)", "entry_date"),
            ("備註", "note"),
        ]
        self.inputs = {}
        for label, key in fields:
            edit = QtWidgets.QLineEdit()
            edit.setMinimumWidth(200)
            if position is not None:
                edit.setText(str(getattr(position, key)))
                if key == "ticker":
                    edit.setReadOnly(True)
            elif key == "entry_date":
                edit.setText(date.today().isoformat())
            form.addRow(label, edit)
            self.inputs[key] = edit

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        ok_btn = accent_button("確認", self._submit)
        cancel_btn = QtWidgets.QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.addLayout(form)
        layout.addSpacing(8)
        layout.addLayout(btn_row)

    def _submit(self):
        try:
            shares = int(self.inputs["shares"].text())
            entry_price = float(self.inputs["entry_price"].text())
        except ValueError:
            QtWidgets.QMessageBox.critical(self, "錯誤", "股數需為整數、成本價需為數字。")
            return
        try:
            self.on_submit(
                self.inputs["ticker"].text(),
                shares,
                entry_price,
                self.inputs["entry_date"].text(),
                self.inputs["note"].text(),
            )
        except ValueError as e:
            QtWidgets.QMessageBox.critical(self, "錯誤", str(e))
            return
        self.accept()


class PriceLookupDialog(DialogBase):
    def __init__(self, parent, snapshot):
        super().__init__(parent)
        self.setWindowTitle("查價")
        self.snapshot = snapshot

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("股票代號"))
        self.edit = QtWidgets.QLineEdit()
        self.edit.setMinimumWidth(140)
        self.edit.returnPressed.connect(self._lookup)
        row.addWidget(self.edit)

        self.result_label = QtWidgets.QLabel("")
        self.result_label.setWordWrap(True)
        self.result_label.setMinimumWidth(320)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.addLayout(row)
        layout.addWidget(self.result_label)
        layout.addWidget(accent_button("查詢", self._lookup))

        self.edit.setFocus()

    def _lookup(self):
        ticker = self.edit.text().strip()
        price = lookup_price(ticker, self.snapshot)
        if price is not None:
            self.result_label.setText(
                f"{price.ticker} {price.name}（{price.market}）\n"
                f"收盤 {price.close}  漲跌 {price.change}\n"
                f"開 {price.open}  高 {price.high}  低 {price.low}\n"
                f"日期 {price.date}"
            )
            return

        # LIFO 備援：即時快照沒有這檔股票時，改向歷史資料庫要最新一筆（date DESC）。
        record = get_latest_ticker_record(app_paths.HISTORY_DB_PATH, ticker.upper())
        if record is None:
            self.result_label.setText(f"查無此股票代號：{ticker}")
            return
        self.result_label.setText(
            f"{ticker.upper()} {record['name']}（{record['market']}，取自歷史資料）\n"
            f"收盤 {record['close']}  成交量 {record['volume']}\n"
            f"日期 {record['date']}（非即時快照，來自歷史資料庫最新一筆）"
        )
