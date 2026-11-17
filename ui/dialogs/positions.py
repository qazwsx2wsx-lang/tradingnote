"""部位新增／編輯設定、新增成交、單檔查價對話框。

「部位」＝目前 OPEN 的交易（tradingnote_tradejournal.py）。新增部位之後，
股數／均價只能透過「新增成交」調整，不能直接編輯——編輯設定只改策略／停損／
停利／風險金額這些「計畫」欄位。"""

from datetime import date, datetime

from PySide6 import QtWidgets

from tradingnote_core import lookup_price
from tradingnote_history import get_latest_ticker_record
from tradingnote_tradejournal import INSTRUMENT_SSF, INSTRUMENT_STOCK, create_strategy, ssf_tax

from ui import app_paths
from ui.widgets import DialogBase, accent_button, show_error

# (顯示文字, instrument, 每口股數)
_INSTRUMENT_CHOICES = (
    ("股票", INSTRUMENT_STOCK, None),
    ("個股期 標準型（2000 股／口）", INSTRUMENT_SSF, 2000),
    ("個股期 小型（100 股／口）", INSTRUMENT_SSF, 100),
)


def default_contract_month(today=None):
    """近月合約：今天還沒過本月第三個星期三就是本月，否則下個月（YYYYMM）。"""
    from tradingnote_futures_backtest import third_wednesday

    today = today or date.today()
    month = f"{today.year:04d}{today.month:02d}"
    if today <= third_wednesday(month):
        return month
    year, mon = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return f"{year:04d}{mon:02d}"


def lookup_futures_code(stock_id, contract_size):
    """從期交所個股期標的清單（tradingnote_taifex）查期貨代號；查不到回傳空字串。"""
    try:
        import tradingnote_taifex as taifex

        return taifex.lookup_ssf_code(
            app_paths.FUTURES_SSF_CACHE_PATH, stock_id, mini=contract_size == 100
        ) or ""
    except Exception:
        return ""


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
        self._form = form

        self.instrument_combo = QtWidgets.QComboBox()
        for label, instrument, size in _INSTRUMENT_CHOICES:
            self.instrument_combo.addItem(label, (instrument, size))
        form.addRow("商品", self.instrument_combo)

        self.ticker_edit = QtWidgets.QLineEdit()
        self.ticker_edit.setMinimumWidth(200)
        self.ticker_edit.setPlaceholderText("股票代號；個股期也填標的股票代號，如 2330")
        self.ticker_edit.editingFinished.connect(self._fill_futures_code)
        form.addRow("代號", self.ticker_edit)

        self.contract_month_edit = QtWidgets.QLineEdit(default_contract_month())
        self.contract_month_edit.setPlaceholderText("YYYYMM")
        form.addRow("合約月份", self.contract_month_edit)

        self.futures_code_edit = QtWidgets.QLineEdit()
        self.futures_code_edit.setPlaceholderText("如 CDF；What-if 抓期貨日 K 用，可留空")
        form.addRow("期貨代號", self.futures_code_edit)

        self.side_combo = QtWidgets.QComboBox()
        self.side_combo.addItems(["LONG", "SHORT"])
        form.addRow("方向", self.side_combo)

        self.shares_edit = QtWidgets.QLineEdit()
        form.addRow("股數", self.shares_edit)

        self.entry_price_edit = QtWidgets.QLineEdit()
        form.addRow("成本價", self.entry_price_edit)

        self.commission_edit = QtWidgets.QLineEdit("0")
        form.addRow("手續費", self.commission_edit)

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

        self.instrument_combo.currentIndexChanged.connect(self._on_instrument_changed)
        self._on_instrument_changed()

    def _instrument(self):
        return self.instrument_combo.currentData()

    def _is_ssf(self):
        return self._instrument()[0] == INSTRUMENT_SSF

    def _on_instrument_changed(self, *_args):
        is_ssf = self._is_ssf()
        for widget in (self.contract_month_edit, self.futures_code_edit, self.commission_edit):
            self._form.setRowVisible(widget, is_ssf)
        self._form.labelForField(self.shares_edit).setText("口數" if is_ssf else "股數")
        self._form.labelForField(self.entry_price_edit).setText("成交價" if is_ssf else "成本價")
        self._form.labelForField(self.stop_loss_edit).setText(
            "初始停損價（可留空）" if is_ssf else "初始停損價 (必填)"
        )
        self.futures_code_edit.clear()
        self._fill_futures_code()
        self.adjustSize()

    def _fill_futures_code(self):
        if not self._is_ssf() or self.futures_code_edit.text().strip():
            return
        ticker = self.ticker_edit.text().strip().upper()
        if ticker:
            self.futures_code_edit.setText(lookup_futures_code(ticker, self._instrument()[1]))

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
            show_error(self, "錯誤", str(e), level="critical")
            return
        self.strategy_combo.addItem(name.strip(), strategy_id)
        self.strategy_combo.setCurrentIndex(self.strategy_combo.count() - 1)

    @staticmethod
    def _parse_optional_float(text):
        text = text.strip()
        return None if not text else float(text)

    def _submit(self):
        ticker = self.ticker_edit.text().strip()
        is_ssf = self._is_ssf()
        try:
            shares = int(self.shares_edit.text())
            entry_price = float(self.entry_price_edit.text())
            if is_ssf:
                initial_stop_loss = self._parse_optional_float(self.stop_loss_edit.text()) or 0.0
                commission = float(self.commission_edit.text() or "0")
            else:
                initial_stop_loss = float(self.stop_loss_edit.text())
                commission = 0.0
            initial_take_profit = self._parse_optional_float(self.take_profit_edit.text())
            planned_risk_amount = self._parse_optional_float(self.risk_amount_edit.text())
        except ValueError:
            show_error(
                self, "錯誤",
                "股數／口數需為整數；價格、停損價、停利價、風險金額與手續費需為數字。",
                level="critical",
            )
            return

        extra = {}
        if is_ssf:
            month = self.contract_month_edit.text().strip().replace("/", "")
            if len(month) != 6 or not month.isdigit() or not 1 <= int(month[4:]) <= 12:
                QtWidgets.QMessageBox.critical(self, "錯誤", "合約月份格式應為 YYYYMM，例如 202611。")
                return
            extra = dict(
                instrument=INSTRUMENT_SSF,
                contract_size=self._instrument()[1],
                futures_code=self.futures_code_edit.text().strip().upper() or None,
                contract_month=month,
                commission=commission,
            )

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
                **extra,
            )
        except ValueError as e:
            show_error(self, "錯誤", str(e), level="critical")
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
            show_error(self, "錯誤", str(e), level="critical")
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
            show_error(self, "錯誤", "初始停損價、停利價與風險金額需為數字。", level="critical")
            return

        try:
            self.on_submit(
                strategy_id=self.strategy_combo.currentData(),
                initial_stop_loss=initial_stop_loss,
                initial_take_profit=initial_take_profit,
                planned_risk_amount=planned_risk_amount,
            )
        except ValueError as e:
            show_error(self, "錯誤", str(e), level="critical")
            return
        self.accept()


