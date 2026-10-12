"""個股／部位「詳細資訊」區塊：8 張籌碼趨勢圖、技術分析、文字摘要與延遲建立的 DetailChartPanel。

StockDetailDialog 與「部位紀錄」頁共用。"""

import html

import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from tradingnote_technical import calculate_indicators

from ui.charts.base import (
    apply_chart_theme,
    clear_zoom_limits,
    empty_state,
    fit_series,
    limit_zoom_to_data,
    line,
    ref_line,
    series_color,
    set_axis_label,
    set_date_ticks,
    set_title,
)
from ui.stock_charts import (
    ComparisonWidget,
    LazyTabBuilder,
    StockChart,
    chart_page,
)
from ui.theme import (
    COLOR_GAIN,
    COLOR_LOSS,
    COLOR_MUTED,
)


def _detail_summary_scroll(label):
    """限制摘要佔用高度，長資料保留捲動閱讀，避免擠壓圖表。"""
    label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
    label.setTextFormat(QtCore.Qt.RichText)
    label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
    scroll = QtWidgets.QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
    scroll.setFixedHeight(110)
    scroll.setWidget(label)
    return scroll


def _cumulative(series):
    running_total, result = 0, []
    for net in series:
        running_total += net
        result.append(running_total)
    return result


def _start_series_chart(chart, dates):
    """時間序列圖共用的開頭：記住日期（給 StockChart 點擊查值用）、設日期刻度，
    回傳 x 座標。"""
    if isinstance(chart, StockChart):
        chart.dates = list(dates)
    set_date_ticks(chart, dates)
    return list(range(len(dates)))


def _populate_price_chart(chart, price_history):
    """歷史股價走勢圖：price_history 是 fetch_stock_price_history() 的回傳格式
    （list of {"date":, "close":, ...}，由舊到新排序），跟其他幾張圖吃的
    {"dates":, "series":/"vpt":/"mfi":} dict 格式不同，這裡直接吃 list。只畫
    收盤價（使用者要的是「歷史股價資訊圖」，不是另外疊漲跌%／成交量，避免跟
    其他幾張圖一樣的軸混在一起）。點擊查值走 StockChart 共用的虛線＋readout。"""
    chart.clear()
    if not price_history:
        return

    dates = [row["date"] for row in price_history]
    closes = [row["close"] for row in price_history]
    x = _start_series_chart(chart, dates)
    line(chart, x, closes, "收盤價")
    set_axis_label(chart, "left", "收盤價（元）")
    set_title(chart, f"歷史股價｜近 {len(dates)} 個交易日")
    fit_series(chart, x, closes)


def _populate_cumulative_flow_chart(chart, history, title):
    """逐日淨買賣超累加成累計曲線、加一條零軸參考線。"""
    chart.clear()
    if history is None or not history["dates"]:
        return

    dates = history["dates"]
    x = _start_series_chart(chart, dates)
    all_y = [0]  # 包含 0，讓零軸參考線不會被縮出視野邊界外
    for index, (label, series) in enumerate(history["series"].items()):
        cumulative = _cumulative(series)
        all_y.extend(cumulative)
        line(chart, x, cumulative, label, index=index)

    ref_line(chart, y=0)
    set_axis_label(chart, "left", "累計淨買賣超（股）")
    set_title(chart, f"{title}｜近 {len(dates)} 個交易日")
    fit_series(chart, x, all_y)


def _populate_flow_chart(chart, history):
    _populate_cumulative_flow_chart(chart, history, "三大法人累計買賣超")


def _populate_institutional_detail_chart(chart, detail_history):
    """法人分別累計買賣超：跟 _populate_flow_chart（合併三大類）同一種畫法，
    但畫的是五個細項各自一條線（外資／外資自營商／投信／自營商(自行)／
    自營商(避險)，見 tradingnote_finmind.INSTITUTIONAL_DETAIL_BUCKETS），讓
    使用者看得出「哪一類法人在持續進出」。detail_history 是
    fetch_institutional_investors_detailed_history() 的回傳值。"""
    _populate_cumulative_flow_chart(chart, detail_history, "法人分別累計買賣超")


