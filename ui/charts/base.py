"""製圖唯一入口：所有 pyqtgraph 圖表的外觀與互動都從這裡取得。

2026-10-10 製圖統一化：原本 7 個檔案各自 setBackground／showGrid／mkPen hardcode
hex，背景有兩種、同一個法人在不同頁面不同顏色。現在規則是：
- 建圖一律用 `new_plot()`（或繼承 `ChartWidget`），不要直接 `pg.PlotWidget()`。
- 顏色只從 `ui.theme` 的 `ENTITY_COLORS`／`SERIES_PALETTE`／`COLOR_GAIN`／`COLOR_LOSS`
  取得，透過 `series_color()`／`signed_brushes()`。
- 互動只有兩種：`"zoom"`（滾輪、觸控板捏合、拖曳平移，縮小下限由
  `limit_zoom_to_data()` 鎖在資料範圍）與 `"static"`（排行榜這類固定版面，鎖定滑鼠）。
"""

import pyqtgraph as pg
from PySide6 import QtCore, QtGui

from ui.theme import (
    CHART_BG,
    CHART_GRID_ALPHA,
    CHART_LINE_WIDTH,
    CHART_TITLE_SIZE,
    COLOR_ACCENT,
    COLOR_BORDER,
    COLOR_GAIN,
    COLOR_LOSS,
    COLOR_MUTED,
    COLOR_TEXT,
    ENTITY_COLORS,
    SERIES_PALETTE,
)

BAR_ALPHA = 0.8
_AXES = ("left", "bottom", "right")


class ChartWidget(pg.PlotWidget):
    """所有圖表的基底：在 pyqtgraph 內建的滾輪縮放之外，加上 macOS 觸控板雙指
    捏合縮放（Qt 的 NativeGesture 事件，跟 wheelEvent 分開處理）。原本只有資金
    流向泡泡圖（FlowChartWidget）有，現在每張可縮放的圖都有。"""

    def event(self, ev):
        if (
            ev.type() == QtCore.QEvent.NativeGesture
            and ev.gestureType() == QtCore.Qt.NativeGestureType.ZoomNativeGesture
            and self.getPlotItem().getViewBox().state["mouseEnabled"][0]
        ):
            vb = self.getPlotItem().getViewBox()
            scale = 1.0 - ev.value()
            center_view = vb.mapSceneToView(self.mapToScene(ev.position().toPoint()))
            vb.scaleBy((scale, scale), center=center_view)
            return True
        return super().event(ev)


def style_plot(chart, *, grid="xy", legend=True, min_height=None, interaction="zoom"):
    """套用統一外觀與互動規則到 PlotWidget（或 PlotItem）。

    grid："xy"／"x"／"y"／""，指哪幾個方向畫格線。
    interaction："zoom" 或 "static"，見模組說明。"""
    plot = chart.getPlotItem() if hasattr(chart, "getPlotItem") else chart
    if hasattr(chart, "setBackground"):
        chart.setBackground(CHART_BG)
    plot.showGrid(x="x" in grid, y="y" in grid, alpha=CHART_GRID_ALPHA)
    for side in _AXES:
        axis = plot.getAxis(side)
        axis.setPen(pg.mkPen(COLOR_BORDER))
        axis.setTextPen(pg.mkPen(COLOR_MUTED))
    plot.hideButtons()
    vb = plot.getViewBox()
    if interaction == "static":
        plot.setMouseEnabled(x=False, y=False)
    else:
        plot.setMouseEnabled(x=True, y=True)
        vb.setMouseMode(pg.ViewBox.PanMode)
    if legend and plot.legend is None:
        plot.addLegend(offset=(-10, 10), labelTextColor=COLOR_TEXT)
    if min_height is not None and hasattr(chart, "setMinimumHeight"):
        chart.setMinimumHeight(min_height)
    return chart


def new_plot(*, min_height=260, grid="xy", legend=False, interaction="zoom"):
    """建立一張已套用統一樣式的圖。"""
    return style_plot(
        ChartWidget(), grid=grid, legend=legend, min_height=min_height, interaction=interaction
    )


