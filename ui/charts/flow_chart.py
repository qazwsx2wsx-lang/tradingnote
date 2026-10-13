"""資金流向泡泡圖（動能／估值／主力同步買超三種模式）與法人方向發散圖。"""

import hashlib
import statistics

import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from tradingnote_history import (
    _cross_sectional_percentile,
    compute_industry_flow,
    compute_valuation_flow,
)

from ui.charts.base import (
    ChartWidget,
    bubble_scatter,
    clear_zoom_limits,
    empty_state,
    ref_line,
    select_bubble_labels,
    set_axis_label,
    set_corner_labels,
    set_title,
    signed_bars,
    style_plot,
)
from ui.format import _compact_money, _sign
from ui.theme import (
    COLOR_GAIN,
    COLOR_GAIN_TINT,
    COLOR_GAIN_TINT_STRONG,
    COLOR_INFO_TINT_STRONG,
    COLOR_LOSS,
    COLOR_LOSS_TINT,
    COLOR_LOSS_TINT_STRONG,
    COLOR_MUTED,
    COLOR_NEUTRAL_TINT,
    COLOR_SPECIAL_TINT,
    COLOR_TEXT,
    COLOR_WARNING_TINT,
    ENTITY_COLORS,
    SERIES_PALETTE,
)


class FlowChartWidget(ChartWidget):
    """產業資金流向泡泡圖：統一外觀＋滾輪／觸控板捏合縮放（見 ui.charts.base）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        style_plot(self, legend=False)


VALUATION_METRIC_LABELS = {"per": "本益比 PER", "pbr": "股價淨值比 PBR"}


def _flow_quadrant_specs(mode):
    """回傳象限名稱、解讀與底色；鍵值依序是 X／Y 是否高於參考線。"""
    if mode == "valuation":
        return {
            # 估值模式的象限是「便宜／貴」不是漲跌，刻意不用紅綠底色以免跟方向混淆。
            (False, True): ("便宜吸金", "估值較低・資金升溫", COLOR_INFO_TINT_STRONG),
            (True, True): ("強勢追價", "估值較高・資金升溫", COLOR_WARNING_TINT),
            (False, False): ("低估觀望", "估值較低・資金降溫", COLOR_NEUTRAL_TINT),
            (True, False): ("高估降溫", "估值較高・資金降溫", COLOR_SPECIAL_TINT),
        }
    if mode == "institutional_sync":
        return {
            (False, True): ("偏賣但金額買超", "多數法人賣・合計卻買超", COLOR_WARNING_TINT),
            (True, True): ("主力同步買超", "同步偏買・買超金額大", COLOR_GAIN_TINT_STRONG),
            (False, False): ("主力同步賣超", "同步偏賣・賣超金額大", COLOR_LOSS_TINT_STRONG),
            (True, False): ("偏買但金額賣超", "多數法人買・合計卻賣超", COLOR_WARNING_TINT),
        }
    return {
        (False, True): ("放量承壓", "區間下跌・今日放量", COLOR_LOSS_TINT),
        (True, True): ("強勢吸金", "區間上漲・今日放量", COLOR_GAIN_TINT_STRONG),
        (False, False): ("弱勢觀望", "區間下跌・量能不足", COLOR_NEUTRAL_TINT),
        (True, False): ("量縮走強", "區間上漲・量能待確認", COLOR_WARNING_TINT),
    }


def _flow_focus_quadrants(mode):
    """（值得關注象限, 需留意象限）；用於「今日重點」清單與標籤優先順序。"""
    if mode == "valuation":
        return (False, True), (True, False)
    if mode == "institutional_sync":
        return (True, True), (False, False)
    return (True, True), (False, True)


def _add_flow_quadrants(chart, bounds, x_ref, y_ref, mode):
    """四個象限底色畫在資料座標；象限名稱釘在視窗角落（縮放後仍看得到）。"""
    x_low, x_high, y_low, y_high = bounds
    specs = _flow_quadrant_specs(mode)
    areas = {
        (False, True): (x_low, x_ref, y_ref, y_high),
        (True, True): (x_ref, x_high, y_ref, y_high),
        (False, False): (x_low, x_ref, y_low, y_ref),
        (True, False): (x_ref, x_high, y_low, y_ref),
    }
    for quadrant, (left, right, bottom, top) in areas.items():
        region = QtWidgets.QGraphicsRectItem(
            QtCore.QRectF(left, bottom, max(right - left, 0), max(top - bottom, 0))
        )
        region.setBrush(pg.mkBrush(QtGui.QColor(specs[quadrant][2])))
        region.setPen(pg.mkPen(None))
        region.setZValue(-20)
        region._flow_quadrant_region = True
        chart.addItem(region)
    set_corner_labels(
        chart, {quadrant: (name, detail) for quadrant, (name, detail, _c) in specs.items()}
    )
    return specs


def _populate_institutional_direction_chart(chart, rows, value_attr, title):
    """畫單一法人的族群淨買賣左右發散圖；右買超、左賣超。"""
    chart.clear()
    plot = chart.getPlotItem()
    ranked = sorted(
        (row for row in rows if getattr(row, value_attr, 0)),
        key=lambda row: abs(getattr(row, value_attr)),
        reverse=True,
    )[:8]
    if not ranked:
        empty_state(chart, "尚無法人資料", title=title)
        return

    ranked.reverse()
    values = [getattr(row, value_attr) / 100_000_000 for row in ranked]
    # 買超（正）紅、賣超（負）綠：台灣慣例，跟 COLOR_GAIN／COLOR_LOSS 一致。
    signed_bars(chart, range(len(ranked)), values, horizontal=True)
    ref_line(chart, x=0, dashed=False)
    plot.getAxis("left").setTicks(
        [[(index, row.group) for index, row in enumerate(ranked)]]
    )
    plot.setYRange(-0.7, len(ranked) - 0.3, padding=0)
    max_abs = max(abs(value) for value in values) or 1
    plot.setXRange(-max_abs * 1.15, max_abs * 1.15, padding=0)
    total = sum(getattr(row, value_attr) for row in rows)
    direction = "買超" if total > 0 else "賣超" if total < 0 else "持平"
    set_title(chart, f"{title}｜整體{direction} {abs(total) / 100_000_000:,.1f} 億")
    set_axis_label(chart, "bottom", "← 賣超　估算淨額（億）　買超 →")


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
        set_corner_labels(chart, {})
        empty_state(chart, empty_hint, title=empty_title)
        clear_zoom_limits(chart)
        return {"focus": [], "avoid": []}

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
    focus_key, avoid_key = _flow_focus_quadrants(mode)
    quadrant_of = {f.industry: (xs_of(f) >= x_ref_line, ys_of(f) >= y_ref_line) for f in plotted}
    # 標籤優先給「值得關注象限」的大泡泡，其餘依資金比重補滿；用共用的碰撞過濾避免重疊。
    ranked = sorted(
        plotted,
        key=lambda f: (quadrant_of[f.industry] != focus_key, -f.capital_share_pct),
    )
    # 離散的 X（同步分數 -3～+3）會讓泡泡疊成一直線，依序散開一點點；提示仍顯示真實分數。
    jitter = {}
    if mode == "institutional_sync":
        seen = {}
        for f in plotted:
            n = seen.get(xs_of(f), 0)
            seen[xs_of(f)] = n + 1
            jitter[f.industry] = ((n % 7) - 3) * 0.09
    x_span = (x_max - x_min) or 1
    y_span = (y_max - y_min) or 1

    def in_corner_label_zone(x, y):
        # 象限名稱釘在四個角落；落在這些角落的泡泡不標名，避免文字疊在象限名稱上。
        fx = (x - view_bounds[0]) / ((view_bounds[1] - view_bounds[0]) or 1)
        fy = (y - view_bounds[2]) / ((view_bounds[3] - view_bounds[2]) or 1)
        return (fx < 0.2 or fx > 0.8) and (fy < 0.15 or fy > 0.85)

    labeled = set(
        select_bubble_labels(
            [
                (f.industry, xs_of(f) + jitter.get(f.industry, 0), ys_of(f))
                for f in ranked
                if not in_corner_label_zone(xs_of(f) + jitter.get(f.industry, 0), ys_of(f))
            ],
            limit=8,
            span_x=x_span,
            span_y=y_span,
            min_dy=0.1,
        )
    )
    spots = []
    for f in plotted:
        x, y = xs_of(f), ys_of(f)
        quadrant_name, quadrant_detail, _quadrant_color = quadrant_specs[quadrant_of[f.industry]]
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
        # 填色＝方向（紅買／綠賣／灰中性，全站同一語意）；大小＝資金比重；
        # 沒被標名的泡泡淡化，讓有標名的重點更突出。分類名稱看標籤與 hover 提示。
        is_labeled = f.industry in labeled
        fill = QtGui.QColor(direction_color)
        fill.setAlpha(210 if is_labeled else 110)
        spots.append(
            {
                "pos": (x + jitter.get(f.industry, 0), y),
                "size": size,
                "data": {"industry": f.industry, "tip": tip},
                "brush": pg.mkBrush(fill),
                "pen": pg.mkPen(QtGui.QColor(COLOR_TEXT), width=1.2 if is_labeled else 0.6),
            }
        )
        if not is_labeled:
            continue
        display_name = f.industry if len(f.industry) <= 18 else f"{f.industry[:17]}…"
        label = pg.TextItem(
            f"{display_name}\n{f.capital_share_pct:.1f}%",
            color=COLOR_TEXT,
            anchor=(0.5, 1.3),
        )
        label.setPos(x + jitter.get(f.industry, 0), y)
        chart.addItem(label)

    # 所有泡泡共用單一 ScatterPlotItem，避免每次更新建立數十個 GraphicsObject。
    # 點擊仍可由 point.data() 找回群組，因此效能改善不犧牲互動。
    chart.addItem(bubble_scatter(
        spots,
        on_click=(lambda data: on_industry_click(data["industry"])) if on_industry_click else None,
    ))

    ref_line(chart, x=x_ref_line)
    ref_line(chart, y=y_ref_line)
    set_axis_label(chart, "bottom", x_label)
    set_axis_label(chart, "left", y_label)
    share_label = "成交涵蓋率%" if overlapping_groups else "資金比重%"
    title = f"{title_prefix}（泡泡大小＝{share_label}，點擊可查看成分股）"
    if skipped:
        title += f"　（另有 {skipped} 個群組因歷史資料不足未顯示）"
    set_title(chart, title)

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

    def _picks(key):
        return [
            f.industry
            for f in sorted(plotted, key=lambda f: -f.capital_share_pct)
            if quadrant_of[f.industry] == key
        ][:5]

    return {
        "focus": _picks(focus_key),
        "avoid": _picks(avoid_key),
        "focus_name": quadrant_specs[focus_key][0],
        "avoid_name": quadrant_specs[avoid_key][0],
    }


# 強度分數權重：法人是唯一非推估的資金數據，佔比最高；缺法人資料時其餘兩項重新正規化。
STRENGTH_WEIGHTS = (("inst", "法人買超", 0.5), ("mom", "價格動能", 0.3), ("vol", "量能", 0.2))


def compute_strength_ranking(dashboard, limit=10):
    """「最強族群」排行：法人買超強度 50%＋區間動能 30%＋量能 20%，各項先在同批族群中
    轉成 0～100 百分位再加權。

    法人強度＝區間法人合計買超 ÷（今日成交額 × 區間交易日數），把族群大小的影響拿掉
    （用絕對金額會永遠偏向大族群）；分母用今日成交額乘天數是近似值。沒有法人資料的
    族群只用動能＋量能（權重重新正規化）並標記 has_inst=False。"""
    if dashboard is None:
        return []
    days = max(len(dashboard.period.actual_dates), 1)
    # 歷史法人資料沒涵蓋整個區間時，institutional_flow 只是單日快照，跟區間動能／量能
    # 的時間口徑不一致——整項排除（其餘兩項重新正規化），由 UI 顯示警告。
    inst_by_group = {
        r.group: r
        for r in (dashboard.institutional_flow or ())
        if r.covered_stocks > 0 and getattr(dashboard, "institutional_covers_period", True)
    }
    flows = [
        f for f in dashboard.industry_flow
        if f.avg_change_pct is not None and f.volume_ratio is not None
    ]
    inst_strength = {
        f.industry: inst_by_group[f.industry].total_value / (f.total_trading_value * days)
        for f in flows
        if f.industry in inst_by_group and f.total_trading_value > 0
    }
    inst_values = list(inst_strength.values())
    mom_values = [f.avg_change_pct for f in flows]
    vol_values = [f.volume_ratio for f in flows]
    rows = []
    for f in flows:
        percentiles = {
            "inst": _cross_sectional_percentile(inst_strength.get(f.industry), inst_values),
            "mom": _cross_sectional_percentile(f.avg_change_pct, mom_values),
            "vol": _cross_sectional_percentile(f.volume_ratio, vol_values),
        }
        available = [(key, w) for key, _label, w in STRENGTH_WEIGHTS if percentiles[key] is not None]
        total_weight = sum(w for _key, w in available)
        parts = {key: percentiles[key] * w / total_weight for key, w in available}
        inst = inst_by_group.get(f.industry)
        rows.append({
            "group": f.industry,
            "score": sum(parts.values()),
            "parts": parts,
            "has_inst": "inst" in parts,
            "inst_value": inst.total_value if inst else None,
            "change_pct": f.avg_change_pct,
            "volume_ratio": f.volume_ratio,
        })
    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]


def populate_strength_chart(chart, rows, on_industry_click=None):
    """最強族群橫向堆疊長條：長度＝強度分數（0～100），三段顏色＝法人／動能／量能貢獻。"""
    chart.clear()
    plot = chart.getPlotItem()
    if not rows:
        empty_state(chart, "尚無足夠資料計算族群強度", title="最強族群排行")
        return
    ranked = list(reversed(rows))  # y 軸由下往上，最強的放最上面
    colors = (ENTITY_COLORS.get("外資") or SERIES_PALETTE[0], SERIES_PALETTE[1], SERIES_PALETTE[2])
    left = [0.0] * len(ranked)
    for (key, label, _w), color in zip(STRENGTH_WEIGHTS, colors):
        widths = [r["parts"].get(key, 0.0) for r in ranked]
        chart.addItem(pg.BarGraphItem(
            x0=left, y=list(range(len(ranked))), height=0.62, width=widths,
            brush=pg.mkBrush(QtGui.QColor(color)), pen=pg.mkPen(None),
        ))
        left = [a + b for a, b in zip(left, widths)]
    spots = []
    for index, r in enumerate(ranked):
        inst_text = (
            f"法人區間買賣超：{_compact_money(r['inst_value'])}"
            if r["inst_value"] is not None else "法人：無資料（僅用動能＋量能）"
        )
        spots.append({
            "pos": (r["score"], index),
            "size": 16,
            "brush": pg.mkBrush(QtGui.QColor(0, 0, 0, 0)),
            "pen": pg.mkPen(None),
            "data": {"industry": r["group"], "tip": (
                f"{r['group']}｜強度 {r['score']:.0f}\n{inst_text}\n"
                f"區間漲跌：{r['change_pct']:+.2f}%　量比：{r['volume_ratio']:.2f} 倍\n"
                f"法人 {r['parts'].get('inst', 0):.0f}＋動能 {r['parts'].get('mom', 0):.0f}"
                f"＋量能 {r['parts'].get('vol', 0):.0f}\n點擊查看成分股"
            )},
        })
    chart.addItem(bubble_scatter(
        spots,
        on_click=(lambda data: on_industry_click(data["industry"])) if on_industry_click else None,
    ))
    for index, r in enumerate(ranked):
        label = pg.TextItem(
            f"{r['score']:.0f}" + ("" if r["has_inst"] else "＊"), color=COLOR_TEXT, anchor=(0, 0.5)
        )
        label.setPos(r["score"] + 1, index)
        chart.addItem(label)
    plot.getAxis("left").setTicks([[(i, r["group"]) for i, r in enumerate(ranked)]])
    plot.getAxis("left").setWidth(130)  # 族群名稱長短不一，固定寬度避免軸被壓縮到看不到字
    plot.setXRange(0, 108, padding=0)
    plot.setYRange(-0.7, len(ranked) - 0.3, padding=0)
    set_title(chart, "最強族群排行（強度分數 0～100，＊＝無法人資料）")
    set_axis_label(
        chart, "bottom",
        "強度分數＝法人買超 50%＋價格動能 30%＋量能 20%（同批族群內的百分位）"
        if any(r["has_inst"] for r in rows)
        else "強度分數＝價格動能 60%＋量能 40%（法人資料不足，未計入；同批族群內的百分位）",
    )
