"""個股／部位「詳細資訊」區塊：8 張籌碼趨勢圖、技術分析、文字摘要與延遲建立的 DetailChartPanel。

StockDetailDialog 與「部位紀錄」頁共用。"""

import html

import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from tradingnote_technical import calculate_indicators

from ui.stock_charts import (
    ComparisonWidget,
    LazyTabBuilder,
    StockChart,
    apply_chart_theme,
    chart_page,
    set_date_ticks,
)
from ui.theme import (
    COLOR_ACCENT,
    COLOR_GAIN,
    COLOR_LOSS,
    COLOR_MUTED,
    COLOR_SPECIAL,
    COLOR_TEXT,
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


def _limit_zoom_to_data(chart, xs, ys, x_floor=1.0, y_floor=1.0):
    """限制往外縮的下限，最多縮到剛好看見全部資料為止，避免縮出一大片空白；
    放大則不受影響，仍可無限拉近。邊界抓資料範圍的 15% 當緩衝。"""
    vb = chart.getPlotItem().getViewBox()
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_pad = max((x_max - x_min) * 0.15, x_floor)
    y_pad = max((y_max - y_min) * 0.15, y_floor)
    vb.setLimits(
        xMin=x_min - x_pad,
        xMax=x_max + x_pad,
        yMin=y_min - y_pad,
        yMax=y_max + y_pad,
    )


def _populate_price_chart(chart, price_history):
    """歷史股價走勢圖：price_history 是 fetch_stock_price_history() 的回傳格式
    （list of {"date":, "close":, ...}，由舊到新排序），跟其他幾張圖吃的
    {"dates":, "series":/"vpt":/"mfi":} dict 格式不同，這裡直接吃 list。只畫
    收盤價（使用者要的是「歷史股價資訊圖」，不是另外疊漲跌%／成交量，避免跟
    其他幾張圖一樣的軸混在一起）。"""
    chart.clear()
    # chart.clear() 會把先前點擊留下的標記線／文字一併清掉，這裡順便重置追蹤
    # 用的屬性，避免 _on_price_chart_click 之後 removeItem 到已經不存在的物件。
    chart._click_marker_items = []
    chart._price_dates = None
    chart._price_closes = None
    if not price_history:
        return

    dates = [row["date"] for row in price_history]
    if isinstance(chart, StockChart):
        chart.dates = list(dates)
    closes = [row["close"] for row in price_history]
    chart._price_dates = dates
    chart._price_closes = closes
    x = list(range(len(dates)))
    set_date_ticks(chart, dates)

    chart.plot(x, closes, pen=pg.mkPen("#e377c2", width=2), name="收盤價")
    chart.setLabel("left", "收盤價（元）", color=COLOR_TEXT)
    chart.setTitle(f"歷史股價｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt")
    _limit_zoom_to_data(chart, x, closes)
    # 跟 _populate_vpt_chart 同樣的理由：這張圖也可能一開始被 QScrollArea 捲到
    # 看不見的地方，enableAutoRange() 算出來的範圍在 widget 還沒有真正版面
    # 尺寸時不可靠，改用算好的資料範圍直接 setRange。
    y_lo, y_hi = min(closes), max(closes)
    y_pad = max((y_hi - y_lo) * 0.1, 1.0)
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(y_lo - y_pad, y_hi + y_pad, padding=0)


def _setup_price_chart_click(chart):
    """幫「歷史股價」圖加上滑鼠點擊查看該日股價的功能：點圖上任一位置，找出
    最接近的交易日，畫一條垂直虛線＋文字標出「日期｜收盤價」。只在 PlotWidget
    建立時呼叫一次——scene 的訊號連線是永久的，不會因為 _populate_price_chart
    之後的 chart.clear() 而消失；_populate_price_chart 每次重繪時把最新的
    dates／closes 存到 chart 物件上（見該函式），這裡的 handler 讀取當下存的
    那份，不用另外傳參數，重新整理／切換股票後點擊仍然對得上目前顯示的資料。"""
    vb = chart.getPlotItem().getViewBox()

    def on_click(event):
        dates = getattr(chart, "_price_dates", None)
        closes = getattr(chart, "_price_closes", None)
        if not dates:
            return
        if not chart.getPlotItem().sceneBoundingRect().contains(event.scenePos()):
            return
        point = vb.mapSceneToView(event.scenePos())
        idx = round(point.x())
        idx = max(0, min(len(dates) - 1, idx))

        for item in getattr(chart, "_click_marker_items", []):
            chart.removeItem(item)

        marker_line = pg.InfiniteLine(
            pos=idx, angle=90, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine, width=1)
        )
        label = pg.TextItem(
            f"{dates[idx]}｜{closes[idx]:.2f}", color=COLOR_TEXT, anchor=(0.5, 1)
        )
        label.setPos(idx, closes[idx])
        chart.addItem(marker_line)
        chart.addItem(label)
        chart._click_marker_items = [marker_line, label]

    chart.scene().sigMouseClicked.connect(on_click)


