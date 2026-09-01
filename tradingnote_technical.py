"""純本地技術指標計算。

本模組只處理已取得的日 OHLCV 資料，不依賴 PySide6，也不直接呼叫 API。
同一份價格序列可以同時計算 KD、MACD、均線、RSI、VPT 與 MFI，避免每個
指標各自查詢一次 FinMind。
"""

from dataclasses import dataclass


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
        return float(value) if value is not None else None
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
    return {
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
