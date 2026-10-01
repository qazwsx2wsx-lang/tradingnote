"""個股／資金流向的規則式徽章與洞察文字（趨勢、動能、籌碼）。"""

from PySide6 import QtWidgets

from ui.components.signal_badge import SignalBadge
from ui.format import _sign
from ui.widgets import _clear_layout


def _flow_momentum_insight(rows):
    """rule-based（不接 LLM）：從族群資金流向清單挑出今天最值得注意的一則量價觀察，
    給 InsightCard 用。只用 turnover_ratio（今日量比）與 daily_change_pct（當日
    漲跌%）——兩者都存在才夠格參與，門檻（量比 >= 1.5、漲跌 >= 0.5%）是為了避免拿
    普通、不特別的日子也硬生出一句「看起來煞有其事」的結論。回傳
    (headline, detail, tone)；沒有夠格的族群時回傳中性的「暫無訊號」文案，而不是
    留白或報錯——見規格「Loading/Error/Empty State」的一致性要求。
    """
    candidates = [
        row
        for row in rows
        if row.turnover_ratio is not None
        and row.daily_change_pct is not None
        and row.turnover_ratio >= 1.5
        and abs(row.daily_change_pct) >= 0.5
    ]
    if not candidates:
        return (
            "暫無明顯資金訊號",
            "今日各族群量能與價格變動都在正常範圍內。",
            "neutral",
        )
    top = max(candidates, key=lambda row: row.turnover_ratio)
    if top.daily_change_pct > 0:
        return (
            "資金動能增強",
            f"{top.industry}今日成交量為近期均量的 {top.turnover_ratio:.2f} 倍，"
            f"且價格同步走強（{top.daily_change_pct:+.2f}%）。",
            "positive",
        )
    return (
        "放量下跌需留意",
        f"{top.industry}今日成交量為近期均量的 {top.turnover_ratio:.2f} 倍，"
        f"但價格走弱（{top.daily_change_pct:+.2f}%），賣壓可能未完全釋放。",
        "negative",
    )


def _latest_valid(values):
    """從指標數列取最後一個非 None 的值（序列尾端通常是最新交易日）。"""
    for value in reversed(values or ()):
        if value is not None:
            return value
    return None


def _stock_trend_badges(technical):
    """rule-based（不接 LLM）：從既有的本地技術指標（tradingnote_technical.
    calculate_indicators 的既有輸出，不新增任何指標計算）萃取「趨勢／動能／量能」
    三個方向性標籤，加上依前三者交叉判斷的「量價背離」標籤，給「個股概覽」的
    SignalBadge 用。純粹是「怎麼解讀已經算好的數字」：
    - 趨勢：收盤價相對 MA20 的乖離（>=1% 偏多、<=-1% 偏空，用來過濾貼著均線
      上下的雜訊）。
    - 動能：RSI(14)（>=55 偏強、<=45 偏弱，50 上下不特別有意義所以留一段中性帶）。
    - 量能：今日成交量相對均量20 的倍數（>=1.2x 放大、<=0.8x 萎縮）；量能本身
      沒有天生的多空傾向（爆量可能是噴出也可能是出貨），tone 刻意不用
      positive/negative，避免暗示「量增=好事」。
    - 量價背離：前面「趨勢」跟「量能」分開判斷、刻意不互相參照，所以量價背離
      （價漲量縮、價跌量增）一直沒被標出來——這裡把兩者的判斷結果交叉比對，
      只在真的出現背離時才多附一個標籤；趨勢中性或量價同步時不強行湊出第四
      個標籤，維持跟其他三項一樣「沒有明顯訊號就不下結論」的原則。價跌量增
      tone 刻意用 info 而非 positive/negative，因為可能是止跌訊號也可能是
      逃命賣壓，方向不明確。
    某一項資料不足（例如新股不到 20 個交易日）時該項直接跳過，不用預設值假裝
    有結論；全部不足時回傳空 list，呼叫端應顯示「資料不足」的空狀態文字。
    """
    if not technical:
        return []
    badges = []
    diff_pct = None
    volume_ratio = None

    close = _latest_valid(technical.get("close"))
    ma20 = _latest_valid(technical.get("ma", {}).get("ma20"))
    if close is not None and ma20:
        diff_pct = (close / ma20 - 1) * 100
        if diff_pct >= 1:
            badges.append(("趨勢：偏多", "positive"))
        elif diff_pct <= -1:
            badges.append(("趨勢：偏空", "negative"))
        else:
            badges.append(("趨勢：中性", "neutral"))

    rsi = _latest_valid(technical.get("rsi"))
    if rsi is not None:
        if rsi >= 55:
            badges.append(("動能：偏強", "positive"))
        elif rsi <= 45:
            badges.append(("動能：偏弱", "negative"))
        else:
            badges.append(("動能：中性", "neutral"))

    volume_series = technical.get("charts", {}).get("volume", {}).get("series", {})
    volume = _latest_valid(volume_series.get("成交量"))
    volume_ma20 = _latest_valid(volume_series.get("均量20"))
    if volume is not None and volume_ma20:
        volume_ratio = volume / volume_ma20
        if volume_ratio >= 1.2:
            badges.append(("量能：放大", "warning"))
        elif volume_ratio <= 0.8:
            badges.append(("量能：萎縮", "neutral"))
        else:
            badges.append(("量能：平穩", "neutral"))

    if diff_pct is not None and volume_ratio is not None:
        if diff_pct >= 1 and volume_ratio <= 0.8:
            badges.append(("量價背離：轉弱", "negative"))
        elif diff_pct <= -1 and volume_ratio >= 1.2:
            badges.append(("量價背離：留意止跌", "info"))

    return badges