def _populate_flow_chart(chart, history):
    chart.clear()
    if history is None or not history["dates"]:
        return

    dates = history["dates"]
    if isinstance(chart, StockChart):
        chart.dates = list(dates)
    x = list(range(len(dates)))
    set_date_ticks(chart, dates)

    bucket_colors = {"外資": "#1f77b4", "投信": "#2ca02c", "自營商": "#d62728"}
    all_y = [0]  # 包含 0，讓下面的零軸參考線不會被縮出視野邊界外
    for label, series in history["series"].items():
        cumulative = []
        running_total = 0
        for net in series:
            running_total += net
            cumulative.append(running_total)
        all_y.extend(cumulative)
        chart.plot(
            x,
            cumulative,
            pen=pg.mkPen(bucket_colors.get(label, COLOR_TEXT), width=2),
            name=label,
        )

    chart.addLine(y=0, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine, width=1))
    chart.setLabel("left", "累計淨買賣超（股）", color=COLOR_TEXT)
    chart.setTitle(f"三大法人累計買賣超｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt")
    _limit_zoom_to_data(chart, x, all_y)
    # 現在這張圖可能是 QTabWidget 裡目前沒被切到的分頁（隱藏、還沒有真正版面
    # 尺寸），這時 enableAutoRange() 算出來的範圍不可靠（同 _populate_price_chart
    # 的理由），改用算好的資料範圍直接 setRange。
    y_lo, y_hi = min(all_y), max(all_y)
    y_pad = max((y_hi - y_lo) * 0.1, 1.0)
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(y_lo - y_pad, y_hi + y_pad, padding=0)


def _populate_institutional_detail_chart(chart, detail_history):
    """法人分別累計買賣超：跟 _populate_flow_chart（合併三大類）完全同一種畫法
    ——逐日淨買賣超累加成累計曲線、加一條零軸參考線——但畫的是五個細項各自
    一條線（外資／外資自營商／投信／自營商(自行)／自營商(避險)，見
    tradingnote_finmind.INSTITUTIONAL_DETAIL_BUCKETS），讓使用者看得出「哪一
    類法人在持續進出」而不只是合併後的三大類。detail_history 是
    fetch_institutional_investors_detailed_history() 的回傳值（也就是
    fetch_position_detail() 存進 institutional_detail_history 的那份）。"""
    chart.clear()
    if detail_history is None or not detail_history["dates"]:
        return

    dates = detail_history["dates"]
    if isinstance(chart, StockChart):
        chart.dates = list(dates)
    x = list(range(len(dates)))
    set_date_ticks(chart, dates)

    detail_colors = {
        "外資": "#1f77b4",
        "外資自營商": "#17becf",
        "投信": "#2ca02c",
        "自營商(自行)": "#d62728",
        "自營商(避險)": "#ff7f0e",
    }
    all_y = [0]  # 包含 0，讓零軸參考線不會被縮出視野邊界外
    for label, series in detail_history["series"].items():
        cumulative = []
        running_total = 0
        for net in series:
            running_total += net
            cumulative.append(running_total)
        all_y.extend(cumulative)
        chart.plot(
            x,
            cumulative,
            pen=pg.mkPen(detail_colors.get(label, COLOR_TEXT), width=2),
            name=label,
        )

    chart.addLine(y=0, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine, width=1))
    chart.setLabel("left", "累計淨買賣超（股）", color=COLOR_TEXT)
    chart.setTitle(
        f"法人分別累計買賣超｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt"
    )
    _limit_zoom_to_data(chart, x, all_y)
    # 同 _populate_flow_chart：這張圖可能是 QTabWidget 裡目前沒被切到的隱藏分頁，
    # enableAutoRange() 算出來的範圍不可靠，改用算好的資料範圍直接 setRange。
    y_lo, y_hi = min(all_y), max(all_y)
    y_pad = max((y_hi - y_lo) * 0.1, 1.0)
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(y_lo - y_pad, y_hi + y_pad, padding=0)