def apply_chart_theme(chart, *, min_height=320):
    """舊名稱：時間序列圖（StockChart 等）的預設樣式＝有圖例的 zoom 圖。"""
    return style_plot(chart, min_height=min_height)


# ---------- 顏色 ----------

def series_color(name, index=0):
    """固定實體（外資、投信、融資餘額…）用 ENTITY_COLORS，其餘依序取 SERIES_PALETTE。"""
    return ENTITY_COLORS.get(name) or SERIES_PALETTE[index % len(SERIES_PALETTE)]


def tone_color(value):
    """正值紅、負值綠、零或 None 灰（台灣慣例）。"""
    if not value:
        return COLOR_MUTED
    return COLOR_GAIN if value > 0 else COLOR_LOSS


def signed_brushes(values, alpha=BAR_ALPHA):
    brushes = []
    for value in values:
        color = QtGui.QColor(tone_color(value))
        color.setAlphaF(alpha)
        brushes.append(pg.mkBrush(color))
    return brushes


# ---------- 繪圖 helper ----------

def line(chart, x, y, name, *, color=None, index=0, **kwargs):
    """畫一條系列線；顏色依 name 自動決定（見 series_color）。"""
    color = color or series_color(name, index)
    return chart.plot(x, y, pen=pg.mkPen(color, width=CHART_LINE_WIDTH), name=name, **kwargs)


def signed_bars(chart, positions, values, *, thickness=0.64, horizontal=False):
    """正負值長條圖：正值 COLOR_GAIN、負值 COLOR_LOSS，一個 BarGraphItem 畫完。"""
    values = list(values)
    if horizontal:
        item = pg.BarGraphItem(
            x0=0, y=list(positions), height=thickness, width=values,
            brushes=signed_brushes(values), pen=pg.mkPen(None),
        )
    else:
        item = pg.BarGraphItem(
            x=list(positions), height=values, width=thickness,
            brushes=signed_brushes(values), pen=pg.mkPen(None),
        )
    chart.addItem(item)
    return item


def ref_line(chart, *, x=None, y=None, color=COLOR_MUTED, dashed=True):
    """參考線（零軸、超買超賣、象限分界…）。"""
    pen = pg.mkPen(color, width=1, style=QtCore.Qt.DashLine if dashed else QtCore.Qt.SolidLine)
    return chart.addLine(x=x, y=y, pen=pen)


def set_title(chart, text, *, color=COLOR_TEXT):
    chart.setTitle(text, color=color, size=CHART_TITLE_SIZE)


def set_axis_label(chart, side, text, *, color=COLOR_MUTED):
    chart.setLabel(side, text, color=color)


def empty_state(chart, text, *, title=None):
    """圖上置中顯示灰色提示文字（尚無資料等）。"""
    item = pg.TextItem(text, color=COLOR_MUTED, anchor=(0.5, 0.5))
    chart.addItem(item)
    item.setPos(0, 0)
    if title is not None:
        set_title(chart, title)
    return item