def _populate_margin_chart(chart, margin_history):
    """融資融券餘額趨勢圖：資料本來就是「餘額」（TodayBalance），不是逐日
    買賣超流量，所以直接畫原始值，不能再累加一次。"""
    chart.clear()
    if margin_history is None or not margin_history["dates"]:
        return

    dates = margin_history["dates"]
    x = _start_series_chart(chart, dates)
    all_y = []
    for index, (label, series) in enumerate(margin_history["series"].items()):
        all_y.extend(series)
        line(chart, x, series, label, index=index)

    set_axis_label(chart, "left", "餘額（張）")
    set_title(chart, f"融資融券餘額｜近 {len(dates)} 個交易日")
    fit_series(chart, x, all_y)


def _mark_latest_value(chart, x, dates, series, color, value_fmt="{:.1f}"):
    """在圖上用水平虛線＋左側文字標出最後一筆資料的數值與日期，方便一眼看到
    目前值，不用把滑鼠移到線的最右端去對。"""
    latest_y = series[-1]
    latest_label = f"{value_fmt.format(latest_y)}｜{dates[-1]}"
    ref_line(chart, y=latest_y, color=color)
    text = pg.TextItem(latest_label, color=color, anchor=(0, 0.5))
    text.setPos(min(x), latest_y)
    chart.addItem(text)


def _populate_vpt_chart(chart, vpt_mfi_history):
    chart.clear()
    if vpt_mfi_history is None or not vpt_mfi_history["dates"]:
        return

    dates = vpt_mfi_history["dates"]
    x = _start_series_chart(chart, dates)
    series = vpt_mfi_history["vpt"]
    line(chart, x, series, "VPT")
    set_axis_label(chart, "left", "VPT")
    set_title(chart, f"VPT 量價趨勢｜近 {len(dates)} 個交易日")
    fit_series(chart, x, series)
    _mark_latest_value(chart, x, dates, series, series_color("VPT"), "{:,.0f}")


def _populate_mfi_chart(chart, vpt_mfi_history):
    """MFI 值域固定在 0～100，圖上加 80／20 兩條參考線（超買／超賣，MFI
    標準慣例）。"""
    chart.clear()
    if vpt_mfi_history is None or not vpt_mfi_history["dates"]:
        return

    dates = vpt_mfi_history["dates"]
    x = _start_series_chart(chart, dates)
    series = vpt_mfi_history["mfi"]
    line(chart, x, series, "MFI")
    ref_line(chart, y=80, color=COLOR_LOSS)
    ref_line(chart, y=20, color=COLOR_GAIN)
    set_axis_label(chart, "left", "MFI")
    set_title(chart, f"MFI 資金流量｜近 {len(dates)} 個交易日")
    limit_zoom_to_data(chart, x, series + [0, 100])
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(0, 100, padding=0.02)
    _mark_latest_value(chart, x, dates, series, series_color("MFI"), "{:.1f}")


def _populate_single_series_chart(chart, data, name, unit_label, title):
    chart.clear()
    if data is None or not data.get("dates"):
        return

    dates = data["dates"]
    x = _start_series_chart(chart, dates)
    series = data["series"][name]
    line(chart, x, series, name)
    set_axis_label(chart, "left", unit_label)
    set_title(chart, f"{title}｜近 {len(dates)} 個交易日")
    fit_series(chart, x, series)


def _populate_short_sale_balance_chart(chart, sbl_balance):
    """借券賣出餘額（股）趨勢圖：跟 _populate_margin_chart 一樣是「餘額」，直接
    畫原始值。單位是股，跟融資融券頁籤的「張」不是同一個量級，所以是獨立分頁。"""
    _populate_single_series_chart(chart, sbl_balance, "借券賣出餘額", "餘額（股）", "借券賣出餘額")


