"""純本地技術指標計算。

本模組只處理已取得的日 OHLCV 資料，不依賴 PySide6，也不直接呼叫 API。
同一份價格序列可以同時計算 KD、MACD、均線、RSI、VPT 與 MFI，避免每個
指標各自查詢一次 FinMind。
"""

from dataclasses import dataclass
import math
import sqlite3
from pathlib import Path


@dataclass(frozen=True)
class TechnicalPriceBar:
    date: str
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: float
    trading_value: float | None


def _number(value):
    try:
        number = float(value) if value is not None else None
        return number if number is not None and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def normalize_price_rows(rows):
    """把 FinMind TaiwanStockPrice 列轉成依日期排序且不重複的 OHLCV。"""
    by_date = {}
    for row in rows or ():
        day = row.get("date")
        close = _number(row.get("close"))
        if not day or close is None:
            continue
        by_date[day] = TechnicalPriceBar(
            date=day,
            open=_number(row.get("open")),
            high=_number(row.get("max")),
            low=_number(row.get("min")),
            close=close,
            volume=_number(row.get("Trading_Volume")) or 0.0,
            trading_value=_number(row.get("Trading_money")),
        )
    return [by_date[day] for day in sorted(by_date)]


def build_price_history(rows):
    """建立既有 GUI 歷史股價圖使用的序列格式。"""
    bars = normalize_price_rows(rows)
    result = []
    previous_close = None
    for bar in bars:
        change_pct = (
            (bar.close - previous_close) / previous_close * 100
            if previous_close
            else None
        )
        result.append(
            {
                "date": bar.date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "change_pct": change_pct,
                "volume": bar.volume,
                "trading_value": bar.trading_value,
            }
        )
        previous_close = bar.close
    return result


def _ema(values, period):
    result = [None] * len(values)
    if period < 1 or len(values) < period:
        return result
    seed_index = period - 1
    result[seed_index] = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for index in range(period, len(values)):
        result[index] = values[index] * alpha + result[index - 1] * (1 - alpha)
    return result


def _ema_sparse(values, period):
    """對前段為 None 的序列計算 EMA，結果仍保持原本索引。"""
    indexes = [index for index, value in enumerate(values) if value is not None]
    compact = [values[index] for index in indexes]
    calculated = _ema(compact, period)
    result = [None] * len(values)
    for index, value in zip(indexes, calculated):
        result[index] = value
    return result


def _simple_moving_average(values, period):
    result = [None] * len(values)
    if period < 1:
        return result
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        if all(value is not None for value in window):
            result[index] = sum(window) / period
    return result


def _calculate_macd(closes, fast=12, slow=26, signal=9):
    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    macd = [
        (fast_ema[index] - slow_ema[index])
        if fast_ema[index] is not None and slow_ema[index] is not None
        else None
        for index in range(len(closes))
    ]
    signal_line = _ema_sparse(macd, signal)
    histogram = [
        (macd[index] - signal_line[index])
        if macd[index] is not None and signal_line[index] is not None
        else None
        for index in range(len(closes))
    ]
    return {
        "macd": macd,
        "signal": signal_line,
        "histogram": histogram,
    }


def _calculate_kd(bars, period=9, k_smooth=3, d_smooth=3):
    del k_smooth, d_smooth  # 台股常用 RSV + 2/3、1/3 平滑算法，參數保留在 API 語意中。
    rsv = [None] * len(bars)
    for index in range(period - 1, len(bars)):
        window = bars[index - period + 1 : index + 1]
        if any(bar.high is None or bar.low is None for bar in window):
            continue
        highest = max(bar.high for bar in window)
        lowest = min(bar.low for bar in window)
        rsv[index] = 50.0 if highest == lowest else (
            (bars[index].close - lowest) / (highest - lowest) * 100
        )

    k_values = [None] * len(bars)
    d_values = [None] * len(bars)
    previous_k = previous_d = 50.0
    for index, value in enumerate(rsv):
        if value is None:
            continue
        previous_k = previous_k * 2 / 3 + value / 3
        previous_d = previous_d * 2 / 3 + previous_k / 3
        k_values[index] = previous_k
        d_values[index] = previous_d
    return {"k": k_values, "d": d_values, "rsv": rsv}


def _calculate_rsi(closes, period=14):
    result = [None] * len(closes)
    if len(closes) <= period:
        return result
    gains = [max(closes[index] - closes[index - 1], 0.0) for index in range(1, len(closes))]
    losses = [max(closes[index - 1] - closes[index], 0.0) for index in range(1, len(closes))]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    def value():
        if average_gain == average_loss == 0:
            return 50.0
        if average_loss == 0:
            return 100.0
        relative_strength = average_gain / average_loss
        return 100 - 100 / (1 + relative_strength)

    result[period] = value()
    for offset in range(period, len(gains)):
        average_gain = (average_gain * (period - 1) + gains[offset]) / period
        average_loss = (average_loss * (period - 1) + losses[offset]) / period
        result[offset + 1] = value()
    return result


