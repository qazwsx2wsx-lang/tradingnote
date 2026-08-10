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

`backfill_tpex_history_via_finmind()` 用 TaiwanStockPrice 資料集補上 TPEX
（上櫃）沒有官方免費歷史端點的缺口（見 tradingnote_history.py「TWSE 歷史回補」
區塊），逐檔呼叫、額度用完會提早停止並回傳目前進度，見該函式 docstring。
"""

import time
from datetime import date, timedelta
from urllib.parse import urlencode

from tradingnote_cache import TTLCache, load_keyed_store, save_keyed_entry
from tradingnote_core import get_tpex_valuation
from tradingnote_history import (
    DEFAULT_BACKFILL_LOOKBACK_CAP_DAYS,
    DEFAULT_BACKFILL_TARGET_DAYS,
    get_industry_directory,
    get_ticker_history_day_counts,
    upsert_daily_prices,
)
from tradingnote_http import PriceFetchError, http_get_json

FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

# FinMind 免費方案額度：600 次／小時（依官方文件），超過會回 HTTP 402。
FINMIND_HOURLY_LIMIT = 600

# TPEX 補歷史資料（backfill_tpex_history_via_finmind）逐檔呼叫之間的節流延遲，
# 跟 tradingnote_history.DEFAULT_BACKFILL_DELAY_SECONDS（TWSE 逐日回補用）同數值，
# 這裡獨立宣告一份，避免只為了共用一個常數就讓兩個回補機制互相 import 對方。
DEFAULT_TPEX_BACKFILL_DELAY_SECONDS = 0.3

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


def fetch_vpt_mfi_history(ticker, token, lookback_days=150, mfi_period=14):
    """回傳近 lookback_days 天（日曆天）VPT／MFI 逐日序列，供「部位紀錄」頁
    量價趨勢圖使用。用同一次 TaiwanStockPrice 呼叫同時取得 close/high/low/volume
    （跟 fetch_stock_price_history() 不同，那個函式是給 TPEX 回補用、故意丟掉
    high/low；這裡要算 MFI 需要 typical price 所以保留）。不分市場一律查
    FinMind（TaiwanStockPrice 上市櫃通用，理由同 fetch_stock_price_history()）。

    VPT（Volume Price Trend）：VPT[0] = 0，
    VPT[t] = VPT[t-1] + volume[t] * (close[t]-close[t-1]) / close[t-1]。

    MFI（Money Flow Index，預設 14 日）：
    typical price TP[t] = (high[t]+low[t]+close[t]) / 3；
    raw money flow RMF[t] = TP[t] * volume[t]；
    TP[t] > TP[t-1] 時計入正流量，TP[t] < TP[t-1] 時計入負流量，相等不計入任何一邊；
    money flow ratio = 近 mfi_period 天正流量總和 / 負流量總和；
    MFI = 100 - 100/(1+ratio)（負流量總和為 0 時 MFI = 100，避免除以 0）。

    回傳最前面 mfi_period 天（沒有完整前一日可比較／視窗不足以算出第一個 MFI 值）
    會被裁掉，讓 dates／vpt／mfi 三個陣列長度一致、都對得上同一段可畫的日期。
    回傳 {"dates":, "vpt":, "mfi":}；資料不足 mfi_period+1 天則回傳 None。"""
    rows = _fetch_dataset("TaiwanStockPrice", ticker, token, lookback_days)
    rows = sorted(
        (
            r
            for r in rows
            if r.get("close") is not None
            and r.get("max") is not None
            and r.get("min") is not None
        ),
        key=lambda r: r["date"],
    )
    if len(rows) <= mfi_period:
        return None

    dates_all = [r["date"] for r in rows]
    closes = [r["close"] for r in rows]
    volumes = [r.get("Trading_Volume") or 0 for r in rows]
    typical = [(r["max"] + r["min"] + r["close"]) / 3 for r in rows]

    vpt = [0.0]
    for t in range(1, len(rows)):
        prev_close = closes[t - 1]
        change = (closes[t] - prev_close) / prev_close if prev_close else 0.0
        vpt.append(vpt[-1] + volumes[t] * change)

    raw_flow = [typical[t] * volumes[t] for t in range(len(rows))]
    mfi = [None] * len(rows)
    for t in range(mfi_period, len(rows)):
        pos_flow = neg_flow = 0.0
        for i in range(t - mfi_period + 1, t + 1):
            if typical[i] > typical[i - 1]:
                pos_flow += raw_flow[i]
            elif typical[i] < typical[i - 1]:
                neg_flow += raw_flow[i]
        mfi[t] = 100.0 if neg_flow == 0 else 100 - 100 / (1 + pos_flow / neg_flow)

    return {
        "dates": dates_all[mfi_period:],
        "vpt": vpt[mfi_period:],
        "mfi": mfi[mfi_period:],
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


# ---------- 「部位紀錄」頁個股詳細資訊：一次抓齊六項＋永久存檔 ----------

# fetch_position_detail() 回傳 dict 的固定欄位順序，跟 GUI 顯示順序一致；
# 用 dict（key 存取）取代原本的 6 元組定位取值，是因為這份結果現在還要
# 存進 position_detail_cache.json 永久保存，往後增減欄位只需要改這裡跟
# 讀取端對應的 key，不用同步調整每一處按位置解包的程式碼。
POSITION_DETAIL_FIELDS = (
    "valuation",
    "institutional_history",
    "margin_history",
    "foreign_shareholding",
    "lending",
    "suspension",
    "vpt_mfi_history",
)


def fetch_position_detail(ticker, token, market=None):
    """一次抓齊「部位紀錄」頁個股詳細資訊區塊要顯示的七項資料（本益比／殖利率／
    股價淨值比、三大法人120日趨勢、融資融券120日趨勢、外資持股比例、借券成交、
    停資停券公告、VPT／MFI量價指標），回傳 dict（key 見 POSITION_DETAIL_FIELDS）。
    每項各自沿用原本的 _dataset_cache（30分鐘行程內快取，擋短時間內重複查詢），
    呼叫端（tradingnote_gui._load_position_detail）另外會把整份結果存進
    position_detail_cache.json 永久保存（見 save_position_detail_cache），
    跟 _dataset_cache 是兩層不同用途：一層擋重複打 API，一層讓資料重開程式
    也不會消失。"""
    return {
        "valuation": fetch_valuation(ticker, token, market=market),
        "institutional_history": fetch_institutional_investors_history(
            ticker, token, lookback_days=120
        ),
        "margin_history": fetch_margin_short_sale_history(ticker, token, lookback_days=120),
        "foreign_shareholding": fetch_foreign_shareholding(ticker, token),
        "lending": fetch_securities_lending_summary(ticker, token),
        "suspension": fetch_margin_short_sale_suspension(ticker, token),
        "vpt_mfi_history": fetch_vpt_mfi_history(ticker, token),
    }


def load_position_detail_cache(cache_path, ticker):
    """讀 ticker 上次成功呼叫 fetch_position_detail() 並存檔的結果（含
    "fetched_at"），從沒抓過這檔就回傳 None。永久保存、不判斷新鮮度／不會
    過期——目前策略是每次選取部位都在背景重新查一次、查到就覆寫（見
    tradingnote_gui._load_position_detail），這裡單純負責讀，新鮮度交給
    呼叫端決定要不要顯示、要不要重查。"""
    return load_keyed_store(cache_path).get(ticker)


def save_position_detail_cache(cache_path, ticker, data):
    """把 fetch_position_detail() 的結果存進 cache_path（以 ticker 為 key，
    自動補上 fetched_at），永久保存、不會過期，也不會動到其他 ticker 已存的
    記錄。"""
    save_keyed_entry(cache_path, ticker, data)


# ---------- TPEX 歷史回補（FinMind 補上官方端點沒有的缺口） ----------

def fetch_stock_price_history(ticker, token, lookback_days):
    """回傳近 lookback_days 天（日曆天，非交易日天數）的收盤價／成交量歷史，供
    backfill_tpex_history_via_finmind() 補齊上櫃歷史資料使用。TaiwanStockPrice
    上市櫃通用、一次呼叫回傳整段區間，不像 TWSE 官方端點（tradingnote_history.
    fetch_twse_historical_day）得逐日各打一次 API。

    回傳 [{"date":, "close":, "change_pct":, "volume":, "trading_value":}, ...]，
    依日期由舊到新排序；收盤價缺值（例如當天停牌）的列會被濾掉，因為
    daily_prices 其餘計算（產業資金流向等）都假設 close 有值。查無資料回傳
    空列表（不是 None，呼叫端可以直接當空清單處理，不用另外判斷）。"""
    rows = _fetch_dataset("TaiwanStockPrice", ticker, token, lookback_days)
    result = []
    for row in sorted(rows, key=lambda r: r["date"]):
        close = row.get("close")
        if close is None:
            continue
        spread = row.get("spread")
        prev_close = close - spread if spread is not None else None
        change_pct = (spread / prev_close * 100) if prev_close else None
        result.append(
            {
                "date": row["date"],
                "close": close,
                "change_pct": change_pct,
                "volume": row.get("Trading_Volume"),
                "trading_value": row.get("Trading_money"),
            }
        )
    return result


def backfill_tpex_history_via_finmind(
    db_path,
    token,
    target_days=DEFAULT_BACKFILL_TARGET_DAYS,
    delay_seconds=DEFAULT_TPEX_BACKFILL_DELAY_SECONDS,
    on_progress=None,
):
    """上櫃（TPEX）沒有像 tradingnote_history.backfill_twse_history 那樣的官方免費
    歷史端點，改用 FinMind TaiwanStockPrice 資料集逐檔補齊。

    跟 backfill_twse_history 同一套「可安全中斷後重跑」的冪等設計，但判斷單位是
    「檔」而非「天」：檢查 daily_prices 裡這檔股票在 TPEX 底下已有的交易日數，
    達到 target_days 天就跳過、不重打 API。**不需要另外記錄「補到哪裡」**——完成
    度直接看 daily_prices 本身即可，中斷後重跑會自動從還沒補到 target_days 天的
    股票接續，跟 TWSE 那份「已存在的日期會跳過」是同一個原則，只是判斷單位從
    「天」換成「檔」。

    FinMind 免費額度只有 600 次／小時（見 FINMIND_HOURLY_LIMIT），全市場上櫃約
    800 檔，一次呼叫通常補不完：每打一檔前用 get_call_count() 檢查目前用量
    （這是行程內的呼叫次數統計，也反映「個股」「部位紀錄」「AI 助理」等其他頁面
    同時在用的額度，不會互相搶到超過真正的帳號上限），達到上限就提早停止並回傳
    目前進度，不會真的打到 FinMind 回 HTTP 402 才發現。額度是帳號層級、每小時
    滾動重置，之後再次呼叫（例如使用者在「設定」頁重新點一次按鈕）會自動跳過已
    補足的股票、接續尚未補到 target_days 天的部分，不需要等程式自動重跑。

    `on_progress(done, total, ticker)` 每處理完一檔（不論新補或本來就已跳過）就
    呼叫一次，讓呼叫端可以顯示「已檢查 X/Y 檔」的進度。

    回傳 {"done": 已達 target_days 天的檔數（含本次新補的）, "total": 上櫃總檔數,
    "newly_fetched": 本次新打 API 的檔數, "stopped_reason": "quota_exhausted" 或
    None（正常跑完全部）}。industry_map 尚未建立、查無上櫃股票清單時回傳全 0。"""
    directory = get_industry_directory(db_path)
    tpex_tickers = sorted(t for t, info in directory.items() if info["market"] == "TPEX")
    total = len(tpex_tickers)
    if not tpex_tickers:
        return {"done": 0, "total": 0, "newly_fetched": 0, "stopped_reason": None}

    lookback_days = max(DEFAULT_BACKFILL_LOOKBACK_CAP_DAYS, int(target_days * 1.6))
    day_counts = get_ticker_history_day_counts(db_path, tpex_tickers, market="TPEX")

    done = sum(1 for t in tpex_tickers if day_counts.get(t, 0) >= target_days)
    newly_fetched = 0
    stopped_reason = None

    for ticker in tpex_tickers:
        if day_counts.get(ticker, 0) >= target_days:
            continue
        if get_call_count() >= FINMIND_HOURLY_LIMIT:
            stopped_reason = "quota_exhausted"
            break

        rows = fetch_stock_price_history(ticker, token, lookback_days)
        newly_fetched += 1
        time.sleep(delay_seconds)

        if rows:
            name = directory[ticker]["name"]
            upsert_daily_prices(
                db_path,
                [
                    (
                        r["date"],
                        ticker,
                        "TPEX",
                        name,
                        r["close"],
                        r["change_pct"],
                        r["volume"],
                        r["trading_value"],
                    )
                    for r in rows
                ],
            )
            done += 1

        if on_progress:
            on_progress(done, total, ticker)

    return {
        "done": done,
        "total": total,
        "newly_fetched": newly_fetched,
        "stopped_reason": stopped_reason,
    }
