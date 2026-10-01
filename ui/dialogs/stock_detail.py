"""個股詳細資訊（FinMind 估值＋完整籌碼）與產業前十大成分股對話框。"""

from PySide6 import QtCore, QtGui, QtWidgets

from tradingnote_finmind import (
    fetch_institutional_investors,
    fetch_institutional_investors_history,
    fetch_margin_short_sale_history,
    fetch_position_detail,
    fetch_valuation,
    load_position_detail_cache,
    save_position_detail_cache,
)
from tradingnote_history import _change_pct
from tradingnote_technical import load_local_technical

from ui import app_paths
from ui.badges import _chip_momentum_badges, _populate_badge_row, _stock_trend_badges
from ui.charts.detail import (
    DetailChartPanel,
    TechnicalAnalysisWidget,
    _detail_summary_scroll,
    _render_detail_summary,
)
from ui.components.stat_card import StatCard
from ui.format import _format_fetched_at, gain_loss_color
from ui.theme import COLOR_MUTED
from ui.widgets import DialogBase, _center_on_screen, _screen_fit_size
from ui.workers import run_task_in_thread


class StockDetailDialog(DialogBase):
    """雙擊「個股」分頁裡的股票時彈出：查本益比／殖利率／股價淨值比，以及最新一個
    交易日的三大法人買賣超。上櫃（TPEX）股票的本益比／殖利率改查 TPEX 官方端點
    （不吃 FinMind 額度，見 tradingnote_finmind.fetch_valuation）；上市（TWSE）
    股票、以及不分市場的三大法人買賣超，仍查 FinMind。查詢在背景執行緒跑
    （run_task_in_thread），避免網路延遲卡住整個視窗。「顯示完整籌碼面資訊」
    按鈕另外提供跟「部位紀錄」頁選取部位時同一份資料（融資融券／外資持股／
    借券／停資停券／VPT／MFI／KD／MACD／均線／RSI，見 _on_show_full_detail），共用同一份
    position_detail_cache.json（key 是 ticker，不分是從部位紀錄還是這裡查
    的）、也共用 _render_detail_summary／DetailChartPanel 畫面邏輯；不點按鈕
    就不會多打完整籌碼查詢，避免瀏覽「個股」頁清單時無謂燒額度。"""

    def __init__(self, parent, ticker, name, finmind_token, market=None):
        super().__init__(parent)
        self.ticker = ticker
        self.name = name
        self.finmind_token = finmind_token
        self.market = market
        self.setWindowTitle(f"{ticker} {name}")
        self.setMinimumWidth(360)

        self.hero = QtWidgets.QFrame()
        self.hero.setProperty("stockHero", True)
        hero_layout = QtWidgets.QHBoxLayout(self.hero)
        hero_layout.setContentsMargins(16, 12, 16, 12)
        identity_layout = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel(f"{name}　{ticker}")
        title.setProperty("header", True)
        market_label = QtWidgets.QLabel(market or "市場未明")
        market_label.setProperty("muted", True)
        identity_layout.addWidget(title)
        identity_layout.addWidget(market_label)
        hero_layout.addLayout(identity_layout)
        hero_layout.addStretch(1)
        price = getattr(parent, "snapshot", {}).get(ticker)
        price_text = (
            f"{price.close:,.2f}" if price is not None and price.close is not None else "—"
        )
        change_pct = _change_pct(price) if price is not None else None
        change_text = f"{change_pct:+.2f}%" if change_pct is not None else "今日漲跌 —"
        status = None if change_pct is None else ("positive" if change_pct >= 0 else "negative")
        self.hero_stat = StatCard(bordered=False)
        self.hero_stat.value_label.setAlignment(QtCore.Qt.AlignRight)
        self.hero_stat.change_label.setAlignment(QtCore.Qt.AlignRight)
        self.hero_stat.set_value(price_text, change=change_text, status=status)
        hero_layout.addWidget(self.hero_stat)

        cached = load_position_detail_cache(app_paths.POSITION_DETAIL_CACHE_PATH, ticker) or {}
        technical = load_local_technical(app_paths.HISTORY_DB_PATH, ticker, cached.get("price_history"))

        trend_row = QtWidgets.QHBoxLayout()
        trend_row.setSpacing(6)
        _populate_badge_row(
            trend_row,
            _stock_trend_badges(technical),
            empty_text="歷史資料不足，暫無法判斷趨勢／動能／量能。",
        )

        self.chip_row = QtWidgets.QHBoxLayout()
        self.chip_row.setSpacing(6)
        _populate_badge_row(
            self.chip_row, [], empty_text="查詢中...（需設定 FinMind token 才有籌碼動能資料）"
        )

        self.status_label = QtWidgets.QLabel("查詢中...")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(QtCore.Qt.RichText)

        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(20, 20, 20, 16)
        self._layout.addWidget(self.hero)
        self._layout.addLayout(trend_row)
        self._layout.addWidget(self.status_label)
        self._layout.addLayout(self.chip_row)
        self.local_technical_widget = TechnicalAnalysisWidget()
        self.local_technical_widget.set_data(technical)
        self._layout.addWidget(self.local_technical_widget, 1)
        self.resize(900, 650)

        self.full_detail_button = QtWidgets.QPushButton("顯示完整籌碼面資訊（同部位紀錄）")
        self.full_detail_button.clicked.connect(self._on_show_full_detail)
        self._layout.addWidget(self.full_detail_button)
        self._full_detail_widgets_built = False

        close_btn = QtWidgets.QPushButton("關閉")
        close_btn.clicked.connect(self.accept)
        self._layout.addWidget(close_btn, alignment=QtCore.Qt.AlignRight)

        def fetch_both():
            return (
                fetch_valuation(ticker, finmind_token, market=market),
                fetch_institutional_investors(ticker, finmind_token),
                fetch_institutional_investors_history(ticker, finmind_token, lookback_days=60),
                fetch_margin_short_sale_history(ticker, finmind_token, lookback_days=20),
            )

        self._timer = run_task_in_thread(self, fetch_both, self._on_done, self._on_error)

    def _on_done(self, result):
        valuation, institutional, institutional_history, margin_history = result
        _populate_badge_row(
            self.chip_row,
            _chip_momentum_badges(institutional_history, margin_history),
            empty_text="籌碼資料不足（可能尚未設定 FinMind token，或該股票暫無資料）。",
        )
        metrics = []
        if valuation is None:
            valuation_date = "估值資料不足"
            metrics.extend((("PER", "—"), ("PBR", "—"), ("殖利率", "—")))
        else:
            per = valuation["per"]
            pbr = valuation["pbr"]
            yield_pct = valuation["dividend_yield"]
            valuation_date = f"估值日期 {valuation['date']}"
            metrics.extend(
                (
                    ("PER", f"{per:.2f}" if per is not None else "—"),
                    ("PBR", f"{pbr:.2f}" if pbr is not None else "—"),
                    ("殖利率", f"{yield_pct:.2f}%" if yield_pct is not None else "—"),
                )
            )
        metric_html = "".join(
            f"<td width='150'><span style='color:{COLOR_MUTED}'>{label}</span><br>"
            f"<span style='font-size:18px;font-weight:600'>{value}</span></td>"
            for label, value in metrics
        )
        if institutional is None:
            institution_html = "三大法人買賣超：資料不足"
        else:
            items = []
            for row in institutional["breakdown"]:
                color = gain_loss_color(row["net"])
                items.append(
                    f"<span style='color:{color}'>{row['label']} {row['net']:+,}</span>"
                )
            institution_html = (
                f"三大法人淨買賣（{institutional['date']}，股）　" + "　".join(items)
            )
        self.status_label.setText(
            f"<table cellspacing='4'><tr>{metric_html}</tr></table>"
            f"<div style='color:{COLOR_MUTED};margin-top:4px'>{valuation_date}</div>"
            f"<div style='margin-top:10px'>{institution_html}</div>"
        )
        self._notify_finmind_call()

    def _on_error(self, message):
        self.status_label.setText(
            f"查詢失敗：{message}\n\n"
            "可能原因：FinMind token 未設定或已失效、"
            "已超過免費額度，或該股票暫無此資料。"
        )
        _populate_badge_row(
            self.chip_row, [], empty_text="籌碼資料查詢失敗，暫無法判斷籌碼動能。"
        )
        self._notify_finmind_call()

    def _notify_finmind_call(self):
        update = getattr(self.parent(), "update_finmind_count_label", None)
        if update is not None:
            update()

    def _build_full_detail_widgets(self):
        """第一次按下「顯示完整籌碼面資訊」時才建立這些 widget，插在按鈕跟
        關閉鈕之間。文字摘要固定顯示在上方，圖表區塊用 DetailChartPanel
        （10 個分頁，延遲建立——見該類別docstring），不用像 QScrollArea 那樣
        把多張圖疊起來捲動瀏覽；同時把視窗放大到看得下內容的尺寸（初始只有
        一行狀態文字時不需要這麼大）。"""
        self.full_detail_label = QtWidgets.QLabel("")
        self.full_detail_label.setWordWrap(True)
        # 完整摘要已包含基本估值與法人資訊，展開後收起上方簡版以免重複並節省高度。
        self.status_label.setVisible(False)
        self.local_technical_widget.setVisible(False)

        self.full_detail_panel = DetailChartPanel()

        insert_at = self._layout.indexOf(self.full_detail_button) + 1
        self._layout.insertWidget(insert_at, _detail_summary_scroll(self.full_detail_label))
        self._layout.insertWidget(insert_at + 1, self.full_detail_panel.tabs, 1)
        self._full_detail_widgets_built = True
        width, height = _screen_fit_size(self, preferred_width=1100, preferred_height=820, ratio=0.9)
        self.setMinimumSize(min(640, width), min(300, height))
        self.resize(width, height)
        _center_on_screen(self)

    def _on_show_full_detail(self):
        if not self._full_detail_widgets_built:
            self._build_full_detail_widgets()

        self.full_detail_button.setEnabled(False)
        self.full_detail_button.setText("查詢中...")
        # 股票身分與即時價格已在上方 hero 區呈現，詳細摘要不再重複一次標題。
        header = ""

        # 跟「部位紀錄」頁 _load_position_detail 同一招：先顯示上次永久存下來
        # 的結果（有的話），背景照樣重打一次 FinMind 拿最新資料。
        cached = load_position_detail_cache(app_paths.POSITION_DETAIL_CACHE_PATH, self.ticker)
        if cached is not None:
            note = f"（上次查詢：{_format_fetched_at(cached['fetched_at'])}，背景更新中...）"
            _render_detail_summary(self.full_detail_label, header, cached, note)
            self.full_detail_panel.set_data(cached)
        else:
            self.full_detail_label.setText(f"{header}\n\nFinMind 查詢中...")
            self.full_detail_panel.clear()

        def fetch():
            data = fetch_position_detail(self.ticker, self.finmind_token, market=self.market)
            save_position_detail_cache(app_paths.POSITION_DETAIL_CACHE_PATH, self.ticker, data)
            return data

        self._full_detail_timer = run_task_in_thread(
            self,
            fetch,
            lambda result: self._on_full_detail_done(header, result),
            lambda message: self._on_full_detail_error(header, message, cached),
        )

    def _on_full_detail_done(self, header, data):
        self.full_detail_button.setEnabled(True)
        self.full_detail_button.setText("重新整理完整籌碼面資訊")
        _render_detail_summary(self.full_detail_label, header, data)
        self.full_detail_panel.set_data(data)
        self._notify_finmind_call()

    def _on_full_detail_error(self, header, message, cached):
        self.full_detail_button.setEnabled(True)
        # 跟部位紀錄頁一樣：有上次快取就繼續顯示它＋「背景更新失敗」提示，
        # 不要把已經在畫面上的舊資料清空。
        if cached is not None:
            self.full_detail_button.setText("重新整理完整籌碼面資訊")
            note = (
                f"（背景更新失敗：{message}；顯示上次查詢結果 "
                f"{_format_fetched_at(cached['fetched_at'])}）"
            )
            _render_detail_summary(self.full_detail_label, header, cached, note)
            self.full_detail_panel.set_data(cached)
        else:
            self.full_detail_button.setText("顯示完整籌碼面資訊（同部位紀錄）")
            self.full_detail_label.setText(
                f"{header}\n\nFinMind 查詢失敗：{message}\n\n"
                "可能原因：FinMind token 未設定或已失效、已超過免費額度，或該股票暫無此資料。"
            )
        self._notify_finmind_call()


