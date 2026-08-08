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

本模組另外查詢融資融券餘額（TaiwanStockMarginPurchaseShortSale）、外資持股比例
（TaiwanStockShareholding）、借券成交（TaiwanStockSecuritiesLending）、停資停券
公告（TaiwanStockMarginShortSaleSuspension），供「部位紀錄」頁籌碼面資訊使用，
一樣不分市場一律查 FinMind（沒有對應的 TPEX 官方端點）。故意不查的兩個資料集：
①TaiwanStockHoldingSharesPer（股權分散表/大戶持股）——實測需要 FinMind 付費
Sponsor 方案，免費 token 回 HTTP 400「Your level is register. Please update
your user level.」，拿不到資料；②TaiwanDailyShortSaleBalances——欄位
（SBLShortSales*／MarginShortSales*）跟 TaiwanStockMarginPurchaseShortSale 的
ShortSale* 系列大量重複，多打一次 API 換不到多少新資訊，不值得。
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

# 「三大法人」標準三分類（外資含外資自營商合併計、投信、自營商自行+避險合併計），
# 給 fetch_institutional_investors_history 的120日趨勢圖用；跟上面逐一細分的
# INSTITUTIONAL_NAME_LABELS（StockDetailDialog 單日明細用）是兩種不同的呈現粒度。
INSTITUTIONAL_BUCKETS = [
    ("外資", ("Foreign_Investor", "Foreign_Dealer_Self")),
    ("投信", ("Investment_Trust",)),
    ("自營商", ("Dealer_self", "Dealer_Hedging", "Dealer")),
]

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


def fetch_margin_short_sale_history(ticker, token, lookback_days=120):
    """回傳近 lookback_days 天融資融券餘額逐日資料＋最新一天摘要，供「部位紀錄」
    頁融資融券趨勢圖與文字摘要共用同一次 API 呼叫（同一份 TaiwanStockMargin
    PurchaseShortSale 資料只打一次，摘要取最後一筆、趨勢圖用全部，不像
    fetch_valuation／fetch_institutional_investors_history 用不同 lookback_days
    各自快取、各打一次）。

    回傳：
    {
        "dates": [...],                                     # 由舊到新
        "series": {"融資餘額": [...], "融券餘額": [...]},     # 逐日「餘額」（張），
                                                              # 跟三大法人買賣超那種
                                                              # 逐日「淨變化」不同，
                                                              # 畫圖要直接畫原始值，
                                                              # 不能再累加一次
        "latest": {
            "date":, "margin_balance":, "margin_change":,
            "short_balance":, "short_change":,
        },
    }
    查無資料回傳 None。"""
    rows = _fetch_dataset(
        "TaiwanStockMarginPurchaseShortSale", ticker, token, lookback_days
    )
    if not rows:
        return None

    rows = sorted(rows, key=lambda r: r["date"])
    dates = [r["date"] for r in rows]
    margin_series = [r.get("MarginPurchaseTodayBalance") or 0 for r in rows]
    short_series = [r.get("ShortSaleTodayBalance") or 0 for r in rows]

    latest = rows[-1]
    return {
        "dates": dates,
        "series": {"融資餘額": margin_series, "融券餘額": short_series},
        "latest": {
            "date": latest["date"],
            "margin_balance": latest.get("MarginPurchaseTodayBalance"),
            "margin_change": (latest.get("MarginPurchaseTodayBalance") or 0)
            - (latest.get("MarginPurchaseYesterdayBalance") or 0),
            "short_balance": latest.get("ShortSaleTodayBalance"),
            "short_change": (latest.get("ShortSaleTodayBalance") or 0)
            - (latest.get("ShortSaleYesterdayBalance") or 0),
        },
    }


def fetch_foreign_shareholding(ticker, token, lookback_days=10):
    """回傳最新交易日外資持股比例：{"date":, "ratio":, "change":}。change 是跟
    區間內次新一筆比較的變化（百分點）；區間內只有一筆資料，或次新一筆缺值時，
    change 回傳 None。查無資料回傳 None。"""
    rows = _fetch_dataset("TaiwanStockShareholding", ticker, token, lookback_days)
    if not rows:
        return None

    rows = sorted(rows, key=lambda r: r["date"])
    latest = rows[-1]
    ratio = latest.get("ForeignInvestmentSharesRatio")

    change = None
    if len(rows) >= 2 and ratio is not None:
        prev_ratio = rows[-2].get("ForeignInvestmentSharesRatio")
        if prev_ratio is not None:
            change = ratio - prev_ratio

    return {"date": latest["date"], "ratio": ratio, "change": change}


