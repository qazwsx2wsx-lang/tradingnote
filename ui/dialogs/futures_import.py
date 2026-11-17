"""個股期成交 CSV 匯入對話框：選檔 → 預覽解析結果 → 確認匯入。

格式見 tradingnote_futures_backtest.CSV_COLUMNS（通用範本，可用「下載範本」取得）。
有任何一列解析失敗就不允許匯入——部分匯入會讓加碼／減碼配對錯位。"""

from PySide6 import QtWidgets

import tradingnote_futures_backtest as fb
from ui.widgets import DialogBase, accent_button

_PREVIEW_COLUMNS = ["行", "日期", "標的", "合約", "月份", "買賣", "口數", "價格", "手續費", "期交稅", "策略"]


def save_template(parent):
    path, _ = QtWidgets.QFileDialog.getSaveFileName(
        parent, "儲存 CSV 範本", "個股期成交範本.csv", "CSV (*.csv)"
    )
    if path:
        fb.write_template(path)
    return path


class FuturesImportDialog(DialogBase):
    def __init__(self, parent, db_path, on_imported=None, code_lookup=None):
        super().__init__(parent)
        self.setWindowTitle("匯入個股期成交 CSV")
        self.db_path = db_path
        self.on_imported = on_imported
        self.code_lookup = code_lookup
        self.rows = []
        self.import_result = None

        top = QtWidgets.QHBoxLayout()
        self.path_label = QtWidgets.QLabel("尚未選擇檔案")
        self.path_label.setProperty("muted", True)
        top.addWidget(self.path_label, 1)
        choose = QtWidgets.QPushButton("選擇檔案…")
        choose.clicked.connect(self._choose_file)
        top.addWidget(choose)
        template = QtWidgets.QPushButton("下載範本…")
        template.clicked.connect(lambda: save_template(self))
        top.addWidget(template)

        hint = QtWidgets.QLabel(
            "欄位：" + "、".join(fb.CSV_COLUMNS)
            + "。標的代號填股票代號（如 2330）；合約填「標準型」或「小型」；期交稅留空自動算。"
            "同一檔案重複匯入會自動跳過已匯入的列。"
        )
        hint.setWordWrap(True)
        hint.setProperty("muted", True)

        self.preview = QtWidgets.QTableWidget(0, len(_PREVIEW_COLUMNS))
        self.preview.setHorizontalHeaderLabels(_PREVIEW_COLUMNS)
        self.preview.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.preview.verticalHeader().setVisible(False)
        self.preview.setMinimumSize(760, 260)

        self.status_label = QtWidgets.QLabel("")
        self.status_label.setWordWrap(True)

        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        self.import_button = accent_button("匯入", self._import)
        self.import_button.setEnabled(False)
        buttons.addWidget(self.import_button)
        close = QtWidgets.QPushButton("關閉")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.addLayout(top)
        layout.addWidget(hint)
        layout.addWidget(self.preview, 1)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

    def _choose_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "選擇成交 CSV", "", "CSV (*.csv);;所有檔案 (*)"
        )
        if path:
            self.load_file(path)

    def load_file(self, path):
        self.path_label.setText(str(path))
        try:
            rows, errors = fb.parse_ssf_csv(path)
        except (OSError, ValueError) as exc:
            rows, errors = [], [(0, str(exc))]
        self.rows = rows
        self.preview.setRowCount(len(rows))
        for r, row in enumerate(rows):
            values = (
                row.line, row.executed_at, row.symbol,
                "標準型" if row.contract_size == 2000 else "小型",
                row.contract_month, "買" if row.action == "BUY" else "賣",
                f"{row.qty:g}", f"{row.price:g}", f"{row.commission:g}",
                "自動" if row.tax is None else f"{row.tax:g}", row.strategy or "",
            )
            for c, value in enumerate(values):
                self.preview.setItem(r, c, QtWidgets.QTableWidgetItem(str(value)))
        self.preview.resizeColumnsToContents()
        if errors:
            lines = [f"第 {line} 行：{message}" if line else message for line, message in errors[:10]]
            more = f"\n…另有 {len(errors) - 10} 個錯誤" if len(errors) > 10 else ""
            self.status_label.setText("無法匯入，請修正：\n" + "\n".join(lines) + more)
            self.import_button.setEnabled(False)
        elif not rows:
            self.status_label.setText("檔案裡沒有成交資料。")
            self.import_button.setEnabled(False)
        else:
            self.status_label.setText(f"解析成功：{len(rows)} 筆成交，按「匯入」寫入交易紀錄。")
            self.import_button.setEnabled(True)

    def _import(self):
        try:
            self.import_result = fb.import_ssf_rows(self.db_path, self.rows, code_lookup=self.code_lookup)
        except Exception as exc:  # noqa: BLE001 - show DB errors to the user
            QtWidgets.QMessageBox.critical(self, "匯入失敗", str(exc))
            return
        self.import_button.setEnabled(False)
        self.status_label.setText(
            f"已匯入 {self.import_result.added} 筆成交（新開 {self.import_result.trades_opened} 筆交易），"
            f"跳過已匯入過的 {self.import_result.skipped} 筆。"
        )
        if self.on_imported:
            self.on_imported(self.import_result)