def calculate_vpt_mfi_history(bars, mfi_period=14):
    """由已正規化的價格列計算既有 VPT／MFI 圖表格式。"""
    # VPT 只需要收盤價／成交量，但 MFI 需要完整的 typical price；沿用舊有
    # 行為，遇到單日 high／low 缺值時只略過該日，不讓一筆異常資料使整段圖表
    # 都無法顯示。
    bars = [bar for bar in bars if bar.high is not None and bar.low is not None]
    if len(bars) <= mfi_period:
        return None
    dates = [bar.date for bar in bars]
    closes = [bar.close for bar in bars]
    volumes = [bar.volume for bar in bars]
    typical = [
        (bar.high + bar.low + bar.close) / 3
        if bar.high is not None and bar.low is not None
        else None
        for bar in bars
    ]
    vpt = [0.0]
    for index in range(1, len(bars)):
        previous_close = closes[index - 1]
        change = (closes[index] - previous_close) / previous_close if previous_close else 0.0
        vpt.append(vpt[-1] + volumes[index] * change)

    raw_flow = [typical[index] * volumes[index] for index in range(len(bars))]
    mfi = [None] * len(bars)
    for index in range(mfi_period, len(bars)):
        positive = negative = 0.0
        for flow_index in range(index - mfi_period + 1, index + 1):
            if typical[flow_index] > typical[flow_index - 1]:
                positive += raw_flow[flow_index]
            elif typical[flow_index] < typical[flow_index - 1]:
                negative += raw_flow[flow_index]
        mfi[index] = 100.0 if negative == 0 else 100 - 100 / (1 + positive / negative)

    return {
        "dates": dates[mfi_period:],
        "vpt": vpt[mfi_period:],
        "mfi": mfi[mfi_period:],
    }


def calculate_indicators(rows, kd_period=9, macd_fast=12, macd_slow=26, macd_signal=9):
    """計算 KD、MACD、均線與 RSI，回傳可直接交給 GUI 的資料。"""
    bars = normalize_price_rows(rows)
    if not bars:
        return None
    closes = [bar.close for bar in bars]
    macd = _calculate_macd(closes, macd_fast, macd_slow, macd_signal)
    result = {
        "dates": [bar.date for bar in bars],
        "close": closes,
        "kd": _calculate_kd(bars, kd_period),
        "macd": macd,
        "ma": {
            "ma5": _simple_moving_average(closes, 5),
            "ma20": _simple_moving_average(closes, 20),
            "ma60": _simple_moving_average(closes, 60),
        },
        "rsi": _calculate_rsi(closes, 14),
    }
    result["charts"] = build_chart_catalog(bars, result)
    return result


