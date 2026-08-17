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

import csv
import io
import time
from datetime import date, timedelta

from tradingnote_cache import load_fresh_file_cache, load_stale_file_cache, write_file_cache
from tradingnote_history import (
    get_large_traders_history_date_range,
    upsert_large_traders_history,
)
from tradingnote_http import (
    PriceFetchError,
    http_get_json,
    http_post_text,
    to_float,
    to_int,
)
from urllib.parse import urlencode

TAIFEX_DAILY_FUTURES_URL = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"

# 大額交易人未沖銷部位（盤後）：同樣是 TAIFEX 官方公開、免金鑰端點，一次回傳
# 最近一個交易日全市場所有期貨契約的前5／前10大交易人買賣方未沖銷部位。
TAIFEX_LARGE_TRADERS_FUTURES_URL = (
    "https://openapi.taifex.com.tw/v1/OpenInterestOfLargeTradersFutures"
)

# TypeOfTraders 代碼 → 中文（TAIFEX 官方定義）：0＝所有交易人、1＝特定法人。
LARGE_TRADERS_TYPE_LABELS = {"0": "所有交易人", "1": "特定法人"}

# TAIFEX 用這個特殊 SettlementMonth 值代表「所有契約合計」（跨全部到期月份），
# 排序時固定排在真實月份之後。
LARGE_TRADERS_ALL_CONTRACTS_MONTH = "999912"

# 大額交易人未沖銷部位「歷史」下載端點（TAIFEX 網站，非 openapi）。openapi 的
# OpenInterestOfLargeTradersFutures 只回最新一天、沒有日期參數，要逐日歷史只能打
# 這支網站 CSV 下載端點（POST queryStartDate／queryEndDate、Big5 編碼）。實測單次
# 查詢的日期跨度有上限（90 天可、120 天會被擋回 HTML 錯誤頁），所以回補時要分段。
TAIFEX_LARGE_TRADERS_HISTORY_URL = "https://www.taifex.com.tw/cht/3/largeTraderFutDown"

# 單次歷史查詢的安全日期跨度（曆日）：實測 90 天可、120 天被擋，取 60 天保守留餘裕。
LARGE_TRADERS_HISTORY_MAX_SPAN_DAYS = 60

# 歷史 CSV 裡「所有契約合計」的到期月別代碼是 999999（跟 openapi 即時端點的 999912
# 不同！趨勢圖要查歷史表時用這個）。
LARGE_TRADERS_HISTORY_ALL_CONTRACTS_MONTH = "999999"

# 股票期貨交易標的清單端點：把股票期貨契約代碼（如 CDF）對應到標的股票
# （台積電 2330）。日盤行情端點只給不具名的契約代碼，靠這支補上標的名稱，
# 讓「期貨」頁看得懂、也能用股票代號／名稱搜尋。免金鑰、內容變動很少。
TAIFEX_SSF_LIST_URL = "https://openapi.taifex.com.tw/v1/SSFLists"

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


# ---------- 大額交易人未沖銷部位（OpenInterestOfLargeTradersFutures） ----------

def fetch_large_traders_futures_report():
    """打一次 OpenInterestOfLargeTradersFutures，回傳最近一個交易日全部期貨契約的
    「大額交易人未沖銷部位」原始清單。每筆含 Contract／ContractName／
    SettlementMonth（YYYYMM，999912＝所有契約合計）／TypeOfTraders（0＝所有
    交易人、1＝特定法人）／Top5Buy／Top5Sell／Top10Buy／Top10Sell（前5／前10大
    交易人買方／賣方未沖銷部位，口數）／OIOfMarket（全市場未沖銷部位）。欄位
    名稱、型別（字串）均照 TAIFEX 原樣，正規化留給 get_large_traders_for_product。
    跟 fetch_daily_futures_report 一樣沒有商品／日期參數，一次回傳全市場。"""
    return http_get_json(TAIFEX_LARGE_TRADERS_FUTURES_URL)


