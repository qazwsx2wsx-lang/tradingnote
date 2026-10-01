"""資金流向泡泡圖（動能／估值／主力同步買超三種模式）與法人方向發散圖。"""

import hashlib
import statistics

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from tradingnote_history import compute_industry_flow, compute_valuation_flow

from ui.format import _compact_money, _sign
from ui.theme import (
    COLOR_ACCENT,
    COLOR_GAIN,
    COLOR_GAIN_TINT,
    COLOR_LOSS,
    COLOR_LOSS_TINT,
    COLOR_MUTED,
    COLOR_NEUTRAL_TINT,
    COLOR_SURFACE,
    COLOR_TEXT,
    COLOR_WARNING_TINT,
)


class FlowChartWidget(pg.PlotWidget):
    """產業資金流向泡泡圖，內建滑鼠滾輪縮放與觸控板手勢縮放（macOS pinch-to-zoom）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setBackground(COLOR_SURFACE)
        self.showGrid(x=True, y=True, alpha=0.08)
        self.getPlotItem().getViewBox().setMouseMode(pg.ViewBox.RectMode)
        # RectMode 讓滑鼠左鍵拖曳做框選縮放；平移改用滾輪縮放 + 觸控板手勢，
        # 保留右鍵拖曳可平移（pyqtgraph 預設行為）。
        self.setMouseEnabled(x=True, y=True)
        self.getPlotItem().getViewBox().setMouseMode(pg.ViewBox.PanMode)

    def event(self, ev):
        # macOS 觸控板雙指捏合（pinch）在 Qt 上是 NativeGesture 事件，
        # 跟滑鼠滾輪縮放（wheelEvent，pyqtgraph 已內建支援）分開處理，
        # 這樣兩指滾動、雙指捏合都能觸發平滑縮放。
        if ev.type() == QtCore.QEvent.NativeGesture:
            if ev.gestureType() == QtCore.Qt.NativeGestureType.ZoomNativeGesture:
                vb = self.getPlotItem().getViewBox()
                scale = 1.0 - ev.value()
                center_scene = self.mapToScene(ev.position().toPoint())
                center_view = vb.mapSceneToView(center_scene)
                vb.scaleBy((scale, scale), center=center_view)
                return True
        return super().event(ev)


VALUATION_METRIC_LABELS = {"per": "本益比 PER", "pbr": "股價淨值比 PBR"}


def _flow_quadrant_specs(mode):
    """回傳象限名稱、解讀與底色；鍵值依序是 X／Y 是否高於參考線。"""
    if mode == "valuation":
        return {
            (False, True): ("便宜吸金", "估值較低・資金升溫", COLOR_GAIN_TINT),
            (True, True): ("強勢追價", "估值較高・資金升溫", COLOR_WARNING_TINT),
            (False, False): ("低估觀望", "估值較低・資金降溫", COLOR_NEUTRAL_TINT),
            (True, False): ("高估降溫", "估值較高・資金降溫", COLOR_LOSS_TINT),
        }
    if mode == "institutional_sync":
        return {
            (False, True): ("訊號不一致", "同步偏賣・金額卻偏買超", COLOR_WARNING_TINT),
            (True, True): ("主力同步買超", "同步偏買・買超金額大", COLOR_GAIN_TINT),
            (False, False): ("主力同步賣超", "同步偏賣・賣超金額大", COLOR_LOSS_TINT),
            (True, False): ("訊號不一致", "同步偏買・金額卻偏賣超", COLOR_WARNING_TINT),
        }
    return {
        (False, True): ("放量承壓", "區間下跌・今日放量", COLOR_LOSS_TINT),
        (True, True): ("強勢吸金", "區間上漲・今日放量", COLOR_GAIN_TINT),
        (False, False): ("弱勢觀望", "區間下跌・量能不足", COLOR_NEUTRAL_TINT),
        (True, False): ("量縮走強", "區間上漲・量能待確認", COLOR_WARNING_TINT),
    }


def _add_flow_quadrants(chart, bounds, x_ref, y_ref, mode):
    """以固定八個圖元畫四象限底色與名稱，不隨泡泡數增加繪圖成本。"""
    x_low, x_high, y_low, y_high = bounds
    x_span = max(x_high - x_low, 1e-9)
    y_span = max(y_high - y_low, 1e-9)
    specs = _flow_quadrant_specs(mode)
    areas = {
        # 上方標籤以頂緣為錨點向下展開；下方反之，避免文字被圖框裁掉。
        (False, True): (x_low, x_ref, y_ref, y_high, (0, 0)),
        (True, True): (x_ref, x_high, y_ref, y_high, (1, 0)),
        (False, False): (x_low, x_ref, y_low, y_ref, (0, 1)),
        (True, False): (x_ref, x_high, y_low, y_ref, (1, 1)),
    }
    for quadrant, (left, right, bottom, top, anchor) in areas.items():
        name, detail, color = specs[quadrant]
        region = QtWidgets.QGraphicsRectItem(
            QtCore.QRectF(left, bottom, max(right - left, 0), max(top - bottom, 0))
        )
        region.setBrush(pg.mkBrush(QtGui.QColor(color)))
        region.setPen(pg.mkPen(None))
        region.setZValue(-20)
        region._flow_quadrant_region = True
        chart.addItem(region)

        x = (left + 0.035 * x_span) if anchor[0] == 0 else (right - 0.035 * x_span)
        y = (top - 0.055 * y_span) if anchor[1] == 0 else (bottom + 0.055 * y_span)
        label = pg.TextItem(
            html=(
                f'<div style="color:{COLOR_TEXT}; font-size:11pt; font-weight:600;">{name}</div>'
                f'<div style="color:{COLOR_MUTED}; font-size:8pt;">{detail}</div>'
            ),
            anchor=anchor,
        )
        label.setPos(x, y)
        label.setZValue(-10)
        label._flow_quadrant_label = True
        chart.addItem(label)
    return specs


def _populate_institutional_direction_chart(chart, rows, value_attr, title):
    """畫單一法人的族群淨買賣左右發散圖；右買超、左賣超。"""
    chart.clear()
    plot = chart.getPlotItem()
    plot.hideButtons()
    plot.setMouseEnabled(x=True, y=False)
    ranked = sorted(
        (row for row in rows if getattr(row, value_attr, 0)),
        key=lambda row: abs(getattr(row, value_attr)),
        reverse=True,
    )[:8]
    if not ranked:
        empty = pg.TextItem("尚無法人資料", color=COLOR_MUTED, anchor=(0.5, 0.5))
        empty.setPos(0, 0)
        chart.addItem(empty)
        chart.setTitle(title, color=COLOR_TEXT, size="11pt")
        return

    ranked.reverse()
    values = [getattr(row, value_attr) / 100_000_000 for row in ranked]
    positions = list(range(len(ranked)))
    positive_y = [y for y, value in zip(positions, values) if value >= 0]
    positive_x = [value for value in values if value >= 0]
    negative_y = [y for y, value in zip(positions, values) if value < 0]
    negative_x = [value for value in values if value < 0]
    if positive_x:
        chart.addItem(
            pg.BarGraphItem(
                y=positive_y,
                height=0.64,
                x0=0,
                x1=positive_x,
                brush=pg.mkBrush(46, 155, 101, 205),
                pen=pg.mkPen("#247E53"),
            )
        )
    if negative_x:
        chart.addItem(
            pg.BarGraphItem(
                y=negative_y,
                height=0.64,
                x0=0,
                x1=negative_x,
                brush=pg.mkBrush(217, 88, 82, 205),
                pen=pg.mkPen("#B94743"),
            )
        )
    chart.addLine(x=0, pen=pg.mkPen(COLOR_MUTED, width=1))
    plot.getAxis("left").setTicks(
        [[(index, row.group) for index, row in enumerate(ranked)]]
    )
    plot.setYRange(-0.7, len(ranked) - 0.3, padding=0)
    max_abs = max(abs(value) for value in values) or 1
    plot.setXRange(-max_abs * 1.15, max_abs * 1.15, padding=0)
    total = sum(getattr(row, value_attr) for row in rows)
    direction = "買超" if total > 0 else "賣超" if total < 0 else "持平"
    chart.setTitle(
        f"{title}｜整體{direction} {abs(total) / 100_000_000:,.1f} 億",
        color=COLOR_TEXT,
        size="11pt",
    )
    chart.setLabel("bottom", "← 賣超　估算淨額（億）　買超 →", color=COLOR_MUTED)


def _institutional_sync_score(institutional_row):
    """三大法人（外資／投信／自營商）同一族群的方向一致性分數，範圍 -3～+3：
    每家淨買超記 +1、淨賣超記 -1、淨額剛好是 0 記 0，三家加總。+3＝三家全部
    買超（主力同步買超）、-3＝三家全部賣超、其餘＝方向不一致。"""
    return (
        _sign(institutional_row.foreign_value)
        + _sign(institutional_row.trust_value)
        + _sign(institutional_row.dealer_value)
    )


def _category_color(name):
    """依分類名稱決定穩定的泡泡填色／清單色塊：同一個分類名稱永遠對應同一個
    顏色，且不依賴「這次畫面上還有哪些其他分類」（不是用排序索引分配色相），
    才能保證泡泡圖跟下方清單用同一個名稱查出來的顏色永遠一致。用雜湊值決定
    色相（0-359°），固定飽和度/明度（針對深色底調校），支援任意數量的分類——
    官方產業（約35個）到概念主題/價值鏈細分類（數百個）都適用，不需要為不同
    分類模式另外設計。"""
    digest = hashlib.md5(name.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:4], "big") % 360
    return QtGui.QColor.fromHsv(hue, 140, 225)


def populate_flow_chart(
    chart,
    db_path,
    snapshot,
    avg_days=5,
    on_industry_click=None,
    mode="momentum",
    valuation_metric="per",
    dashboard=None,
    classification_label="官方產業",
    overlapping_groups=False,
):
    """把族群資金流向資料畫進既有的 FlowChartWidget。
    avg_days 決定 X／Y 兩軸的天數（5/10/20 日流向切換），只影響 mode="momentum"；
    mode="valuation" 的短期窗口最多 5 日、長期基準至少 20 日，避免預設 5 日區間
    造成分子分母相同、所有泡泡都落在 Y=1；沒有 dashboard 時才回退到獨立計算。
    on_industry_click 若提供，點擊泡泡時會被呼叫並帶入該群組名稱（例如用來刷新
    下方的成分股面板）。

    `mode="momentum"`（預設）：X＝近 avg_days 日累積漲跌%、Y＝今日量比，即原本的
    「產業資金流向」泡泡圖。`mode="valuation"`：X＝valuation_metric（PER 或 PBR）
    最新一筆數值（成交金額加權平均，只需要今天/最近一次的估值快照，不用等歷史
    百分位）、Y＝資金流入強度（短期窗口 ÷ 所選流向區間），找的是左上角——
    估值比其他產業便宜、流入強度高（錢已經在進）的產業，見 compute_valuation_flow。"""
    chart.clear()
    vb = chart.getPlotItem().getViewBox()

    if mode == "valuation":
        valuation_short_days = min(5, max(1, avg_days))
        valuation_long_days = max(20, max(1, avg_days))
        flow = (
            dashboard.valuation_flow
            if dashboard is not None and dashboard.valuation_flow is not None
            else compute_valuation_flow(
                db_path,
                snapshot,
                short_days=valuation_short_days,
                long_days=valuation_long_days,
                metric=valuation_metric,
            )
        )
        plotted = [
            f for f in flow if f.latest_valuation is not None and f.money_flow_ratio is not None
        ]
        xs_of = lambda f: f.latest_valuation  # noqa: E731
        ys_of = lambda f: f.money_flow_ratio  # noqa: E731
        metric_label = VALUATION_METRIC_LABELS[valuation_metric]
        empty_hint = "尚無估值資料可繪製（等待下一次「重新整理」或啟動時的估值快照）"
        empty_title = f"{classification_label}估值 vs 資金流向"
        x_label = f"最新 {metric_label}（成交金額加權中位數）"
        y_label = f"資金流入強度（近{valuation_short_days}日均額 / 近{valuation_long_days}日均額）"
        title_prefix = (
            f"{classification_label}估值 vs 資金流向｜最新{metric_label} × "
            f"{valuation_short_days}/{valuation_long_days}日資金流入強度"
        )
        # X 沒有像百分位那樣天然的 0～100 參考值，改用「所有產業目前值的中位數」當
        # 參考線——落在線左邊＝比其他產業便宜、右邊＝比其他產業貴，是同業間的相對定位，
        # 不是自身歷史定位（後者需要長天期歷史，見 compute_valuation_flow 的取捨說明）。
        xs_for_ref = [xs_of(f) for f in plotted]
        x_ref_line = statistics.median(xs_for_ref) if xs_for_ref else 0
        y_ref_line = 1
    elif mode == "institutional_sync":
        # 歷史法人表（tradingnote_institutional_history，scripts/backfill.py 回補）
        # 涵蓋整個流向區間時是區間加總；否則退回最新一筆快照
        # （get_cached_institutional_snapshot）。dashboard.institutional_flow 由
        # FlowAnalysisService.analyze() 無條件算好（見 tradingnote_flow.py），
        # 用 group 名稱對應 dashboard.industry_flow 的 f.industry。
        institutional_by_group = {
            row.group: row
            for row in (dashboard.institutional_flow if dashboard is not None else ())
        }
        flow = dashboard.industry_flow if dashboard is not None else ()
        plotted = [
            f for f in flow
            if f.industry in institutional_by_group
            and institutional_by_group[f.industry].covered_stocks > 0
        ]
        xs_of = lambda f: _institutional_sync_score(institutional_by_group[f.industry])  # noqa: E731
        ys_of = lambda f: institutional_by_group[f.industry].total_value / 1e8  # noqa: E731
        empty_hint = (
            "尚無三大法人資料可繪製（需要當日三大法人買賣超資料，見「法人方向」"
            "子頁；資料每30分鐘更新一次，僅反映最新一筆，不受上方「流向區間」影響）"
        )
        empty_title = f"{classification_label}主力同步買超"
        x_label = "三大法人同步方向（-3=全部賣超　0=不一致　+3=全部買超）"
        y_label = "三大法人合計買賣超金額（億元，正買超負賣超）"
        title_prefix = f"{classification_label}主力同步買超｜三大法人方向一致性（僅最新一筆資料）"
        x_ref_line, y_ref_line = 0, 0
    else:
        flow = (
            dashboard.industry_flow
            if dashboard is not None
            else compute_industry_flow(db_path, snapshot, avg_days=avg_days)
        )
        plotted = [f for f in flow if f.volume_ratio is not None and f.avg_change_pct is not None]
        xs_of = lambda f: f.avg_change_pct  # noqa: E731
        ys_of = lambda f: f.volume_ratio  # noqa: E731
        empty_hint = "尚無足夠歷史資料可繪製（請先執行「回補歷史資料」，\n或等待逐日累積達到最小天數）"
        empty_title = f"{classification_label}資金流向"
        x_label = f"近{avg_days}日成交金額加權平均累積漲跌 %"
        y_label = f"今日成交量 / 近{avg_days}日均量"
        title_prefix = f"{classification_label}資金流向｜{avg_days}日"
        x_ref_line, y_ref_line = 0, 1

    skipped = len(flow) - len(plotted)

    if not plotted:
        text = pg.TextItem(empty_hint, color=COLOR_MUTED, anchor=(0.5, 0.5))
        chart.addItem(text)
        text.setPos(0, 0)
        chart.setTitle(empty_title, color=COLOR_TEXT, size="13pt")
        vb.setLimits(xMin=None, xMax=None, yMin=None, yMax=None)
        return

    # 泡泡大小用「資金比重(%)」（該群組成交金額 ÷ 全市場今日總成交金額）而非絕對金額，
    # 讓數字換算成跟當日大盤規模脫鉤的相對占比，不再是每天隨大盤總量起伏的絕對值。
    max_share = max(max(f.capital_share_pct for f in plotted), 0.0001)
    xs = [xs_of(f) for f in plotted]
    ys = [ys_of(f) for f in plotted]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_pad = max((x_max - x_min) * 0.15, 1.0)
    y_pad = max((y_max - y_min) * 0.15, 0.2)
    view_bounds = (x_min - x_pad, x_max + x_pad, y_min - y_pad, y_max + y_pad)
    quadrant_specs = _add_flow_quadrants(
        chart, view_bounds, x_ref_line, y_ref_line, mode
    )

    # 當日方向來自同一批 dashboard 的動能資料。估值模式也沿用這份方向，讓使用者
    # 切換模式後仍看到一致的紅綠語意，不需要再查一次資料庫。
    direction_source = dashboard.industry_flow if dashboard is not None else flow
    direction_by_group = {
        f.industry: (
            getattr(f, "daily_change_pct", None),
            getattr(f, "turnover_ratio", None),
            getattr(f, "directional_flow_value", None),
        )
        for f in direction_source
    }
    prominent = {
        f.industry
        for f in sorted(plotted, key=lambda f: f.capital_share_pct, reverse=True)[:5]
    }
    spots = []
    for f in plotted:
        x, y = xs_of(f), ys_of(f)
        quadrant_name, quadrant_detail, _quadrant_color = quadrant_specs[
            (x >= x_ref_line, y >= y_ref_line)
        ]
        size = max(14.0, (f.capital_share_pct / max_share) ** 0.5 * 55.0)
        if mode == "institutional_sync":
            # 這個模式的「方向」不是當日漲跌，是三大法人的同步一致性——外框顏色
            # 改用一致性分數（+3～-3），不沿用其他模式共用的 daily_change_pct。
            row = institutional_by_group[f.industry]
            sync_score = xs_of(f)
            if sync_score >= 2:
                direction_color = QtGui.QColor(COLOR_GAIN)
                direction_text = "主力同步買超"
            elif sync_score <= -2:
                direction_color = QtGui.QColor(COLOR_LOSS)
                direction_text = "主力同步賣超"
            else:
                direction_color = QtGui.QColor(COLOR_MUTED)
                direction_text = "方向不一致"
            tip = (
                f"{f.industry}｜{direction_text}\n"
                f"外資：{_compact_money(row.foreign_value)}　"
                f"投信：{_compact_money(row.trust_value)}　"
                f"自營商：{_compact_money(row.dealer_value)}\n"
                f"合計：{_compact_money(row.total_value)}　涵蓋 {row.covered_stocks} 檔\n"
                f"資料日：{row.date}（僅最新一筆，不受上方「流向區間」影響）\n"
                f"所在象限：{quadrant_name}（{quadrant_detail}）\n"
                f"成交佔比：{f.capital_share_pct:.1f}%\n"
                f"{x_label}：{x:.0f}\n{y_label}：{y:.2f}\n點擊查看成分股"
            )
        else:
            daily_change, turnover_ratio, directional_value = direction_by_group.get(
                f.industry, (None, None, None)
            )
            if daily_change is None or abs(daily_change) < 0.05:
                direction_color = QtGui.QColor(COLOR_MUTED)
                direction_text = "中性"
            elif daily_change > 0:
                direction_color = QtGui.QColor(COLOR_GAIN)
                direction_text = "偏流入"
            else:
                direction_color = QtGui.QColor(COLOR_LOSS)
                direction_text = "偏流出"
            tip = (
                f"{f.industry}｜當日{direction_text}\n"
                f"方向推估：{_compact_money(directional_value)}　"
                f"當日漲跌：{daily_change:+.2f}%\n" if daily_change is not None else
                f"{f.industry}｜當日方向資料不足\n"
            )
            tip += (
                f"成交活躍度：{turnover_ratio:.2f} 倍\n"
                if turnover_ratio is not None else "成交活躍度：—\n"
            )
            if dashboard is not None:
                dates = dashboard.period.actual_dates
                tip += f"分析期間：{dates[0] if dates else '—'} ～ {dashboard.period.end_date}\n"
            tip += (
                f"所在象限：{quadrant_name}（{quadrant_detail}）\n"
                f"成交佔比：{f.capital_share_pct:.1f}%\n"
                f"{x_label}：{x:.2f}\n{y_label}：{y:.2f}\n點擊查看成分股"
            )
        # 填色＝分類（_category_color，同一分類永遠同一顏色，對照下方清單色塊）；
        # 外框＝方向（direction_color，加粗讓辨識度更高）——兩種資訊分開兩個
        # 視覺通道，不會互相蓋掉（2026-09-16 改版，原本填色跟外框都只有方向色，
        # 沒有分類資訊）。
        fill_color = _category_color(f.industry)
        spots.append(
            {
                "pos": (x, y),
                "size": size,
                "data": {"industry": f.industry, "tip": tip},
                "brush": pg.mkBrush(fill_color.red(), fill_color.green(), fill_color.blue(), 195),
                "pen": pg.mkPen(direction_color, width=2.5),
            }
        )
        if f.industry not in prominent:
            continue
        display_name = f.industry if len(f.industry) <= 18 else f"{f.industry[:17]}…"
        label = pg.TextItem(
            f"{display_name}\n{f.capital_share_pct:.1f}%",
            color=COLOR_TEXT,
            anchor=(0.5, 1.3),
        )
        label.setPos(x, y)
        chart.addItem(label)

    # 所有泡泡共用單一 ScatterPlotItem，避免每次更新建立數十個 GraphicsObject。
    # 點擊仍可由 point.data() 找回群組，因此效能改善不犧牲互動。
    # antialias=False：全域 pg.setConfigOptions(antialias=True) 套用到這顆
    # scatter 後，搭配 hoverable=True 會讓滑鼠移動時每次 hover 重繪都要重新
    # 光柵化抗鋸齒圓形，是泡泡圖互動卡頓的主因；只關掉這一個 widget 的抗鋸齒，
    # 不動全域設定，不影響其他已經調好的深色主題視覺。
    scatter = pg.ScatterPlotItem(
        spots=spots,
        hoverable=True,
        # 2026-09-17：目前裝的 pyqtgraph 版本用關鍵字引數呼叫
        # tip(x=, y=, data=)（見 ScatterPlotItem.hoverEvent），參數名稱一定要
        # 叫 x/y 才接得到——原本命名成 _x/_y 會直接 TypeError（拿
        # PYTHONFAULTHANDLER=1 實機跑才發現：hover 泡泡圖時 stderr 一直噴
        # RuntimeWarning，導致這個功能從來沒真的顯示過提示文字）。
        tip=lambda x, y, data: data["tip"],
        hoverPen=pg.mkPen(COLOR_ACCENT, width=2),
        antialias=False,
    )
    if on_industry_click is not None:
        scatter.sigClicked.connect(
            lambda _plot, points, _ev: points
            and on_industry_click(points[0].data()["industry"])
        )
    chart.addItem(scatter)

    chart.addLine(x=x_ref_line, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine, width=1))
    chart.addLine(y=y_ref_line, pen=pg.mkPen(COLOR_MUTED, style=QtCore.Qt.DashLine, width=1))
    chart.setLabel("bottom", x_label, color=COLOR_TEXT)
    chart.setLabel("left", y_label, color=COLOR_TEXT)
    share_label = "成交涵蓋率%" if overlapping_groups else "資金比重%"
    title = f"{title_prefix}（泡泡大小＝{share_label}，點擊可查看成分股）"
    if skipped:
        title += f"　（另有 {skipped} 個群組因歷史資料不足未顯示）"
    chart.setTitle(title, color=COLOR_TEXT, size="13pt")

    # 限制縮小（滾輪／觸控板捏合）的下限，最多縮到剛好看見全部泡泡為止，避免
    # 縮出一大片空白；邊界抓資料範圍的 15% 當緩衝，讓泡泡本身（半徑）與旁邊的
    # 產業名稱標籤不會被邊緣裁到。上限（放大）不受影響，仍可無限拉近。
    vb.setLimits(
        xMin=view_bounds[0],
        xMax=view_bounds[1],
        yMin=view_bounds[2],
        yMax=view_bounds[3],
    )

    chart.enableAutoRange()