class IndustryTopStocksDialog(DialogBase):
    """點擊「資金流向分析」頁的族群泡泡／熱度清單時彈出的小視窗，顯示該群組
    近 days 個交易日累積成交金額前 N 大成分股（市值資料的代理指標，見
    get_industry_top_stocks_range；N 不足時全部顯示），並附上近日成交量／均量／
    本益比。純本地資料（daily_prices／valuation_history 快取），不打任何
    API，開啟即顯示。days 來自資金流向頁最上方的共用期間，讓累積成交金額與
    近日均量都跟畫面上看到的分析口徑一致；本益比則取既有最新估值快取。"""

    def __init__(self, parent, group_name, top_stocks, days):
        super().__init__(parent)
        self.setWindowTitle(
            f"{group_name}　近 {days} 日成交金額前 {len(top_stocks)} 大成分股"
        )
        self.setMinimumWidth(420)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)

        if top_stocks:
            first = top_stocks[0]
            layout.addWidget(QtWidgets.QLabel(
                f"分析期間：{first.get('start_date', '')} ～ {first.get('end_date', '')}；"
                "1 日採當日漲跌，量比均量不含截止日。"
            ))
        table = QtWidgets.QTableWidget(len(top_stocks), 9)
        table.setHorizontalHeaderLabels(
            [
                "代號",
                "名稱",
                "現價",
                "累積漲跌%",
                "累積成交金額(億)",
                "近日量(張)",
                "均量(張)",
                "本益比",
                "EPS",
            ]
        )
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        for row, stock in enumerate(top_stocks):
            change_pct = stock["change_pct"]
            change_str = f"{change_pct:+.2f}%" if change_pct is not None else f"實際有 {stock.get('actual_days', 0)}／需要 {stock.get('required_days', days)} 個交易日（價格不足）"
            color = gain_loss_color(change_pct or 0)
            avg_volume = stock["avg_volume"]
            per = stock["per"]
            eps = stock["close"] / per if per and stock["close"] is not None else None
            values = [
                stock["ticker"],
                stock["name"] or "-",
                f"{stock['close']:.2f}" if stock["close"] is not None else "資料不足",
                change_str,
                f"{stock['trading_value'] / 1e8:,.2f}",
                f"{stock['today_volume'] / 1000:,.0f}" if stock["today_volume"] is not None else "-",
                f"{avg_volume / 1000:,.0f}" if avg_volume is not None else f"實際有 {stock.get('actual_volume_days', 0)}／需要 {stock.get('required_volume_days', days)} 個歷史交易日",
                f"{per:.2f}" if per is not None else "N/A",
                f"{eps:.2f}" if eps is not None else "N/A",
            ]
            for col, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                if col in (0, 2, 3, 4, 5, 6, 7, 8):
                    item.setTextAlignment(QtCore.Qt.AlignCenter)
                if col == 3 and change_pct is not None:
                    item.setForeground(QtGui.QColor(color))
                table.setItem(row, col, item)
        table.resizeColumnsToContents()
        _, height_cap = _screen_fit_size(self, preferred_height=760, ratio=0.8)
        table.setFixedHeight(min(height_cap, 36 + table.rowCount() * 30))
        layout.addWidget(table)

        close_btn = QtWidgets.QPushButton("關閉")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=QtCore.Qt.AlignRight)
        _center_on_screen(self)
