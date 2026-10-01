"""GUI 顯示用的小型格式化 helper（跟 theme 搭配，不含業務邏輯）。"""

from collections import Counter
from datetime import datetime

from tradingnote_taifex import LARGE_TRADERS_ALL_CONTRACTS_MONTH

from ui.theme import COLOR_GAIN, COLOR_LOSS


def gain_loss_color(value):
    """依正負回傳台股慣例的漲跌色（漲＝紅、跌＝綠）；value 為 None 或 0 視為漲
    （沿用既有 `COLOR_GAIN if x >= 0 else COLOR_LOSS` 三元式的既有行為，不改語意）。"""
    return COLOR_GAIN if (value is None or value >= 0) else COLOR_LOSS


def _compact_money(value):
    """把金額縮成適合摘要卡與提示框閱讀的億元格式。"""
    if value is None:
        return "—"
    return f"{value / 1e8:+,.1f} 億"


def _snapshot_date(snapshot):
    """回傳快照裡最多股票共用的交易日（多數 PriceInfo.date 會是同一天，用眾數
    避免少數個股資料延遲／異常日期影響判斷）；快照是空的就回傳 None。"""
    dates = [p.date for p in snapshot.values() if p.date]
    if not dates:
        return None
    return Counter(dates).most_common(1)[0][0]


def _futures_snapshot_date(futures_snapshot):
    """回傳期貨盤後快照（get_futures_snapshot 的回傳值）裡最新的資料日期，格式轉成
    跟 _snapshot_date／history.db 一致的 "YYYY-MM-DD"（TAIFEX 原始欄位是 "YYYYMMDD"）；
    快照是空的就回傳 None。"""
    dates = [
        data["date"]
        for sessions in futures_snapshot.values()
        for data in sessions.values()
        if data.get("date")
    ]
    if not dates:
        return None
    raw = max(dates)
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


def _futures_date_to_iso(raw):
    """TAIFEX 原始日期欄位 "YYYYMMDD" → "YYYY-MM-DD"；格式不符或空值回傳空字串。"""
    if raw and len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return ""


def _format_settlement_month(month):
    """大額交易人未沖銷部位的 SettlementMonth 顯示文字：999912（TAIFEX 用來表示
    「所有契約合計」）轉成「所有契約」；正常的 6 碼西元年月轉成 "YYYY/MM"；
    其他非標準代碼（例如 TX 偶爾出現、全市場未沖銷僅 1 口的 666666 佔位資料）
    原樣顯示，不臆測其語意。"""
    if month == LARGE_TRADERS_ALL_CONTRACTS_MONTH:
        return "所有契約"
    if month and len(month) == 6 and month.isdigit() and "01" <= month[4:6] <= "12":
        return f"{month[0:4]}/{month[4:6]}"
    return month or "-"


_MARKET_LABELS = {"TWSE": "上市 TWSE", "TPEX": "上櫃 TPEX"}


def _format_history_status(status):
    overall = status["overall"]
    if not overall["days"]:
        return "資料庫目前無歷史資料，請按「回補歷史資料」或等待下次自動同步。"

    lines = [
        f"整體：{overall['days']} 個交易日｜{overall['min_date']} ～ {overall['max_date']}"
        f"｜{overall['tickers']:,} 檔｜{overall['rows']:,} 筆",
    ]
    for market, label in _MARKET_LABELS.items():
        m = status["by_market"].get(market)
        if m is None:
            continue
        lines.append(
            f"{label}：{m['days']} 個交易日｜{m['min_date']} ～ {m['max_date']}"
            f"｜{m['tickers']:,} 檔｜{m['rows']:,} 筆"
        )

    size_mb = status["file_size_bytes"] / (1024 * 1024)
    lines.append(f"資料庫檔案大小：{size_mb:.1f} MB")
    return "\n".join(lines)


def _format_fetched_at(iso_string):
    """把 position_detail_cache.json 的時間戳格式化成畫面可讀的文字。

    舊快取若有異常格式，不應該讓整個部位詳細資訊區塊無法顯示，直接保留
    原字串作為 fallback。
    """
    try:
        return datetime.fromisoformat(iso_string).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return iso_string


def _sign(value):
    return (value > 0) - (value < 0)