def fetch_securities_lending_summary(ticker, token, lookback_days=10):
    """回傳最新交易日借券成交彙總：{"date":, "volume":, "avg_fee_rate":}。
    TaiwanStockSecuritiesLending 是逐筆成交資料（同一天可能有多筆不同費率的
    成交紀錄），volume 是當天全部成交量加總（張），avg_fee_rate 是用成交量
    加權的平均費率；當天總量為 0 時 avg_fee_rate 回傳 None（避免除以 0）。
    查無資料回傳 None。"""
    rows = _fetch_dataset("TaiwanStockSecuritiesLending", ticker, token, lookback_days)
    if not rows:
        return None

    latest_date = max(row["date"] for row in rows)
    day_rows = [row for row in rows if row["date"] == latest_date]
    total_volume = sum(row.get("volume") or 0 for row in day_rows)

    avg_fee_rate = None
    if total_volume > 0:
        weighted_fee = sum(
            (row.get("volume") or 0) * (row.get("fee_rate") or 0) for row in day_rows
        )
        avg_fee_rate = weighted_fee / total_volume

    return {"date": latest_date, "volume": total_volume, "avg_fee_rate": avg_fee_rate}


def fetch_margin_short_sale_suspension(ticker, token, lookback_days=90):
    """回傳區間內的停資停券公告清單（依日期由新到舊）。這個資料集本來就很少有
    資料（多數股票多數期間都沒有），lookback_days 預設拉長到 90 天（其他函式
    預設 10 天），降低區間太短漏掉近期公告的機率。

    回傳 [{"date":, "end_date":, "reason":}, ...]；沒有公告（查無資料）回傳
    None——GUI 端只在非 None 時才顯示警示行，平常不佔任何版面。"""
    rows = _fetch_dataset(
        "TaiwanStockMarginShortSaleSuspension", ticker, token, lookback_days
    )
    if not rows:
        return None

    rows = sorted(rows, key=lambda r: r["date"], reverse=True)
    return [
        {"date": r.get("date"), "end_date": r.get("end_date"), "reason": r.get("reason")}
        for r in rows
    ]


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


def fetch_institutional_investors_history(ticker, token, lookback_days=120):
    """回傳近 lookback_days 天三大法人買賣超逐日資料，聚合成三大類（外資／投信／
    自營商，見 INSTITUTIONAL_BUCKETS），供「部位紀錄」頁120日資金流向趨勢圖用；
    跟 fetch_institutional_investors 只回傳最新一天、且不分類別合併不同，這裡是
    完整區間、且每天都合併好三大類。

    回傳 {"dates": [...], "series": {"外資": [...], "投信": [...], "自營商": [...]}}，
    dates 由舊到新排序，series 逐日淨買賣超（股數，買-賣），跟 dates 一一對應；
    查無資料回傳 None。lookback_days 是走 _fetch_dataset 同一套快取（key 含
    lookback_days），跟 fetch_institutional_investors 預設的 10 天是分開的快取項目。"""
    rows = _fetch_dataset(
        "TaiwanStockInstitutionalInvestorsBuySell", ticker, token, lookback_days
    )
    if not rows:
        return None

    by_date = {}
    for row in rows:
        by_date.setdefault(row["date"], {})[row["name"]] = row

    dates = sorted(by_date)
    series = {label: [] for label, _names in INSTITUTIONAL_BUCKETS}
    for d in dates:
        day_rows = by_date[d]
        for label, names in INSTITUTIONAL_BUCKETS:
            net = 0
            for name in names:
                row = day_rows.get(name)
                if row is not None:
                    net += (row.get("buy") or 0) - (row.get("sell") or 0)
            series[label].append(net)

    return {"dates": dates, "series": series}
