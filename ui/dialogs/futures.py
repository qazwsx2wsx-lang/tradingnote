"""期貨大額交易人未沖銷部位：表格／趨勢圖 helper 與對話框（期貨頁也共用 helper）。"""

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from ui.charts.detail import _limit_zoom_to_data
from ui.format import _format_settlement_month, _futures_date_to_iso, gain_loss_color
from ui.stock_charts import set_date_ticks
from ui.theme import COLOR_TEXT
from ui.widgets import DialogBase, _center_on_screen, _screen_fit_size


# 「大額交易人未沖銷部位」明細表欄位——「期貨」頁下方常駐面板（_build_futures_lt_panel）
# 與雙擊彈窗（FuturesLargeTradersDialog）共用同一組欄位與同一份填表邏輯
# （_populate_large_traders_table）。
LARGE_TRADERS_TABLE_COLUMNS = [
    "到期月份", "交易人類別", "前5大買", "前5大賣", "前10大買", "前10大賣",
    "前10大淨", "前10大買佔比", "全市場未沖銷",
]


def _populate_large_traders_table(table, groups):
    """把某商品的大額交易人未沖銷部位（get_large_traders_for_product 的回傳
    groups）填進 table（欄位須為 LARGE_TRADERS_TABLE_COLUMNS）。每個到期月份最多
    兩列（所有交易人／特定法人）。前10大淨＝前10大買－賣，正紅負綠；前10大買佔比
    ＝前10大買 ÷ 全市場未沖銷。回傳實際畫出的列數（0＝查無資料）。"""
    display_rows = []
    for group in groups:
        for label in ("所有交易人", "特定法人"):
            entry = group["by_type"].get(label)
            if entry is not None:
                display_rows.append((group, label, entry))

    table.setRowCount(len(display_rows))
    for row, (group, label, entry) in enumerate(display_rows):
        top10_net = (entry["top10_buy"] or 0) - (entry["top10_sell"] or 0)
        market_oi = entry["market_oi"]
        buy_share = (
            f"{(entry['top10_buy'] or 0) / market_oi * 100:.1f}%" if market_oi else "-"
        )
        values = [
            _format_settlement_month(group["settlement_month"]),
            label,
            f"{entry['top5_buy']:,}" if entry["top5_buy"] is not None else "-",
            f"{entry['top5_sell']:,}" if entry["top5_sell"] is not None else "-",
            f"{entry['top10_buy']:,}" if entry["top10_buy"] is not None else "-",
            f"{entry['top10_sell']:,}" if entry["top10_sell"] is not None else "-",
            f"{top10_net:+,}",
            buy_share,
            f"{market_oi:,}" if market_oi is not None else "-",
        ]
        for col, value in enumerate(values):
            item = QtWidgets.QTableWidgetItem(value)
            if col != 1:
                item.setTextAlignment(QtCore.Qt.AlignCenter)
            if col == 6:  # 前10大淨：正紅負綠
                item.setForeground(QtGui.QColor(gain_loss_color(top10_net)))
            table.setItem(row, col, item)
    return len(display_rows)


def _large_traders_summary_entry(groups, contract_month):
    """從某商品的大額交易人分組挑一筆代表值給「期貨」頁主表格的摘要欄用：優先取
    到期月份等於近月合約（contract_month）的那組，其次取「所有契約合計」，再不然
    第一組；回傳該組的「所有交易人」entry（沒有就 None）。"""
    if not groups:
        return None
    chosen = next((g for g in groups if g["settlement_month"] == contract_month), None)
    if chosen is None:
        chosen = next((g for g in groups if g["is_all_contracts"]), None)
    if chosen is None:
        chosen = groups[0]
    return chosen["by_type"].get("所有交易人")