def _populate_margin_chart(chart, margin_history):
    """融資融券餘額趨勢圖：跟 _populate_flow_chart 不同，這裡的資料本來就是
    「餘額」（TodayBalance），不是逐日買賣超流量，所以直接畫原始值，不能再
    累加一次（累加會變成「餘額的餘額」，數字沒有意義）。"""
    chart.clear()
    if margin_history is None or not margin_history["dates"]:
        return

    dates = margin_history["dates"]
    if isinstance(chart, StockChart):
        chart.dates = list(dates)
    x = list(range(len(dates)))
    set_date_ticks(chart, dates)

    series_colors = {"融資餘額": "#9467bd", "融券餘額": "#ff7f0e"}
    all_y = []
    for label, series in margin_history["series"].items():
        all_y.extend(series)
        chart.plot(
            x,
            series,
            pen=pg.mkPen(series_colors.get(label, COLOR_TEXT), width=2),
            name=label,
        )

    chart.setLabel("left", "餘額（張）", color=COLOR_TEXT)
    chart.setTitle(f"融資融券餘額｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt")
    _limit_zoom_to_data(chart, x, all_y)
    # 同 _populate_flow_chart：這張圖也可能是 QTabWidget 裡目前沒被切到的
    # 分頁，enableAutoRange() 不可靠，改用算好的資料範圍直接 setRange。
    y_lo, y_hi = min(all_y), max(all_y)
    y_pad = max((y_hi - y_lo) * 0.1, 1.0)
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(y_lo - y_pad, y_hi + y_pad, padding=0)


def _mark_latest_value(chart, x, dates, series, color, value_fmt="{:.1f}"):
    """在圖上用水平虛線＋左側文字標出最後一筆資料的數值與日期，方便一眼看到
    目前值，不用把滑鼠移到線的最右端去對。"""
    latest_y = series[-1]
    latest_label = f"{value_fmt.format(latest_y)}｜{dates[-1]}"
    chart.addLine(y=latest_y, pen=pg.mkPen(color, style=QtCore.Qt.DashLine, width=1))
    text = pg.TextItem(latest_label, color=color, anchor=(0, 0.5))
    text.setPos(min(x), latest_y)
    chart.addItem(text)


def _populate_vpt_chart(chart, vpt_mfi_history):
    chart.clear()
    if vpt_mfi_history is None or not vpt_mfi_history["dates"]:
        return

    dates = vpt_mfi_history["dates"]
    if isinstance(chart, StockChart):
        chart.dates = list(dates)
    x = list(range(len(dates)))
    set_date_ticks(chart, dates)

    series = vpt_mfi_history["vpt"]
    chart.plot(x, series, pen=pg.mkPen("#17becf", width=2), name="VPT")
    chart.setLabel("left", "VPT", color=COLOR_TEXT)
    chart.setTitle(f"VPT 量價趨勢｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt")
    _limit_zoom_to_data(chart, x, series)
    # 這個區塊可能一開始被 QScrollArea 捲到看不見的地方，widget 還沒有真正的
    # 版面尺寸時呼叫 enableAutoRange()／chart.autoRange() 算出來的範圍不可靠
    # （實測會卡在 ±1 附近的退化值）。直接用剛剛算好的資料範圍 setRange，不
    # 依賴 pyqtgraph 的自動偵測；y_pad 下限跟 _limit_zoom_to_data 的 y_floor
    # 同樣抓 1.0，避免 VPT 全程沒有變化（min==max）時 setYRange 收斂成高度 0
    # 的退化範圍。
    y_lo, y_hi = min(series), max(series)
    y_pad = max((y_hi - y_lo) * 0.1, 1.0)
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(y_lo - y_pad, y_hi + y_pad, padding=0)
    _mark_latest_value(chart, x, dates, series, "#17becf", "{:,.0f}")


def _populate_mfi_chart(chart, vpt_mfi_history):
    """MFI 值域固定在 0～100，圖上加 80／20 兩條參考線（超買／超賣，MFI
    標準慣例），跟 _populate_flow_chart 的零軸參考線同一種畫法。"""
    chart.clear()
    if vpt_mfi_history is None or not vpt_mfi_history["dates"]:
        return

    dates = vpt_mfi_history["dates"]
    if isinstance(chart, StockChart):
        chart.dates = list(dates)
    x = list(range(len(dates)))
    set_date_ticks(chart, dates)

    series = vpt_mfi_history["mfi"]
    chart.plot(x, series, pen=pg.mkPen("#bcbd22", width=2), name="MFI")
    chart.addLine(y=80, pen=pg.mkPen(COLOR_LOSS, style=QtCore.Qt.DashLine, width=1))
    chart.addLine(y=20, pen=pg.mkPen(COLOR_GAIN, style=QtCore.Qt.DashLine, width=1))
    chart.setLabel("left", "MFI", color=COLOR_TEXT)
    chart.setTitle(f"MFI 資金流量｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt")
    _limit_zoom_to_data(chart, x, series + [0, 100])
    chart.setXRange(min(x), max(x), padding=0.02)
    chart.setYRange(0, 100, padding=0.02)
    _mark_latest_value(chart, x, dates, series, "#bcbd22", "{:.1f}")


