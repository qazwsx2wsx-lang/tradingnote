"""tradingnote - TAIFEX（臺灣期貨交易所）官方期貨每日交易行情（盤後）用戶端。

原本「期貨」分頁串接 Fugle 的 data-futopt 即時行情，但查證 developer.fugle.tw
文件（pricing.md）後發現：futopt（期貨/選擇權）完全不在免費方案內——連歷史/
盤後資料都沒有，只有 intraday 且需要付費 Developer 方案（NT$1,499/月起）才能
呼叫。改用 TAIFEX 官方公開的「期貨每日交易行情」端點（openapi.taifex.com.tw，
無需 API Key、免費、無 header 限制），只反映「盤後」（EOD，含日盤/夜盤收盤後的
彙總）資訊，不是真即時報價——跟 TWSE/TPEX 股票走 EOD 而非即時的既有設計原則
一致（見 HANDOFF.md：「只有 EOD 資料，非即時報價」）。

這支端點沒有商品/日期參數，一次回傳「最近一個交易日」全市場所有期貨契約
（含價差單），呼叫端自行從裡面篩出想要的商品代碼與近月合約。
"""

from tradingnote_cache import load_fresh_file_cache, load_stale_file_cache, write_file_cache
from tradingnote_http import PriceFetchError, http_get_json, to_float, to_int

TAIFEX_DAILY_FUTURES_URL = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"

# 預設顯示商品：TX（臺股期貨，大台）／MTX（小型臺指期貨，小台），對應 TAIFEX
# 官方契約代號（不是 Fugle 舊版用的 TXF／MXF 命名）。
DEFAULT_FUTURES_PRODUCTS = ["TX", "MTX"]

# 比照 tradingnote_core.CACHE_TTL_SECONDS（個股快取）：這支端點本來就是一天只
# 更新一次（見 fetch_daily_futures_report），30 分鐘純粹是擋短時間內（例如搜尋
# 商品時）重複打 API。
FUTURES_CACHE_TTL_SECONDS = 30 * 60


def fetch_daily_futures_report():
    """打一次 DailyMarketReportFut，回傳最近一個交易日全部期貨契約的原始清單，
    每筆含 Contract／ContractMonth(Week)／TradingSession（"一般"＝日盤、
    "盤後"＝夜盤）等欄位，欄位名稱、型別（字串）均照 TAIFEX 原樣，正規化留給
    get_front_month_sessions 處理。"""
    return http_get_json(TAIFEX_DAILY_FUTURES_URL)


def _normalize_row(row):
    return {
        "date": row.get("Date"),
        "contract": row.get("Contract"),
        "contract_month": row.get("ContractMonth(Week)"),
        "session": row.get("TradingSession"),
        "open": to_float(row.get("Open")),
        "high": to_float(row.get("High")),
        "low": to_float(row.get("Low")),
        "last": to_float(row.get("Last")),
        "change": to_float(row.get("Change")),
        "change_pct": to_float(str(row.get("%") or "").rstrip("%")),
        "settlement_price": to_float(row.get("SettlementPrice")),
        "volume": to_int(row.get("Volume")),
        "open_interest": to_int(row.get("OpenInterest")),
    }


def get_front_month_sessions(product, rows):
    """從 fetch_daily_futures_report() 的原始清單裡篩出某商品（如 "TX"）目前
    近月合約的日盤／夜盤兩筆資料，回傳 {"一般": {...}, "盤後": {...}}（缺任一
    session 則該 key 不存在）。近月判斷：取 ContractMonth(Week) 為純 6 碼西元
    年月（不含「/」價差組合、不含「W」週別）中最小的一筆——當天最早到期、尚未
    輪替的契約；不落地存代號，每次都用當天資料動態算，跟舊版
    tradingnote_fugle.resolve_front_month_symbol 的近月判斷原則相同。
    查無該商品任何近月資料時回傳空 dict。"""
    candidates = [
        r
        for r in rows
        if r.get("Contract") == product
        and "/" not in (r.get("ContractMonth(Week)") or "")
        and "W" not in (r.get("ContractMonth(Week)") or "")
    ]
    if not candidates:
        return {}

    front_month = min(r["ContractMonth(Week)"] for r in candidates)
    return {
        _normalize_row(r)["session"]: _normalize_row(r)
        for r in candidates
        if r["ContractMonth(Week)"] == front_month
    }


def get_cached_daily_futures_report(cache_path, force_refresh=False):
    """比照 tradingnote_core.get_market_snapshot 的檔案快取模式：命中
    FUTURES_CACHE_TTL_SECONDS 內的快取就不重打 API，過期或 force_refresh 才
    真的呼叫 fetch_daily_futures_report()；API 失敗時退回舊快取（若有）而不是
    直接噴錯，跟個股快取失敗時的 fallback 邏輯一致。"""
    if not force_refresh:
        cached = load_fresh_file_cache(cache_path, FUTURES_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached["rows"]

    try:
        rows = fetch_daily_futures_report()
    except PriceFetchError:
        stale = load_stale_file_cache(cache_path)
        if stale is not None:
            return stale["rows"]
        raise

    write_file_cache(cache_path, {"rows": rows})
    return rows


def get_futures_snapshot(products=None, rows=None):
    """回傳 {product: {"一般": {...}, "盤後": {...}}, ...}。rows 若已由呼叫端
    先呼叫過 fetch_daily_futures_report() 取得，可傳入重用，避免重複打
    API——這支端點本來就是一次回傳全市場，一次抓取即可涵蓋所有商品。"""
    products = products or DEFAULT_FUTURES_PRODUCTS
    if rows is None:
        rows = fetch_daily_futures_report()
    return {product: get_front_month_sessions(product, rows) for product in products}


def list_all_products(rows):
    """從 fetch_daily_futures_report() 的原始清單裡取出所有不重複的商品代碼
    （Contract）並排序回傳，作為期貨搜尋功能的商品全集來源。"""
    return sorted({r["Contract"] for r in rows if r.get("Contract")})