class ExecutionFormDialog(DialogBase):
    """為選定的部位新增一筆成交記錄（分批進出場明細）。trade 是個股期時數量
    改稱口數，並多一欄期交稅（依價格×口數自動帶入，可手動改）。"""

    def __init__(self, parent, on_submit, trade=None):
        super().__init__(parent)
        self.setWindowTitle("新增成交")
        self.on_submit = on_submit
        self.trade = trade
        self.is_ssf = trade is not None and trade.instrument == INSTRUMENT_SSF

        form = QtWidgets.QFormLayout()
        form.setSpacing(8)

        self.action_combo = QtWidgets.QComboBox()
        self.action_combo.addItems(["BUY", "SELL"])
        form.addRow("買賣", self.action_combo)

        self.price_edit = QtWidgets.QLineEdit()
        form.addRow("價格", self.price_edit)

        self.qty_edit = QtWidgets.QLineEdit()
        form.addRow("口數" if self.is_ssf else "數量", self.qty_edit)

        self.commission_edit = QtWidgets.QLineEdit()
        self.commission_edit.setText("0")
        form.addRow("手續費", self.commission_edit)

        self.tax_edit = None
        if self.is_ssf:
            self.tax_edit = QtWidgets.QLineEdit()
            self.tax_edit.setPlaceholderText("自動計算（十萬分之二）")
            form.addRow("期交稅", self.tax_edit)
            self.price_edit.textChanged.connect(self._update_tax)
            self.qty_edit.textChanged.connect(self._update_tax)

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

    def _update_tax(self, *_args):
        try:
            price = float(self.price_edit.text())
            qty = float(self.qty_edit.text())
        except ValueError:
            return
        self.tax_edit.setText(f"{ssf_tax(price, self.trade.contract_size, qty):.0f}")

    def _submit(self):
        try:
            price = float(self.price_edit.text())
            qty = float(self.qty_edit.text())
            commission = float(self.commission_edit.text() or "0")
            slippage = float(self.slippage_edit.text() or "0")
            extra = {}
            if self.tax_edit is not None and self.tax_edit.text().strip():
                extra["tax"] = float(self.tax_edit.text())
        except ValueError:
            show_error(self, "錯誤", "價格、數量、手續費、期交稅與滑價需為數字。", level="critical")
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
                **extra,
            )
        except ValueError as e:
            show_error(self, "錯誤", str(e), level="critical")
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