def _populate_short_sale_balance_chart(chart, sbl_balance):
    """借券賣出餘額（股）趨勢圖：跟 _populate_margin_chart 一樣是「餘額」，直接
    畫原始值。這是證券商辦理有價證券借貸的餘額，單位是股，跟融資融券頁籤的
    「張」不是同一個量級，所以是獨立分頁，不是加進融資融券那張圖。"""
    chart.clear()
    if sbl_balance is None or not sbl_balance.get("dates"):
        return

    dates = sbl_balance["dates"]
    if isinstance(chart, StockChart):
        chart.dates = list(dates)
    x = list(range(len(dates)))
    set_date_ticks(chart, dates)

    series = sbl_balance["series"]["借券賣出餘額"]
    chart.plot(x, series, pen=pg.mkPen(COLOR_ACCENT, width=2), name="借券賣出餘額")
    chart.setLabel("left", "餘額（股）", color=COLOR_TEXT)
    chart.setTitle(f"借券賣出餘額｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt")


def _populate_lending_volume_chart(chart, lending):
    """借券成交量（張）趨勢圖：借券市場的每日成交量，跟借券賣出餘額是不同性質
    的數字（一個是累積餘額，一個是當日流量），單位也不同（股 vs 張），所以
    分開兩張圖，不合併成一張雙軸圖。"""
    chart.clear()
    if lending is None or not lending.get("dates"):
        return

    dates = lending["dates"]
    if isinstance(chart, StockChart):
        chart.dates = list(dates)
    x = list(range(len(dates)))
    set_date_ticks(chart, dates)

    series = lending["series"]["借券成交量"]
    chart.plot(x, series, pen=pg.mkPen(COLOR_SPECIAL, width=2), name="借券成交量")
    chart.setLabel("left", "成交量（張）", color=COLOR_TEXT)
    chart.setTitle(f"借券成交量｜近 {len(dates)} 個交易日", color=COLOR_TEXT, size="11pt")


def _plot_technical_series(chart, dates, series, color, name):
    points = [
        (index, value)
        for index, value in enumerate(series or ())
        if value is not None
    ]
    if not points:
        return []
    x, y = zip(*points)
    chart.plot(list(range(len(series))), [float("nan") if value is None else value for value in series], connect="finite", pen=pg.mkPen(color, width=2), name=name)
    return list(y)


def _populate_technical_chart(chart, technical_data, mode="kd"):
    """繪製本地計算的 KD／MACD／均線圖。"""
    chart.clear()
    if not technical_data or not technical_data.get("dates"):
        empty = pg.TextItem("尚無足夠歷史價格資料可計算技術指標", color=COLOR_MUTED)
        chart.addItem(empty)
        empty.setPos(0, 0)
        chart.setTitle("技術分析", color=COLOR_TEXT, size="11pt")
        return

    dates = technical_data["dates"]
    if isinstance(chart, StockChart):
        chart.dates = list(dates)
    spec = technical_data.get("charts", {}).get(mode)
    if not spec:
        chart.setTitle("此快取尚無指標，請重新載入資料")
        return
    values = []
    colors = ("#409cff", "#ffab40", "#43c59e", "#dc709f", "#b399ff", "#b8c85a", "#cccccc")
    for index, (name, series) in enumerate(spec["series"].items()):
        values += _plot_technical_series(chart, dates, series, colors[index % len(colors)], name)
    for level in spec["levels"]:
        chart.addLine(y=level, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine))
    chart.setTitle(spec["title"], color=COLOR_TEXT, size="11pt")
    chart.setLabel("left", spec["unit"])
    set_date_ticks(chart, dates, max_labels=6)
    chart.setXRange(0, max(1,len(dates)-1), padding=.02)
    if values:
        lo, hi = min(values), max(values)
        pad = max(abs(lo)*.02, 1) if lo == hi else (hi-lo)*.1
        chart.setYRange(lo-pad, hi+pad, padding=0)
    else:
        chart.setYRange(0, 1)
        empty = pg.TextItem("資料不足：缺少必要欄位或尚未滿計算期數", color=COLOR_MUTED)
        chart.addItem(empty)
        empty.setPos(0,.5)


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
            extra_setup = _setup_price_chart_click if data_key == "price_history" else None
            self._lazy.add_tab(tab_label, self._make_chart_build(state, data_key, populate_fn, extra_setup))
        self._technical_state = {"widget": None}
        self._lazy.add_tab("技術分析", self._build_technical_tab)
        self._comparison_state = {"widget": None}
        self._lazy.add_tab("雙資料比較", self._build_comparison_tab)

    def _make_chart_build(self, state, data_key, populate_fn, extra_setup):
        def build(container):
            chart = state["chart"]
            if chart is None:
                chart = StockChart()
                apply_chart_theme(chart)
                if extra_setup is not None:
                    extra_setup(chart)
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