def _populate_lending_volume_chart(chart, lending):
    """借券成交量（張）趨勢圖：當日流量，跟借券賣出餘額（累積餘額、單位股）
    性質不同，所以分開兩張圖。"""
    _populate_single_series_chart(chart, lending, "借券成交量", "成交量（張）", "借券成交量")


def _plot_technical_series(chart, series, index, name):
    values = [value for value in (series or ()) if value is not None]
    if not values:
        return []
    line(
        chart,
        list(range(len(series))),
        [float("nan") if value is None else value for value in series],
        name,
        index=index,
        connect="finite",
    )
    return values


def _populate_technical_chart(chart, technical_data, mode="kd"):
    """繪製本地計算的 KD／MACD／均線圖。"""
    chart.clear()
    if not technical_data or not technical_data.get("dates"):
        empty_state(chart, "尚無足夠歷史價格資料可計算技術指標", title="技術分析")
        return

    dates = technical_data["dates"]
    if isinstance(chart, StockChart):
        chart.dates = list(dates)
    spec = technical_data.get("charts", {}).get(mode)
    if not spec:
        set_title(chart, "此快取尚無指標，請重新載入資料")
        return
    values = []
    for index, (name, series) in enumerate(spec["series"].items()):
        values += _plot_technical_series(chart, series, index, name)
    for level in spec["levels"]:
        ref_line(chart, y=level)
    set_title(chart, spec["title"])
    set_axis_label(chart, "left", spec["unit"])
    set_date_ticks(chart, dates, max_labels=6)
    x = list(range(max(2, len(dates))))
    if values:
        lo, hi = min(values), max(values)
        y_floor = max(abs(lo) * .02, 1) if lo == hi else 1e-9
        limit_zoom_to_data(chart, x, values, y_floor=y_floor)
        pad = y_floor if lo == hi else (hi - lo) * .1
        chart.setXRange(0, max(1, len(dates) - 1), padding=.02)
        chart.setYRange(lo - pad, hi + pad, padding=0)
    else:
        clear_zoom_limits(chart)
        chart.setXRange(0, max(1, len(dates) - 1), padding=.02)
        chart.setYRange(0, 1)
        empty = pg.TextItem("資料不足：缺少必要欄位或尚未滿計算期數", color=COLOR_MUTED)
        chart.addItem(empty)
        empty.setPos(0, .5)