def _chip_momentum_badges(institutional_history, margin_history):
    """rule-based（不接 LLM）：從個股 120 日三大法人買賣超與融資融券餘額歷史
    （tradingnote_finmind.fetch_institutional_investors_history／
    fetch_margin_short_sale_history 既有輸出，不新增任何抓取邏輯）萃取「籌碼
    方向／籌碼動能／融資動向」三個標籤，給「個股概覽」的 SignalBadge 用，跟
    _stock_trend_badges 同一套設計原則：
    - 籌碼方向：跟族群層級 _institutional_sync_score 同一套三家各自取正負號
      加總的演算法（範圍 -3~+3），套用在單一個股「最新一天」的外資／投信／
      自營商淨買賣超（股數）上——正負號判斷跟金額或股數無關，演算法可以直接
      沿用；只有 +3／-3（三家方向完全一致）才算「同步」，其餘（含只有兩家
      同向）都算「分歧」，跟 _institutional_sync_score 的既有定義一致。
    - 籌碼動能：從最新一天往回數，三大法人合計淨額連續同號（同買或同賣）的
      天數；未達 3 天視為訊號不明顯，不特別標出來（跟量能的「平穩」不同，
      這裡選擇直接不顯示，因為「連 1～2 天」本來就稱不上動能）。
    - 融資動向：最新融資餘額 vs 10 個交易日前的融資餘額變化率，未達 ±1% 視
      為持平不特別標出。tone 刻意用 info：融資增加可能是散戶追價（偏空的
      反指標）也可能是真的看好加碼，跟 _stock_trend_badges 的量能徽章一樣
      不假設方向。
    institutional_history／margin_history 任一為 None（例如沒設定 FinMind
    token）就跳過對應項目；兩者都缺時回傳空 list，呼叫端顯示「資料不足」。
    """
    badges = []

    if institutional_history:
        series = institutional_history.get("series", {})
        foreign = series.get("外資") or []
        trust = series.get("投信") or []
        dealer = series.get("自營商") or []
        n = min(len(foreign), len(trust), len(dealer))
        if n:
            score = _sign(foreign[-1]) + _sign(trust[-1]) + _sign(dealer[-1])
            if score == 3:
                badges.append(("籌碼方向：三大法人同步買超", "positive"))
            elif score == -3:
                badges.append(("籌碼方向：三大法人同步賣超", "negative"))
            else:
                badges.append(("籌碼方向：三大法人分歧", "neutral"))

            streak = 0
            streak_sign = 0
            for index in range(n - 1, -1, -1):
                daily_sign = _sign(foreign[index] + trust[index] + dealer[index])
                if daily_sign == 0:
                    break
                if streak == 0:
                    streak_sign = daily_sign
                    streak = 1
                elif daily_sign == streak_sign:
                    streak += 1
                else:
                    break
            if streak >= 3:
                if streak_sign > 0:
                    badges.append((f"籌碼動能：連買 {streak} 日", "positive"))
                else:
                    badges.append((f"籌碼動能：連賣 {streak} 日", "negative"))

    if margin_history:
        margin_series = margin_history.get("series", {}).get("融資餘額") or []
        if len(margin_series) > 10:
            latest = margin_series[-1]
            reference = margin_series[-11]
            if reference:
                change_pct = (latest / reference - 1) * 100
                if abs(change_pct) >= 1:
                    badges.append((f"融資動向：10 日變化 {change_pct:+.1f}%", "info"))

    return badges


def _populate_badge_row(layout, badges, empty_text):
    """清空並重繪一列 SignalBadge；沒有夠格的資料（badges 為空）時改顯示
    empty_text 的 muted 提示，維持跟其他空狀態一致的呈現方式。呼叫端自己算好
    badges 列表（例如 _stock_trend_badges／_chip_momentum_badges 的回傳值）
    再傳進來，這個函式只負責畫面呈現，共用給 `StockDetailDialog` 的趨勢／
    籌碼徽章列與「個股查詢」分頁的 `stock_preview` 摘要面板。
    """
    _clear_layout(layout)
    if badges:
        for text, tone in badges:
            layout.addWidget(SignalBadge(text, tone=tone))
    else:
        hint = QtWidgets.QLabel(empty_text)
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
    layout.addStretch(1)
