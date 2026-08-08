"""tradingnote - FinMind API 用戶端（本益比／殖利率／三大法人買賣超）。

跟 tradingnote_core.py／tradingnote_history.py 同一套原則：不依賴任何介面，
CLI／GUI 都可以各自 import。「個股」模組使用者雙擊個股、跟「AI 助理」的工具
呼叫都會用到，不影響其餘功能（部位查價、產業資金流向）原本用的 TWSE/TPEX
快取機制。這兩個呼叫點共用同一份行程內資料快取（見 `_dataset_cache`），避免
同一檔股票在短時間內被重複查詢時（例如「個股」頁連點兩下、或 AI 助理在同一段
對話裡被問到同一檔股票兩次）白白多打 FinMind API、更快把免費額度用完。

`fetch_valuation()` 對上櫃（TPEX）股票會改查 tradingnote_core 的 TPEX 官方
估值端點，不是 FinMind（見該函式 docstring）；三大法人買賣超沒有對應的 TPEX
官方端點，不分市場一律仍查 FinMind。
"""

import time
from datetime import date, timedelta
from urllib.parse import urlencode

from tradingnote_cache import TTLCache
from tradingnote_core import get_tpex_valuation
from tradingnote_http import PriceFetchError, http_get_json

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

# FinMind 免費方案額度：600 次／小時（依官方文件），超過會回 HTTP 402。
FINMIND_HOURLY_LIMIT = 600

# 本益比／殖利率／三大法人買賣超這兩個資料集本來就是一天只更新一次，快取 30 分鐘
# 只是為了擋掉短時間內的重複查詢，不追求跟盤中即時報價一樣的新鮮度。
DATASET_CACHE_TTL_SECONDS = 1800

# FinMind「三大法人買賣超」資料集的 name 欄位代碼 → 中文顯示名稱，
# 顯示順序影響力由大到小：外資、外資自營商、投信、自營商自行買賣、自營商避險。
INSTITUTIONAL_NAME_LABELS = {
    "Foreign_Investor": "外資",
    "Foreign_Dealer_Self": "外資自營商",
    "Investment_Trust": "投信",
    "Dealer_self": "自營商(自行買賣)",
    "Dealer_Hedging": "自營商(避險)",
    "Dealer": "自營商(合併，舊制)",
}
INSTITUTIONAL_NAME_ORDER = list(INSTITUTIONAL_NAME_LABELS)

# 每次打 FinMind API 的時間戳記，給「個股」／「AI 助理」頁顯示用量統計；
# 只存在記憶體中，重啟程式會歸零，不代表 FinMind 帳號其他來源的真實用量。
_call_timestamps = []

# key 是 (dataset, ticker, lookback_days)。「個股」雙擊查詢跟「AI 助理」工具
# 呼叫都走 _fetch_dataset，共用同一份，命中快取時不會被算進 _call_timestamps
# （用量統計只反映真的打出去的 API 次數）。只快取成功的結果（包含查無資料的
# 空陣列，因為 TTLCache.get_or_fetch 只在 fetch_fn 正常回傳時才存入）；連線
# 失敗的例外會直接往上拋，不快取，下次呼叫照樣重試。
_dataset_cache = TTLCache(DATASET_CACHE_TTL_SECONDS)


def get_call_count(window_seconds=3600):
    """回傳過去 window_seconds 秒內（預設 1 小時，對齊免費方案 600 次／小時的
    額度）呼叫過幾次 FinMind API；順便把過期的時間戳記清掉，避免無限累積。"""
    global _call_timestamps
    cutoff = time.time() - window_seconds
    _call_timestamps = [t for t in _call_timestamps if t >= cutoff]
    return len(_call_timestamps)


def _fetch_dataset(dataset, ticker, token, lookback_days=10):
    """打 FinMind v4 /data 端點，回傳 data 陣列（依日期由舊到新）。抓一段區間
    （預設 10 天）而不是單一天，是因為假日、盤後資料延遲時當天可能還沒有資料，
    用區間取最後一筆確保拿得到最新的一筆。命中 _dataset_cache（30 分鐘內查過
    同一個 dataset／ticker／lookback_days 組合）就直接回傳，不重打 API。"""

    def _do_fetch():
        end = date.today()
        start = end - timedelta(days=lookback_days)
        params = {
            "dataset": dataset,
            "data_id": ticker,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        if token:
            params["token"] = token
        url = f"{FINMIND_URL}?{urlencode(params)}"
        _call_timestamps.append(time.time())
        payload = http_get_json(url)
        if payload.get("msg") != "success":
            raise PriceFetchError(f"FinMind 回應異常：{payload.get('msg')}")
        return payload.get("data") or []

    return _dataset_cache.get_or_fetch((dataset, ticker, lookback_days), _do_fetch)


def fetch_valuation(ticker, token, lookback_days=10, market=None):
    """回傳最新一筆本益比／殖利率／股價淨值比：
    {"date":, "per":, "pbr":, "dividend_yield":}；查無資料回傳 None。

    market="TPEX" 時改查 tradingnote_core.get_tpex_valuation()（TPEX 官方
    tpex_mainboard_peratio_analysis，實測跟 FinMind 同一天數值完全一致），
    不吃 FinMind 額度、也不進 _dataset_cache／_call_timestamps；查無資料
    （例如虧損公司算不出本益比）直接回傳 None，不 fallback 回 FinMind。
    market 是 TWSE 或未提供時，行為跟原本一樣打 FinMind。"""
    if market == "TPEX":
        return get_tpex_valuation(ticker)

    rows = _fetch_dataset("TaiwanStockPER", ticker, token, lookback_days)
    if not rows:
        return None
    latest = rows[-1]
    return {
        "date": latest.get("date"),
        "per": latest.get("PER"),
        "pbr": latest.get("PBR"),
        "dividend_yield": latest.get("dividend_yield"),
    }


def fetch_institutional_investors(ticker, token, lookback_days=10):
    """回傳最新交易日的三大法人買賣超：
    {"date":, "breakdown": [{"label":, "buy":, "sell":, "net":}, ...]}；
    breakdown 依 INSTITUTIONAL_NAME_ORDER 排序；查無資料回傳 None。"""
    rows = _fetch_dataset(
        "TaiwanStockInstitutionalInvestorsBuySell", ticker, token, lookback_days
    )
    if not rows:
        return None
    latest_date = max(row["date"] for row in rows)
    by_name = {row["name"]: row for row in rows if row["date"] == latest_date}

    breakdown = []
    for name in INSTITUTIONAL_NAME_ORDER:
        row = by_name.get(name)
        if row is None:
            continue
        buy = row.get("buy") or 0
        sell = row.get("sell") or 0
        breakdown.append(
            {
                "label": INSTITUTIONAL_NAME_LABELS[name],
                "buy": buy,
                "sell": sell,
                "net": buy - sell,
            }
        )
    return {"date": latest_date, "breakdown": breakdown}