def _populate_large_traders_trend(chart, series):
    """畫某商品「所有契約合計・所有交易人」前10大買方／賣方未沖銷部位的逐日趨勢
    （兩條線，跟 _populate_margin_chart 一樣直接畫原始「部位數（口）」、不累加）。
    series 是 tradingnote_taifex.get_large_traders_history_series 的回傳（歷史由
    背景回補累積，見 backfill_large_traders_history）；空的就清空圖並提示。
    以「所有契約合計」為序列而非近月，是因為近月合約每月換倉會造成序列斷點，
    所有契約合計才連續。"""
    chart.clear()
    if not series:
        chart.setTitle(
            "大額交易人未沖銷部位趨勢（尚無歷史：背景回補中，或本商品無此統計）",
            color=COLOR_TEXT,
            size="10pt",
        )
        return

    dates = [r["date"] for r in series]
    x = list(range(len(dates)))
    set_date_ticks(chart, dates)

    buy = [r["top10_buy"] or 0 for r in series]
    sell = [r["top10_sell"] or 0 for r in series]
    chart.plot(x, buy, pen=pg.mkPen("#1f77b4", width=2), name="前10大買方")
    chart.plot(x, sell, pen=pg.mkPen("#ff7f0e", width=2), name="前10大賣方")
    chart.setLabel("left", "未沖銷部位（口）", color=COLOR_TEXT)
    chart.setTitle(
        f"前10大交易人未沖銷部位趨勢（所有契約·所有交易人）｜近 {len(dates)} 日",
        color=COLOR_TEXT,
        size="10pt",
    )
    all_y = buy + sell
    _limit_zoom_to_data(chart, x, all_y)
    y_lo, y_hi = min(all_y), max(all_y)
    y_pad = max((y_hi - y_lo) * 0.1, 1.0)
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(y_lo - y_pad, y_hi + y_pad, padding=0)


class FuturesLargeTradersDialog(DialogBase):
    """雙擊「期貨」頁表格某商品時彈出的較大檢視：顯示該商品的「大額交易人未沖銷
    部位」（TAIFEX OpenInterestOfLargeTradersFutures）。內容跟「期貨」頁下方常駐
    的明細面板相同（共用 _populate_large_traders_table），只是彈窗版面更大、方便
    細看。資料由呼叫端從「期貨」頁重新整理時已抓好的全市場清單裡篩出（純本地、
    開啟即顯示，不另打 API）。"""

    def __init__(self, parent, product, groups):
        super().__init__(parent)
        title_name = groups[0]["contract_name"] if groups else ""
        data_date = _futures_date_to_iso(groups[0]["date"]) if groups else ""
        self.setWindowTitle(
            f"{product} {title_name}　大額交易人未沖銷部位"
            + (f"（{data_date}）" if data_date else "")
        )
        self.setMinimumWidth(560)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)

        hint = QtWidgets.QLabel(
            "單位：口數。「特定法人」為前述大額交易人中屬期交所公告特定法人者。"
            "「前10大淨」＝前10大買方－賣方未沖銷部位；「前10大買佔比」＝前10大"
            "買方未沖銷部位 ÷ 全市場未沖銷部位。資料來自 TAIFEX 官方盤後統計，"
            "每交易日更新一次。"
        )
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        table = QtWidgets.QTableWidget(0, len(LARGE_TRADERS_TABLE_COLUMNS))
        table.setHorizontalHeaderLabels(LARGE_TRADERS_TABLE_COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        row_count = _populate_large_traders_table(table, groups)
        table.resizeColumnsToContents()
        _, height_cap = _screen_fit_size(self, preferred_height=760, ratio=0.8)
        table.setFixedHeight(min(height_cap, 36 + max(row_count, 1) * 30))
        layout.addWidget(table)

        if row_count == 0:
            layout.addWidget(QtWidgets.QLabel("查無此商品的大額交易人資料。"))

        close_btn = QtWidgets.QPushButton("關閉")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn, alignment=QtCore.Qt.AlignRight)
        _center_on_screen(self)