class TechnicalAnalysisWidget(QtWidgets.QWidget):
    """個股明細共用的技術分析視圖。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("指標："))
        self.mode_combo = QtWidgets.QComboBox()
        catalog = calculate_indicators([dict(date="sample", close=1)])["charts"]
        for key, spec in catalog.items():
            self.mode_combo.addItem(spec["title"], key)
        self.mode_combo.currentIndexChanged.connect(self._refresh_chart)
        controls.addWidget(self.mode_combo)
        self.range_combo = QtWidgets.QComboBox()
        for title, count in (("全部歷史", 0), ("近60筆", 60), ("近120筆", 120), ("近240筆", 240)):
            self.range_combo.addItem(title, count)
        self.range_combo.currentIndexChanged.connect(self._refresh_chart)
        controls.addWidget(self.range_combo)
        self.latest_label = QtWidgets.QLabel("")
        self.latest_label.setProperty("muted", True)
        self.latest_label.setWordWrap(True)
        layout.addWidget(self.latest_label)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.chart = StockChart()
        apply_chart_theme(self.chart)
        layout.addWidget(chart_page(self.chart), 1)
        self._technical_data = None

    def set_data(self, technical_data):
        self._technical_data = technical_data
        self._refresh_chart()

    def _refresh_chart(self):
        mode = self.mode_combo.currentData()
        data = self._technical_data
        count = self.range_combo.currentData()
        if data and count:
            data = {**data, "dates": data["dates"][-count:], "charts": {
                key: {**spec, "series": {name: values[-count:] for name, values in spec["series"].items()}}
                for key, spec in data.get("charts", {}).items()
            }}
        _populate_technical_chart(self.chart, data, mode)
        self.latest_label.setText(self._latest_text(mode))

    def _latest_text(self, mode):
        data = self._technical_data
        if not data:
            return "資料不足"
        spec = data.get("charts", {}).get(mode, {})
        latest = "　".join(f"{name} {self._last(series)}" for name, series in spec.get("series", {}).items())
        dates = data.get("dates", [])
        return f"{dates[0]} ～ {dates[-1]}｜{len(dates)} 筆日線｜{latest}" if dates else "資料不足"

    @staticmethod
    def _last(values):
        value = values[-1] if values else None
        return f"{value:.2f}" if value is not None else "N/A"


def _technical_comparison_sources(technical_data):
    """把 calculate_indicators() 的全部 24 類指標轉成「雙資料比較」
    （ComparisonWidget）可選的來源。技術分析分頁畫面上一次只會顯示使用者選取
    的其中一種指標，但比較功能要能選到全部 24 種，所以直接從計算結果取序列，
    不透過畫在 TechnicalAnalysisWidget 上的那份曲線。每個指標類別各自成一組
    （組名帶類別標題），不是全部塞進同一組「技術分析」——不同類別剛好用了
    同樣的序列名稱很常見（例如「均線」跟「布林通道」都有「收盤」），同一組
    會互相蓋掉，分開組別才能保留全部 24 種都選得到。"""
    if not technical_data or not technical_data.get("dates"):
        return []
    dates = technical_data["dates"]
    sources = []
    for spec in technical_data.get("charts", {}).values():
        unit = spec["unit"]
        series_list = [
            (name, unit, {day: value for day, value in zip(dates, values) if value is not None})
            for name, values in spec["series"].items()
        ]
        sources.append((f"技術分析－{spec['title']}", series_list))
    return sources


def _price_chart_series(price_history):
    """比照 `_populate_price_chart` 的畫法（只有一條「收盤價」曲線），直接從
    原始 `price_history` 算出「雙資料比較」可用的序列，不依賴股價圖 widget
    是否已經建立／populate 過——延遲分頁（見 `ui.stock_charts.LazyTabBuilder`）
    下，「雙資料比較」分頁可能在使用者還沒切到「歷史股價」分頁前就被打開。"""
    if not price_history:
        return []
    values = {row["date"]: float(row["close"]) for row in price_history if row.get("close") is not None}
    return [("收盤價", "收盤價（元）", values)]


def _flow_chart_series(history):
    """比照 `_populate_flow_chart` 的累計買賣超算法。"""
    if history is None or not history["dates"]:
        return []
    dates = history["dates"]
    result = []
    for label, series in history["series"].items():
        cumulative = {}
        running_total = 0
        for day, net in zip(dates, series):
            running_total += net
            cumulative[day] = float(running_total)
        result.append((label, "累計淨買賣超（股）", cumulative))
    return result


def _institutional_detail_chart_series(detail_history):
    """比照 `_populate_institutional_detail_chart` 的累計買賣超算法。"""
    if detail_history is None or not detail_history["dates"]:
        return []
    dates = detail_history["dates"]
    result = []
    for label, series in detail_history["series"].items():
        cumulative = {}
        running_total = 0
        for day, net in zip(dates, series):
            running_total += net
            cumulative[day] = float(running_total)
        result.append((label, "累計淨買賣超（股）", cumulative))
    return result


def _margin_chart_series(margin_history):
    """比照 `_populate_margin_chart`：餘額直接取原始值，不能再累加一次。"""
    if margin_history is None or not margin_history["dates"]:
        return []
    dates = margin_history["dates"]
    return [
        (label, "餘額（張）", {day: float(value) for day, value in zip(dates, series)})
        for label, series in margin_history["series"].items()
    ]


def _vpt_chart_series(vpt_mfi_history):
    if vpt_mfi_history is None or not vpt_mfi_history["dates"]:
        return []
    dates = vpt_mfi_history["dates"]
    values = {day: float(value) for day, value in zip(dates, vpt_mfi_history["vpt"])}
    return [("VPT", "VPT", values)]


def _mfi_chart_series(vpt_mfi_history):
    if vpt_mfi_history is None or not vpt_mfi_history["dates"]:
        return []
    dates = vpt_mfi_history["dates"]
    values = {day: float(value) for day, value in zip(dates, vpt_mfi_history["mfi"])}
    return [("MFI", "MFI", values)]


def _short_sale_balance_chart_series(sbl_balance):
    if sbl_balance is None or not sbl_balance.get("dates"):
        return []
    dates = sbl_balance["dates"]
    series = sbl_balance["series"]["借券賣出餘額"]
    return [("借券賣出餘額", "餘額（股）", {day: float(value) for day, value in zip(dates, series)})]


def _lending_volume_chart_series(lending):
    if lending is None or not lending.get("dates"):
        return []
    dates = lending["dates"]
    series = lending["series"]["借券成交量"]
    return [("借券成交量", "成交量（張）", {day: float(value) for day, value in zip(dates, series)})]


def _render_detail_summary(label, header, data, note=None):
    """畫「個股籌碼面詳細資訊」文字摘要（不含圖表——圖表交給
    `DetailChartPanel.set_data()`，見下方）。data 是 fetch_position_detail() 的
    回傳值（不管是剛查到的，還是 position_detail_cache.json 讀出來的上次結果，
    shape 都相同，見 tradingnote_finmind.POSITION_DETAIL_FIELDS）；「部位紀錄」
    頁跟「個股」頁的 StockDetailDialog 共用這份畫面邏輯，畫在各自傳入的
    label 上。note 非 None 時插在 header 下面一行，用來標示「這是上次的快取，
    背景更新中」或「背景更新失敗，顯示上次結果」。"""
    valuation = data["valuation"]
    history = data["institutional_history"]
    margin_history = data["margin_history"]
    foreign_shareholding = data["foreign_shareholding"]
    lending = data["lending"]
    suspension = data["suspension"]
    sbl_balance = data.get("sbl_short_balance")

    lines = [header] if header else []
    if note:
        lines.append(note)
    lines.append("")

    if valuation is None:
        lines.append("FinMind 基本面：查無資料")
    else:
        per = valuation["per"]
        pbr = valuation["pbr"]
        yield_pct = valuation["dividend_yield"]
        lines.append(
            f"FinMind 基本面（{valuation['date']}）　本益比：{per if per is not None else 'N/A'}　"
            f"股價淨值比：{pbr if pbr is not None else 'N/A'}　"
            f"殖利率：{yield_pct if yield_pct is not None else 'N/A'}%"
        )

    if history is None or not history["dates"]:
        lines.append("三大法人買賣超：查無資料")
    else:
        latest_date = history["dates"][-1]
        lines.append(f"三大法人買賣超（最新 {latest_date}，單位：股）")
        for label_name, series in history["series"].items():
            lines.append(f"　{label_name}：淨買超 {series[-1]:+,}")

    if margin_history is None:
        lines.append("融資融券：查無資料")
    else:
        latest = margin_history["latest"]
        lines.append(
            f"融資融券（最新 {latest['date']}，單位：張）　"
            f"融資餘額：{latest['margin_balance']:,}（{latest['margin_change']:+,}）　"
            f"融券餘額：{latest['short_balance']:,}（{latest['short_change']:+,}）"
        )

    margin_cost = data.get("margin_cost_estimate")
    if margin_cost is None:
        lines.append("融資成本（估算）：查無資料")
    else:
        lines.append(
            f"融資成本（估算，{margin_cost['date']}）：約 {margin_cost['cost']:.2f} 元　"
            "※非券商真實成本，僅由歷史融資量反推近似值"
        )

    if sbl_balance is None:
        lines.append("借券賣出餘額：查無資料")
    else:
        lines.append(
            f"借券賣出餘額（{sbl_balance['date']}，單位：股）："
            f"{sbl_balance['balance']:,}（{sbl_balance['change']:+,}）"
        )

    if foreign_shareholding is None or foreign_shareholding["ratio"] is None:
        lines.append("外資持股比例：查無資料")
    else:
        change = foreign_shareholding["change"]
        change_text = f"（{change:+.2f}pp）" if change is not None else ""
        lines.append(
            f"外資持股比例（{foreign_shareholding['date']}）："
            f"{foreign_shareholding['ratio']:.2f}%{change_text}"
        )

    if lending is None or not lending["volume"]:
        lines.append("借券成交：查無資料")
    else:
        fee_text = (
            f"，均費率 {lending['avg_fee_rate']:.2f}%"
            if lending["avg_fee_rate"] is not None
            else ""
        )
        lines.append(f"借券成交（{lending['date']}）：合計 {lending['volume']:,} 張{fee_text}")

    if suspension:
        for event in suspension:
            lines.append(
                f"⚠ 停資停券公告　{event['date']} ～ {event['end_date'] or '未提供'}　"
                f"原因：{event['reason'] or '未提供'}"
            )

    # QLabel 的簡單 Rich Text 可增加段落層級，又不需要額外建立大量 widget。
    # 這個摘要在「部位紀錄」與個股彈窗共用，因此保留原有內容，只改善掃讀性。
    html_lines = []
    for index, line in enumerate(lines):
        if not line:
            continue
        safe = html.escape(line).replace("　", "&nbsp;&nbsp;")
        if index == 0 and header:
            html_lines.append(f"<div style='font-weight:600'>{safe}</div>")
        elif note and line == note:
            html_lines.append(f"<div style='color:{COLOR_MUTED};margin:3px 0 7px'>{safe}</div>")
        elif line.startswith("　"):
            html_lines.append(f"<div style='margin-left:14px'>{safe}</div>")
        elif line.startswith("⚠"):
            html_lines.append(f"<div style='color:{COLOR_LOSS};margin-top:5px'>{safe}</div>")
        else:
            html_lines.append(f"<div style='margin-top:4px'>{safe}</div>")
    label.setTextFormat(QtCore.Qt.RichText)
    label.setText("".join(html_lines))


# (tab_label, comparison_group_label, data_key, populate_fn, series_fn)：
# DetailChartPanel 的 8 張明細圖分頁共用這份對照表，series_fn 給「雙資料比較」
# 分頁用，直接從原始資料算序列，不依賴對應的 StockChart 是否已經建立。
_DETAIL_CHART_SPECS = (
    ("歷史股價", "股價", "price_history", _populate_price_chart, _price_chart_series),
    ("三大法人", "三大法人累計", "institutional_history", _populate_flow_chart, _flow_chart_series),
    ("法人分別", "法人分別累計", "institutional_detail_history",
     _populate_institutional_detail_chart, _institutional_detail_chart_series),
    ("融資融券", "融資融券", "margin_history", _populate_margin_chart, _margin_chart_series),
    ("VPT", "VPT", "vpt_mfi_history", _populate_vpt_chart, _vpt_chart_series),
    ("MFI", "MFI", "vpt_mfi_history", _populate_mfi_chart, _mfi_chart_series),
    ("借券賣出餘額", "借券餘額", "sbl_short_balance",
     _populate_short_sale_balance_chart, _short_sale_balance_chart_series),
    ("借券成交", "借券成交", "lending", _populate_lending_volume_chart, _lending_volume_chart_series),
)


class DetailChartPanel:
    """「個股完整籌碼」（StockDetailDialog）／「部位紀錄」詳細資訊共用的
    8 張明細圖＋技術分析＋雙資料比較，10 個分頁全部用 LazyTabBuilder 延遲
    建立：分頁標籤／順序在建構時就看得到，但每個分頁背後的 StockChart／
    ComparisonWidget／TechnicalAnalysisWidget 直到第一次被切到才真正建立＋
    populate；已建立過的分頁物件永久保留（不重建），只有內容會在 set_data()
    之後、下次被切到時重新 populate。「雙資料比較」分頁直接從 set_data() 存
    的原始資料算序列（見 _DETAIL_CHART_SPECS 的 series_fn／
    _technical_comparison_sources），不依賴其他分頁的 StockChart 是否已經
    建立過，所以使用者可以在完全沒造訪過其他 8 個分頁的情況下直接打開「雙
    資料比較」也看得到完整資料。"""

    def __init__(self):
        self.tabs = QtWidgets.QTabWidget()
        self._lazy = LazyTabBuilder(self.tabs)
        self._data = None
        self._technical_cache = None  # (data, technical_data)，data 換了才重算
        self._chart_states = []  # 對應 _DETAIL_CHART_SPECS，每項 {"chart": StockChart|None}
        for tab_label, _group_label, data_key, populate_fn, _series_fn in _DETAIL_CHART_SPECS:
            state = {"chart": None}
            self._chart_states.append(state)
            self._lazy.add_tab(tab_label, self._make_chart_build(state, data_key, populate_fn))
        self._technical_state = {"widget": None}
        self._lazy.add_tab("技術分析", self._build_technical_tab)
        self._comparison_state = {"widget": None}
        self._lazy.add_tab("雙資料比較", self._build_comparison_tab)

    def _make_chart_build(self, state, data_key, populate_fn):
        def build(container):
            chart = state["chart"]
            if chart is None:
                chart = StockChart()
                apply_chart_theme(chart)
                container.layout().addWidget(chart_page(chart))
                state["chart"] = chart
            populate_fn(chart, (self._data or {}).get(data_key))
        return build

    def _technical_data(self):
        if self._data is None:
            return None
        if self._technical_cache is not None and self._technical_cache[0] is self._data:
            return self._technical_cache[1]
        rows = self._data.get("price_history") or []
        technical_data = calculate_indicators([
            dict(date=r["date"], open=r.get("open"), max=r.get("high"), min=r.get("low"),
                 close=r.get("close"), Trading_Volume=r.get("volume"),
                 Trading_money=r.get("trading_value"))
            for r in rows
        ]) or self._data.get("technical_indicators")
        self._technical_cache = (self._data, technical_data)
        return technical_data

    def _build_technical_tab(self, container):
        widget = self._technical_state["widget"]
        if widget is None:
            widget = TechnicalAnalysisWidget()
            container.layout().addWidget(widget)
            self._technical_state["widget"] = widget
        widget.set_data(self._technical_data())

    def _build_comparison_tab(self, container):
        widget = self._comparison_state["widget"]
        if widget is None:
            widget = ComparisonWidget()
            container.layout().addWidget(widget)
            self._comparison_state["widget"] = widget
        data = self._data or {}
        sources = [
            (group_label, series_fn(data.get(data_key)))
            for _tab_label, group_label, data_key, _populate_fn, series_fn in _DETAIL_CHART_SPECS
        ]
        sources.extend(_technical_comparison_sources(self._technical_data()))
        widget.set_sources(sources)

    def set_data(self, data):
        """換股票／換部位／背景查詢回來新資料時呼叫：更新內部資料、把全部
        分頁標成「下次被切到時要重新 populate」，並立刻重新 populate 目前
        作用中的分頁——其餘分頁維持延遲，等使用者真的切過去才會用新資料
        重新 populate。data 是 None 時（例如未選取任何部位）目前分頁會顯示
        對應的「查無資料」空狀態，跟原本 _populate_*_chart(chart, None) 的
        行為一致。"""
        self._data = data
        self._technical_cache = None
        self._lazy.reset()
        self._lazy.activate_current()

    def clear(self):
        self.set_data(None)