def set_date_ticks(chart, dates, axis="bottom", max_labels=8):
    """把依時間排序的日期字串清單等距抽樣成軸刻度標籤。"""
    if not dates:
        chart.getAxis(axis).setTicks([])
        return
    step = max(1, len(dates) // max_labels)
    chart.getAxis(axis).setTicks([[(i, dates[i]) for i in range(0, len(dates), step)]])


def set_corner_labels(chart, labels):
    """把象限名稱釘在目前視窗的四個角落（像素座標，不隨縮放／平移移開）。

    labels：{(在右邊?, 在上方?): (名稱, 說明)}。名稱畫在泡泡下層，重畫時先清掉舊的。"""
    vb = chart.getPlotItem().getViewBox()
    for item in getattr(chart, "_corner_labels", ()):
        item.setParentItem(None)
        if item.scene() is not None:
            item.scene().removeItem(item)
    items = []
    for (right, top), (name, detail) in labels.items():
        item = pg.TextItem(
            html=(
                f'<div style="color:{COLOR_TEXT}; font-size:12pt; font-weight:700;">{name}</div>'
                f'<div style="color:{COLOR_MUTED}; font-size:8pt;">{detail}</div>'
            ),
            anchor=(1 if right else 0, 0 if top else 1),
        )
        item.setParentItem(vb)
        item.setZValue(-1)
        item._corner = (right, top)
        items.append(item)

    def place(*_):
        width, height = vb.width(), vb.height()
        for item in items:
            right, top = item._corner
            item.setPos(width - 10 if right else 10, 8 if top else height - 8)

    if getattr(chart, "_corner_resize_slot", None) is not None:
        try:
            vb.sigResized.disconnect(chart._corner_resize_slot)
        except (TypeError, RuntimeError):
            pass
    chart._corner_labels = items
    chart._corner_resize_slot = place
    vb.sigResized.connect(place)
    place()


def select_bubble_labels(points, limit, span_x, span_y, min_dx=0.12, min_dy=0.06):
    """依優先順序挑出不會互相重疊的標籤。

    points：已依優先度由高到低排好的 [(key, x, y)]；回傳被選中的 key 清單。
    座標先除以各軸跨度正規化，跟已選標籤太近的就跳過（hover 仍看得到）。"""
    span_x, span_y = span_x or 1, span_y or 1
    placed, keys = [], []
    for key, x, y in points:
        if len(keys) >= limit:
            break
        nx, ny = x / span_x, y / span_y
        if any(abs(nx - px) < min_dx and abs(ny - py) < min_dy for px, py in placed):
            continue
        placed.append((nx, ny))
        keys.append(key)
    return keys


def bubble_scatter(spots, on_click=None):
    """泡泡圖共用的 ScatterPlotItem：hover 顯示 spot["data"]["tip"]、hover 外框用
    accent 色，點擊時把第一個點的 data 傳給 on_click。

    antialias=False：全域 antialias 搭配 hoverable 會讓每次 hover 重繪都重新光柵化
    抗鋸齒圓形，是泡泡圖互動卡頓的主因。tip 的參數名稱必須叫 x/y（pyqtgraph 用
    關鍵字引數呼叫）。"""
    scatter = pg.ScatterPlotItem(
        spots=spots,
        hoverable=True,
        tip=lambda x, y, data: data["tip"],
        hoverPen=pg.mkPen(COLOR_ACCENT, width=2),
        antialias=False,
    )
    if on_click is not None:
        scatter.sigClicked.connect(lambda _plot, points, _ev: points and on_click(points[0].data()))
    return scatter


# ---------- 範圍與縮放 ----------

def limit_zoom_to_data(chart, xs, ys, x_floor=1.0, y_floor=1.0, pad_ratio=0.15):
    """限制往外縮的下限，最多縮到剛好看見全部資料（外加 15% 緩衝），避免縮出一大片
    空白；放大不受影響。"""
    vb = chart.getPlotItem().getViewBox()
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_pad = max((x_max - x_min) * pad_ratio, x_floor)
    y_pad = max((y_max - y_min) * pad_ratio, y_floor)
    vb.setLimits(xMin=x_min - x_pad, xMax=x_max + x_pad, yMin=y_min - y_pad, yMax=y_max + y_pad)


def clear_zoom_limits(chart):
    chart.getPlotItem().getViewBox().setLimits(xMin=None, xMax=None, yMin=None, yMax=None)


def fit_series(chart, xs, ys, *, y_floor=1.0):
    """時間序列圖的標準收尾：鎖定縮放下限，並直接用資料範圍 setRange。

    不用 enableAutoRange()：圖可能在還沒被切到的分頁或被 QScrollArea 捲到看不見
    的地方，widget 還沒有真正版面尺寸時自動範圍會卡在 ±1 附近的退化值。y_pad 下限
    y_floor 避免整段數值不變（min==max）時收斂成高度 0。"""
    limit_zoom_to_data(chart, xs, ys, y_floor=y_floor)
    y_lo, y_hi = min(ys), max(ys)
    y_pad = max((y_hi - y_lo) * 0.1, y_floor)
    chart.setXRange(min(xs), max(xs), padding=0.02)
    chart.setYRange(y_lo - y_pad, y_hi + y_pad, padding=0)
