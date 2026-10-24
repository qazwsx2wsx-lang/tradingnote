"""部位新增／編輯設定、新增成交、單檔查價對話框。

「部位」＝目前 OPEN 的交易（tradingnote_tradejournal.py）。新增部位之後，
股數／均價只能透過「新增成交」調整，不能直接編輯——編輯設定只改策略／停損／
停利／風險金額這些「計畫」欄位。"""

from datetime import date, datetime

from PySide6 import QtWidgets

from tradingnote_core import lookup_price
from tradingnote_history import get_latest_ticker_record
from tradingnote_tradejournal import create_strategy

from ui import app_paths
from ui.widgets import DialogBase, accent_button


class PositionFormDialog(DialogBase):
    """新增部位（開一筆新交易）。strategies 是目前既有策略清單（Strategy
    物件），下拉選單可選「(無)」或其中一個；旁邊「+」按鈕可直接新增策略並
    重新整理選單。新增之後代號／方向不可變更，股數／均價要調整請用「新增
    成交」，不是回來編輯這個表單——這裡只負責「開倉」那一刻的資料。"""

    def __init__(self, parent, on_submit, strategies, db_path):
        super().__init__(parent)
        self.setWindowTitle("新增部位")
        self.on_submit = on_submit
        self.db_path = db_path

        form = QtWidgets.QFormLayout()
        form.setSpacing(8)

        self.ticker_edit = QtWidgets.QLineEdit()
        self.ticker_edit.setMinimumWidth(200)
        form.addRow("代號", self.ticker_edit)

        self.side_combo = QtWidgets.QComboBox()
        self.side_combo.addItems(["LONG", "SHORT"])
        form.addRow("方向", self.side_combo)

        self.shares_edit = QtWidgets.QLineEdit()
        form.addRow("股數", self.shares_edit)

        self.entry_price_edit = QtWidgets.QLineEdit()
        form.addRow("成本價", self.entry_price_edit)

        self.entry_date_edit = QtWidgets.QLineEdit()
        self.entry_date_edit.setText(date.today().isoformat())
        form.addRow("日期 (YYYY-MM-DD)", self.entry_date_edit)

        self.stop_loss_edit = QtWidgets.QLineEdit()
        form.addRow("初始停損價 (必填)", self.stop_loss_edit)

        self.take_profit_edit = QtWidgets.QLineEdit()
        form.addRow("初始停利價", self.take_profit_edit)

        self.risk_amount_edit = QtWidgets.QLineEdit()
        form.addRow("預計風險金額 (1R)", self.risk_amount_edit)

        strategy_row = QtWidgets.QHBoxLayout()
        self.strategy_combo = QtWidgets.QComboBox()
        self.strategy_combo.setMinimumWidth(160)
        self._load_strategies(strategies)
        strategy_row.addWidget(self.strategy_combo)
        add_strategy_button = QtWidgets.QPushButton("+")
        add_strategy_button.setMaximumWidth(28)
        add_strategy_button.clicked.connect(self._add_strategy)
        strategy_row.addWidget(add_strategy_button)
        form.addRow("策略", strategy_row)

        self.note_edit = QtWidgets.QLineEdit()
        form.addRow("備註", self.note_edit)

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

    def _load_strategies(self, strategies):
        self.strategy_combo.clear()
        self.strategy_combo.addItem("(無)", None)
        for strategy in strategies:
            self.strategy_combo.addItem(strategy.name, strategy.strategy_id)

    def _add_strategy(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "新增策略", "策略名稱")
        if not ok or not name.strip():
            return
        description, ok = QtWidgets.QInputDialog.getText(self, "新增策略", "描述 (可留空)")
        if not ok:
            description = ""
        try:
            strategy_id = create_strategy(self.db_path, name.strip(), description.strip() or None)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "錯誤", str(e))
            return
        self.strategy_combo.addItem(name.strip(), strategy_id)
        self.strategy_combo.setCurrentIndex(self.strategy_combo.count() - 1)

    @staticmethod
    def _parse_optional_float(text):
        text = text.strip()
        return None if not text else float(text)

    def _submit(self):
        ticker = self.ticker_edit.text().strip()
        try:
            shares = int(self.shares_edit.text())
            entry_price = float(self.entry_price_edit.text())
            initial_stop_loss = float(self.stop_loss_edit.text())
            initial_take_profit = self._parse_optional_float(self.take_profit_edit.text())
            planned_risk_amount = self._parse_optional_float(self.risk_amount_edit.text())
        except ValueError:
            QtWidgets.QMessageBox.critical(
                self, "錯誤", "股數需為整數；成本價、初始停損價、停利價與風險金額需為數字。"
            )
            return

        try:
            self.on_submit(
                ticker=ticker,
                shares=shares,
                entry_price=entry_price,
                entry_date=self.entry_date_edit.text().strip(),
                initial_stop_loss=initial_stop_loss,
                side=self.side_combo.currentText(),
                note=self.note_edit.text().strip(),
                strategy_id=self.strategy_combo.currentData(),
                initial_take_profit=initial_take_profit,
                planned_risk_amount=planned_risk_amount,
            )
        except ValueError as e:
            QtWidgets.QMessageBox.critical(self, "錯誤", str(e))
            return
        self.accept()