def build_chart_catalog(bars, base):
    """日線指標目錄；None 表示暖機或必要欄位不足，不補造價格。"""
    c = [b.close for b in bars]
    v = [b.volume for b in bars]
    n = len(c)
    charts = {}

    def add(key, title, unit, series, levels=()):
        charts[key] = dict(title=title, unit=unit, series=series, levels=levels)

    ma = {f"MA{p}": _simple_moving_average(c, p) for p in (5, 10, 20, 60, 120, 240)}
    add("ma", "收盤價與均線", "價格", {"收盤": c, **ma})
    add("ema", "指數移動平均", "價格", {f"EMA{p}": _ema(c, p) for p in (5, 12, 26, 60)})
    add("kd", "KD（9／3／3）", "數值", {"K": base["kd"]["k"], "D": base["kd"]["d"]}, (20, 80))
    add("macd", "MACD（12／26／9）", "價差", base["macd"], (0,))
    add("rsi", "RSI（Wilder）", "數值", {f"RSI{p}": _calculate_rsi(c, p) for p in (6, 14)}, (30, 70))
    mid = ma["MA20"]
    std = [None if i < 19 else (sum((x-mid[i])**2 for x in c[i-19:i+1])/20)**.5 for i in range(n)]
    upper = [None if s is None else m+2*s for m, s in zip(mid, std)]
    lower = [None if s is None else m-2*s for m, s in zip(mid, std)]
    add("boll", "布林通道（20／2，母體標準差）", "價格", {"收盤": c, "中軌": mid, "上軌": upper, "下軌": lower})
    add("bandwidth", "布林帶寬", "%", {"帶寬": [None if m in (None, 0) else 400*s/m for m,s in zip(mid,std)]})
    add("bias", "乖離率", "%", {f"BIAS{p}": [None if m in (None,0) else (x/m-1)*100 for x,m in zip(c,_simple_moving_average(c,p))] for p in (5,20,60)}, (0,))
    add("roc", "變動率 ROC（12）", "%", {"ROC": [None if i<12 or c[i-12]==0 else (c[i]/c[i-12]-1)*100 for i in range(n)]}, (0,))
    add("momentum", "動量 MOM（10）", "價差", {"MOM": [None if i<10 else c[i]-c[i-10] for i in range(n)]}, (0,))
    add("volume", "成交量與均量", "股", {"成交量": v, "均量5": _simple_moving_average(v,5), "均量20": _simple_moving_average(v,20)})
    add("value", "成交金額", "元", {"成交金額": [b.trading_value for b in bars]})
    returns = [None if i == 0 or c[i-1] == 0 else (c[i]/c[i-1]-1)*100 for i in range(n)]
    add("returns", "日報酬率", "%", {"日報酬": returns}, (0,))
    volatility = [None]*n
    for i in range(20,n):
        window = returns[i-19:i+1]
        if all(x is not None for x in window):
            mean = sum(window)/20
            volatility[i] = (sum((x-mean)**2 for x in window)/19*252)**.5
    add("volatility", "歷史波動率（20日，年化252日）", "%", {"波動率": volatility})
    add("volume_ratio", "量比（今日量／前20日均量）", "倍", {"量比": [None if i<20 or sum(v[i-20:i])==0 else v[i]*20/sum(v[i-20:i]) for i in range(n)]}, (1,))
    peak = c[0]
    drawdown = []
    for close in c:
        peak = max(peak, close)
        drawdown.append((close/peak-1)*100 if peak else None)
    add("drawdown", "距區間歷史高點回落", "%", {"回落": drawdown}, (0,))
    add("daily_vwap", "每日成交均價（金額／股數）", "價格", {"成交均價": [b.trading_value/b.volume if b.trading_value is not None and b.volume else None for b in bars], "收盤": c})
    obv, vpt = [0.0], [0.0]
    for i in range(1,n):
        obv.append(obv[-1]+v[i]*(1 if c[i]>c[i-1] else -1 if c[i]<c[i-1] else 0))
        vpt.append(vpt[-1]+(v[i]*(c[i]/c[i-1]-1) if c[i-1] else 0))
    add("obv", "OBV（區間起點為零）", "股", {"OBV": obv})
    add("vpt", "VPT（區間起點為零）", "量價", {"VPT": vpt})
    add("vwma", "成交量加權均線（20）", "價格", {"收盤": c, "VWMA20": [None if i<19 or sum(v[i-19:i+1])==0 else sum(c[j]*v[j] for j in range(i-19,i+1))/sum(v[i-19:i+1]) for i in range(n)]})
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    wr, cci, atr, mfi = ([None]*n for _ in range(4))
    tr = [None if b.high is None or b.low is None else max(b.high-b.low, abs(b.high-c[i-1]), abs(b.low-c[i-1])) if i else b.high-b.low for i,b in enumerate(bars)]
    typical = [None if b.high is None or b.low is None else (b.high+b.low+b.close)/3 for b in bars]
    for i in range(n):
        if i>=13 and all(x is not None for x in highs[i-13:i+1]+lows[i-13:i+1]):
            h,l = max(highs[i-13:i+1]),min(lows[i-13:i+1])
            wr[i] = -50 if h==l else -100*(h-c[i])/(h-l)
        if i>=13 and all(x is not None for x in tr[i-13:i+1]):
            atr[i] = (atr[i-1]*13+tr[i])/14 if i and atr[i-1] is not None else sum(tr[i-13:i+1])/14
        if i>=19 and all(x is not None for x in typical[i-19:i+1]):
            window=typical[i-19:i+1]
            mean=sum(window)/20
            dev=sum(abs(x-mean) for x in window)/20
            cci[i]=(typical[i]-mean)/(.015*dev) if dev else 0
        if i>=14 and all(x is not None for x in typical[i-14:i+1]):
            positive=sum(typical[j]*v[j] for j in range(i-13,i+1) if typical[j]>typical[j-1])
            negative=sum(typical[j]*v[j] for j in range(i-13,i+1) if typical[j]<typical[j-1])
            mfi[i]=100*positive/(positive+negative) if positive+negative else 50
    add("wr", "Williams %R（14，需要高低價）", "%", {"%R": wr}, (-80,-20))
    add("cci", "CCI（20，需要高低價）", "數值", {"CCI": cci}, (-100,100))
    add("atr", "ATR（Wilder 14，需要高低價）", "價格", {"ATR": atr})
    add("mfi", "MFI（14，需要高低價）", "數值", {"MFI": mfi}, (20,80))
    return charts


def load_local_technical(db_path, ticker, price_history=()):
    """唯讀本地 DB，合併已快取 OHLCV；同日以 DB 收盤量價為準。"""
    rows = {r["date"]: dict(date=r["date"], open=r.get("open"), max=r.get("high"), min=r.get("low"), close=r.get("close"), Trading_Volume=r.get("volume"), Trading_money=r.get("trading_value")) for r in price_history or ()}
    path = Path(db_path)
    if path.exists():
        with sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True) as conn:
            for day, close, volume, value in conn.execute("SELECT date, close, volume, trading_value FROM daily_prices WHERE ticker=? ORDER BY date", (ticker,)):
                if close is None:
                    continue
                row=rows.setdefault(day, {"date":day})
                # 不混用不同收盤基準的 OHLC（例如除權息調整）。
                if row.get("close") is not None and row["close"] != close:
                    for key in ("open", "max", "min"):
                        row.pop(key, None)
                row.update(close=close, Trading_Volume=volume, Trading_money=value)
    return calculate_indicators(list(rows.values()))