def _normalize_large_traders_row(row):
    return {
        "date": row.get("Date"),
        "contract": row.get("Contract"),
        "contract_name": row.get("ContractName"),
        "settlement_month": row.get("SettlementMonth"),
        "trader_type": row.get("TypeOfTraders"),
        "top5_buy": to_int(row.get("Top5Buy")),
        "top5_sell": to_int(row.get("Top5Sell")),
        "top10_buy": to_int(row.get("Top10Buy")),
        "top10_sell": to_int(row.get("Top10Sell")),
        "market_oi": to_int(row.get("OIOfMarket")),
    }


def get_large_traders_for_product(product, rows):
    """從 fetch_large_traders_futures_report() 的原始清單裡篩出某商品（如 "TX"）
    的大額交易人未沖銷部位，依 SettlementMonth 分組，每組收齊所有交易人／特定
    法人兩種 trader_type。回傳依到期月份由近到遠排序的清單、「所有契約合計」
    （999912）固定排在最後：

    [
        {"settlement_month": "202608", "is_all_contracts": False,
         "contract_name": "臺股期貨",
         "by_type": {"所有交易人": {...}, "特定法人": {...}}},
        ...
        {"settlement_month": "999912", "is_all_contracts": True, ...},
    ]

    by_type 的值是 _normalize_large_traders_row 的回傳 dict；缺某一 trader_type
    時該 key 不存在。查無該商品資料回傳空清單。"""
    product_rows = [
        _normalize_large_traders_row(r) for r in rows if r.get("Contract") == product
    ]
    if not product_rows:
        return []

    by_month = {}
    for r in product_rows:
        month = r["settlement_month"]
        label = LARGE_TRADERS_TYPE_LABELS.get(r["trader_type"], r["trader_type"] or "?")
        by_month.setdefault(month, {})[label] = r

    # 真實月份（6 碼西元年月）由近到遠排序；999912（所有契約合計）永遠排最後。
    def sort_key(month):
        return (month == LARGE_TRADERS_ALL_CONTRACTS_MONTH, month)

    result = []
    for month in sorted(by_month, key=sort_key):
        any_row = next(iter(by_month[month].values()))
        result.append(
            {
                "settlement_month": month,
                "is_all_contracts": month == LARGE_TRADERS_ALL_CONTRACTS_MONTH,
                "contract_name": any_row["contract_name"],
                "date": any_row["date"],
                "by_type": by_month[month],
            }
        )
    return result


def group_large_traders_all(rows):
    """把 fetch_large_traders_futures_report() 的整份清單一次分組成
    {Contract: get_large_traders_for_product(該 Contract) 的結果}，讓「期貨」頁
    可以 O(1) 查任一商品的大額分組，不必為每個商品各掃一次全表。key 是大額端點
    自己的契約代碼（如 CD／TX／BRF，不是日盤的 CDF；日盤→大額的換算見
    large_traders_code_for_product）。"""
    by_contract = {}
    for row in rows:
        contract = row.get("Contract")
        if contract:
            by_contract.setdefault(contract, []).append(row)
    return {
        contract: get_large_traders_for_product(contract, contract_rows)
        for contract, contract_rows in by_contract.items()
    }


def build_large_traders_name_map(rows):
    """{Contract: ContractName}（大額端點的商品中文名，例如 BRF→布蘭特原油期貨、
    TX→臺股期貨(TX+MTX/4)、CD→臺積電期貨）。給「期貨」頁非股票期貨的名稱顯示／
    文字搜尋用；key 是大額端點的契約代碼。"""
    return {
        row.get("Contract"): row.get("ContractName")
        for row in rows
        if row.get("Contract") and row.get("ContractName")
    }