class PositionPlanFormDialog(DialogBase):
    """編輯既有部位的「計畫」欄位：策略／停損／停利／風險金額。股數、均價、
    備註都不在這裡改——備註留給部位詳細面板的自動保存欄位（避免跟這個表單
    的 on_submit 同時寫同一欄，無法判斷哪個贏）；股數/均價要改請用新增成交。"""

    def __init__(self, parent, on_submit, strategies, db_path, trade):
        super().__init__(parent)
        self.setWindowTitle(f"編輯設定：{trade.symbol}")
        self.on_submit = on_submit
        self.db_path = db_path

        form = QtWidgets.QFormLayout()
        form.setSpacing(8)

        strategy_row = QtWidgets.QHBoxLayout()
        self.strategy_combo = QtWidgets.QComboBox()
        self.strategy_combo.setMinimumWidth(160)
        self._load_strategies(strategies, trade.strategy_id)
        strategy_row.addWidget(self.strategy_combo)
        add_strategy_button = QtWidgets.QPushButton("+")
        add_strategy_button.setMaximumWidth(28)
        add_strategy_button.clicked.connect(self._add_strategy)
        strategy_row.addWidget(add_strategy_button)
        form.addRow("策略", strategy_row)

        self.stop_loss_edit = QtWidgets.QLineEdit(str(trade.initial_stop_loss))
        form.addRow("初始停損價 (必填)", self.stop_loss_edit)

        self.take_profit_edit = QtWidgets.QLineEdit(
            "" if trade.initial_take_profit is None else str(trade.initial_take_profit)
        )
        form.addRow("初始停利價", self.take_profit_edit)

        self.risk_amount_edit = QtWidgets.QLineEdit(
            "" if trade.planned_risk_amount is None else str(trade.planned_risk_amount)
        )
        form.addRow("預計風險金額 (1R)", self.risk_amount_edit)

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

    def _load_strategies(self, strategies, current_strategy_id):
        self.strategy_combo.clear()
        self.strategy_combo.addItem("(無)", None)
        for strategy in strategies:
            self.strategy_combo.addItem(strategy.name, strategy.strategy_id)
        index = self.strategy_combo.findData(current_strategy_id)
        if index >= 0:
            self.strategy_combo.setCurrentIndex(index)

    def _add_strategy(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "新增策略", "策略名稱")
        if not ok or not name.strip():
            return
        description, ok = QtWidgets.QInputDialog.getText(self, "新增策略", "描述 (可留空)")
        if not ok:
            description = ""
        try:
            strategy_id = create_strategy(self.db_path, name.strip(), description.strip() or None)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "錯誤", str(e))
            return
        self.strategy_combo.addItem(name.strip(), strategy_id)
        self.strategy_combo.setCurrentIndex(self.strategy_combo.count() - 1)

    @staticmethod
    def _parse_optional_float(text):
        text = text.strip()
        return None if not text else float(text)

    def _submit(self):
        try:
            initial_stop_loss = float(self.stop_loss_edit.text())
            initial_take_profit = self._parse_optional_float(self.take_profit_edit.text())
            planned_risk_amount = self._parse_optional_float(self.risk_amount_edit.text())
        except ValueError:
            QtWidgets.QMessageBox.critical(self, "錯誤", "初始停損價、停利價與風險金額需為數字。")
            return

        try:
            self.on_submit(
                strategy_id=self.strategy_combo.currentData(),
                initial_stop_loss=initial_stop_loss,
                initial_take_profit=initial_take_profit,
                planned_risk_amount=planned_risk_amount,
            )
        except ValueError as e:
            QtWidgets.QMessageBox.critical(self, "錯誤", str(e))
            return
        self.accept()


class ExecutionFormDialog(DialogBase):
    """為選定的部位新增一筆成交記錄（分批進出場明細）。"""

    def __init__(self, parent, on_submit):
        super().__init__(parent)
        self.setWindowTitle("新增成交")
        self.on_submit = on_submit

        form = QtWidgets.QFormLayout()
        form.setSpacing(8)

        self.action_combo = QtWidgets.QComboBox()
        self.action_combo.addItems(["BUY", "SELL"])
        form.addRow("買賣", self.action_combo)

        self.price_edit = QtWidgets.QLineEdit()
        form.addRow("價格", self.price_edit)

        self.qty_edit = QtWidgets.QLineEdit()
        form.addRow("數量", self.qty_edit)

        self.commission_edit = QtWidgets.QLineEdit()
        self.commission_edit.setText("0")
        form.addRow("手續費", self.commission_edit)

        self.slippage_edit = QtWidgets.QLineEdit()
        self.slippage_edit.setText("0")
        form.addRow("滑價", self.slippage_edit)

        self.executed_at_edit = QtWidgets.QLineEdit()
        self.executed_at_edit.setText(datetime.now().isoformat(timespec="seconds"))
        form.addRow("成交時間", self.executed_at_edit)

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
            price = float(self.price_edit.text())
            qty = float(self.qty_edit.text())
            commission = float(self.commission_edit.text() or "0")
            slippage = float(self.slippage_edit.text() or "0")
        except ValueError:
            QtWidgets.QMessageBox.critical(self, "錯誤", "價格、數量、手續費與滑價需為數字。")
            return

        executed_at = self.executed_at_edit.text().strip() or datetime.now().isoformat(
            timespec="seconds"
        )

        try:
            self.on_submit(
                action=self.action_combo.currentText(),
                price=price,
                qty=qty,
                commission=commission,
                slippage=slippage,
                executed_at=executed_at,
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