def get_cached_large_traders_futures_report(cache_path, force_refresh=False):
    """比照 get_cached_daily_futures_report 的檔案快取模式：命中
    FUTURES_CACHE_TTL_SECONDS 內的快取就不重打 API，過期或 force_refresh 才
    真的呼叫 fetch_large_traders_futures_report()；API 失敗時退回舊快取（若有）
    而不是直接噴錯。"""
    if not force_refresh:
        cached = load_fresh_file_cache(cache_path, FUTURES_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached["rows"]

    try:
        rows = fetch_large_traders_futures_report()
    except PriceFetchError:
        stale = load_stale_file_cache(cache_path)
        if stale is not None:
            return stale["rows"]
        raise

    write_file_cache(cache_path, {"rows": rows})
    return rows


# ---------- 股票期貨交易標的（SSFLists，個股期貨代碼 → 標的股票） ----------

def fetch_ssf_list():
    """打一次 SSFLists，回傳股票期貨交易標的清單。每筆含 Contract（股票期貨契約
    代碼，如 CDF）／UnderlyingStock（標的公司全名）／StockCode（標的股票代號，
    如 2330）／StockName（股票簡稱，如 台積電）／Type。免金鑰、內容變動很少，
    一次回傳全部標的。"""
    return http_get_json(TAIFEX_SSF_LIST_URL)


def get_cached_ssf_list(cache_path, force_refresh=False):
    """SSFLists 的檔案快取，模式同 get_cached_daily_futures_report（命中
    FUTURES_CACHE_TTL_SECONDS 內快取不重打、失敗退回舊快取）。"""
    if not force_refresh:
        cached = load_fresh_file_cache(cache_path, FUTURES_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached["rows"]

    try:
        rows = fetch_ssf_list()
    except PriceFetchError:
        stale = load_stale_file_cache(cache_path)
        if stale is not None:
            return stale["rows"]
        raise

    write_file_cache(cache_path, {"rows": rows})
    return rows


def build_ssf_map(rows):
    """把 fetch_ssf_list() 的清單轉成 {Contract: {"stock_code":, "stock_name":,
    "underlying":}}，供「期貨」頁顯示股票期貨標的名稱、以及用股票代號／名稱搜尋
    比對。"""
    return {
        r["Contract"]: {
            "stock_code": r.get("StockCode"),
            "stock_name": r.get("StockName"),
            "underlying": r.get("UnderlyingStock"),
        }
        for r in rows
        if r.get("Contract")
    }


def large_traders_code_for_product(product, ssf_map):
    """把日盤行情的商品代碼換成「大額交易人未沖銷部位」端點用的代碼。股票期貨
    （在 ssf_map 裡、代碼結尾為 F）在大額端點是去掉結尾 F（實測日盤 CDF＝台積電
    期貨 → 大額端點是 CD，全部 320 檔股票期貨都吻合這個規則）；其餘商品（指數
    期貨 TX、商品期貨 BRF 等，代碼即使結尾有 F 也不屬股票期貨）沿用原代碼。"""
    if product in ssf_map and product.endswith("F"):
        return product[:-1]
    return product


# ---------- 大額交易人未沖銷部位「歷史」回補（TAIFEX 網站 CSV，綁定回補天數） ----------

def fetch_large_traders_history_range(start_date, end_date):
    """打一次 TAIFEX 歷史 CSV 下載端點，回傳 [start_date, end_date]（含端點，datetime.date）
    區間內全市場大額交易人未沖銷部位，正規化成可直接餵給
    tradingnote_history.upsert_large_traders_history 的 tuple 清單：
    (date_iso, contract, settlement_month, trader_type, contract_name,
     top5_buy, top5_sell, top10_buy, top10_sell, market_oi)。

    注意單次查詢跨度不能超過 LARGE_TRADERS_HISTORY_MAX_SPAN_DAYS（見常數說明），
    超過會被端點擋回 HTML 錯誤頁、解析不到任何 CSV 資料列——分段由
    backfill_large_traders_history 負責。CSV 是 Big5 編碼，日期欄 "YYYY/MM/DD"
    轉成 daily_prices 慣用的 ISO "YYYY-MM-DD"。"""
    body = urlencode(
        {
            "queryStartDate": start_date.strftime("%Y/%m/%d"),
            "queryEndDate": end_date.strftime("%Y/%m/%d"),
        }
    )
    text = http_post_text(TAIFEX_LARGE_TRADERS_HISTORY_URL, body, encoding="big5")

    rows = []
    for record in csv.reader(io.StringIO(text)):
        # 只收真正的資料列：至少 10 欄、且第一欄是 "YYYY/MM/DD" 日期（跳過標題列、
        # 跨度超限時回的 HTML 錯誤頁、以及任何非資料雜訊列）。
        if len(record) < 10 or "/" not in record[0]:
            continue
        rows.append(
            (
                record[0].strip().replace("/", "-"),
                record[1].strip(),
                record[3].strip(),
                record[4].strip(),
                record[2].strip(),
                to_int(record[5]),
                to_int(record[6]),
                to_int(record[7]),
                to_int(record[8]),
                to_int(record[9]),
            )
        )
    return rows


def _date_chunks(start_date, end_date, max_span_days):
    """把 [start_date, end_date] 切成每段最多 max_span_days 曆日的 (chunk_start,
    chunk_end) 清單（含端點）。start_date > end_date 時回傳空清單。"""
    chunks = []
    cursor = start_date
    while cursor <= end_date:
        chunk_end = min(cursor + timedelta(days=max_span_days - 1), end_date)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def backfill_large_traders_history(
    db_path, target_days, delay_seconds=0.3, on_progress=None
):
    """把最近 target_days 曆日的大額交易人未沖銷部位歷史補進 large_traders_history。
    跟股價歷史回補同一套「可安全中斷後重跑」的冪等原則：只抓目前資料庫還缺的日期
    段落，已存在的日期用 INSERT OR REPLACE 覆蓋不會重複。

    缺口判斷：以已存的最新日期為準往「今天」補（forward gap），若 target_days 變大、
    使資料庫最早日期還晚於期望起點，另外補「往前延伸」那段（backward gap）。每段再
    依 LARGE_TRADERS_HISTORY_MAX_SPAN_DAYS 切塊分次下載（單次查詢跨度有上限）。

    `on_progress(done_chunks, total_chunks)` 每下載完一塊呼叫一次，給狀態列顯示進度。
    回傳 {"chunks": 下載的塊數, "rows": 寫入的資料列總數}；已是最新、無缺口時
    chunks=0。"""
    today = date.today()
    desired_start = today - timedelta(days=target_days)
    min_stored, max_stored = get_large_traders_history_date_range(db_path)

    ranges = []
    if max_stored is None:
        ranges.append((desired_start, today))
    else:
        max_d = date.fromisoformat(max_stored)
        min_d = date.fromisoformat(min_stored)
        # 往最新補：只抓已存最新日「之後」到今天（不重抓 max_d，EOD 資料存下時已完整）。
        # 沒有新交易日時這段只含假日、CSV 回空、不寫入也不會壞——資料已是最新時
        # forward_start 會大於 today，整段跳過、不發請求。
        forward_start = max_d + timedelta(days=1)
        if forward_start <= today:
            ranges.append((forward_start, today))
        # 往前延伸：只有在缺的舊資料「明顯」超過期望起點（>7 曆日）才補，避免每次
        # 因為 desired_start 落在非交易日、跟最早交易日差一兩天就無謂重抓一段。
        if min_d > desired_start + timedelta(days=7):
            ranges.append((desired_start, min_d))

    chunks = []
    for range_start, range_end in ranges:
        chunks.extend(
            _date_chunks(range_start, range_end, LARGE_TRADERS_HISTORY_MAX_SPAN_DAYS)
        )

    total_rows = 0
    for index, (chunk_start, chunk_end) in enumerate(chunks):
        rows = fetch_large_traders_history_range(chunk_start, chunk_end)
        upsert_large_traders_history(db_path, rows)
        total_rows += len(rows)
        if delay_seconds:
            time.sleep(delay_seconds)
        if on_progress:
            on_progress(index + 1, len(chunks))

    return {"chunks": len(chunks), "rows": total_rows}
