#!/usr/bin/env python3
"""tradingnote 歷史模組 - 120日歷史價格、產業分類、產業資金流向分析（不依賴任何介面）"""

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from tradingnote_http import PriceFetchError, http_get_json, to_float, to_int

TWSE_MI_INDEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TWSE_INDUSTRY_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_INDUSTRY_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

INDUSTRY_MAP_MAX_AGE_DAYS = 7
DEFAULT_BACKFILL_TARGET_DAYS = 120
DEFAULT_BACKFILL_LOOKBACK_CAP_DAYS = 200
DEFAULT_BACKFILL_DELAY_SECONDS = 0.3

# 同一個行程（process）內最多只讓一個 backfill_twse_history 在跑：GUI 啟動時的自動
# 資料連續性同步，跟使用者手動點「回補歷史資料」有可能前後腳同時觸發，兩個都寫同一個
# SQLite 檔會造成鎖爭用、拖慢甚至互相卡住。用這個 lock 讓第二個呼叫排隊等第一個做完，
# 而不是真的同時打 API／寫入。
_backfill_lock = threading.Lock()

_SIGN_RE = re.compile(r">([+-])<")

# 公開資訊觀測站產業別代碼（TWSE／TPEX 共用），驗證方式見 ARCHITECTURE.md。
# 少數罕見代碼（如 37、38）未能確認對應名稱，industry_name() 會 fallback 顯示原始代碼。
INDUSTRY_CODE_NAMES = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "14": "建材營造",
    "15": "航運業", "16": "觀光事業", "17": "金融保險", "18": "貿易百貨",
    "19": "綜合", "20": "其他", "21": "化學工業", "22": "生技醫療業",
    "23": "油電燃氣業", "24": "半導體業", "25": "電腦及週邊設備業", "26": "光電業",
    "27": "通信網路業", "28": "電子零組件業", "29": "電子通路業", "30": "資訊服務業",
    "31": "其他電子業", "32": "文化創意業", "33": "農業科技業", "34": "電子商務業",
    "35": "綠能環保", "36": "數位雲端", "80": "全額交割股", "91": "臺灣存託憑證",
    "97": "社會企業", "98": "農林漁牧業",
}


@dataclass
class IndustryFlow:
    industry: str
    avg_change_pct: float | None
    volume_ratio: float | None
    total_trading_value: float
    capital_share_pct: float
    stock_count: int


def industry_name(code):
    code = (code or "").strip()
    return INDUSTRY_CODE_NAMES.get(code, code or "未分類")


def _change_pct(price):
    if price.close is None or price.change is None:
        return None
    prev_close = price.close - price.change
    if not prev_close:
        return None
    return price.change / prev_close * 100


# ---------- DB ----------

def _connect(db_path):
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # timeout=30：遇到鎖定時最多等 30 秒才放棄（sqlite3 預設只有 5 秒），搭配下面的
    # WAL 模式，讓 CLI／GUI 同時開啟，或 GUI 內背景執行緒（啟動自動同步）跟使用者手動
    # 點「回補歷史資料」湊巧同時寫入時，不會直接丟出 database is locked。
    conn = sqlite3.connect(p, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daily_prices (
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            market TEXT,
            name TEXT,
            close REAL,
            change_pct REAL,
            volume INTEGER,
            trading_value REAL,
            PRIMARY KEY (date, ticker)
        )"""
    )
    # PK 索引是 (date, ticker)，對「依日期範圍查詢」（compute_industry_flow）有效，但
    # 對 LIFO 的主要存取模式「WHERE ticker = ? ORDER BY date DESC」（get_latest_ticker_record／
    # get_ticker_history）完全用不上，會退化成全表掃描。這裡另建一個 (ticker, date DESC)
    # 索引，讓個股層級的 LIFO 查詢也能吃到索引。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_prices_ticker_date "
        "ON daily_prices (ticker, date DESC)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS industry_map (
            ticker TEXT PRIMARY KEY,
            name TEXT,
            industry TEXT,
            market TEXT,
            updated_at TEXT
        )"""
    )
    conn.commit()
    return conn


# ---------- 每日快照累積（TPEX 逐日累積的基礎來源，另見下方 upsert_daily_prices／
# tradingnote_finmind.backfill_tpex_history_via_finmind 的 FinMind 補缺口機制） ----------

def record_snapshot(db_path, snapshot, as_of_date=None):
    """`as_of_date` 給定時強制套用到每一筆（呼叫端明確指定的情況）；未給定時改採
    每檔股票自己的 `price.date`（即 API 回傳的實際交易日期，例如 TWSE 的
    STOCK_DAY_ALL 尚未更新到今天時，回傳的仍是上一個交易日的資料，此時
    `price.date` 會忠實反映那個「上一個交易日」而不是打 API 當下的日曆日期）。
    只有 `price.date` 缺漏（理論上不會發生，防禦性 fallback）才退回今天的日期。
    這樣同一次 snapshot 裡 TWSE／TPEX 更新進度不同步時，各自會存到正確的日期，
    不會把某個市場還沒更新的舊資料誤標成今天。"""
    today_iso = date.today().isoformat()
    conn = _connect(db_path)
    try:
        conn.executemany(
            """INSERT OR REPLACE INTO daily_prices
               (date, ticker, market, name, close, change_pct, volume, trading_value)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    as_of_date or price.date or today_iso,
                    price.ticker,
                    price.market,
                    price.name,
                    price.close,
                    _change_pct(price),
                    price.volume,
                    price.trading_value,
                )
                for price in snapshot.values()
            ],
        )
        conn.commit()
    finally:
        conn.close()


def upsert_daily_prices(db_path, rows):
    """寫入一批 (date, ticker, market, name, close, change_pct, volume, trading_value)
    tuple 到 daily_prices（INSERT OR REPLACE，跟 record_snapshot 寫入邏輯一致），
    供不同來源共用同一個寫入路徑而不用各自重寫一次 SQL／欄位順序——目前給
    tradingnote_finmind.backfill_tpex_history_via_finmind() 寫入 FinMind 補齊的
    上櫃歷史資料用。rows 為空時直接 return，不開連線。"""
    if not rows:
        return
    conn = _connect(db_path)
    try:
        conn.executemany(
            """INSERT OR REPLACE INTO daily_prices
               (date, ticker, market, name, close, change_pct, volume, trading_value)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def get_history_status(db_path):
    """回傳「設定」分頁「資料庫狀態」用的彙總統計：整體與 TWSE／TPEX 各自的交易日數、
    日期範圍、股票數、筆數，外加資料庫檔案大小。只做 GROUP BY 聚合查詢，不把 200 天
    ×全市場的原始列（數十萬筆）拉進 Python／UI，維持跟「個股」分頁同一套「輕量摘要，
    非逐列瀏覽」原則。"""
    conn = _connect(db_path)
    try:
        overall_row = conn.execute(
            "SELECT COUNT(DISTINCT date), MIN(date), MAX(date), "
            "COUNT(DISTINCT ticker), COUNT(*) FROM daily_prices"
        ).fetchone()
        by_market = conn.execute(
            "SELECT market, COUNT(DISTINCT date), MIN(date), MAX(date), "
            "COUNT(DISTINCT ticker), COUNT(*) FROM daily_prices GROUP BY market"
        ).fetchall()
    finally:
        conn.close()

    def _to_dict(row):
        days, min_date, max_date, tickers, rows = row
        return {
            "days": days,
            "min_date": min_date,
            "max_date": max_date,
            "tickers": tickers,
            "rows": rows,
        }

    db_file = Path(db_path)
    file_size_bytes = db_file.stat().st_size if db_file.exists() else 0

    return {
        "overall": _to_dict(overall_row),
        "by_market": {
            market: _to_dict((days, min_date, max_date, tickers, rows))
            for market, days, min_date, max_date, tickers, rows in by_market
        },
        "file_size_bytes": file_size_bytes,
    }


def prune_history(db_path, keep_trading_days=DEFAULT_BACKFILL_TARGET_DAYS):
    conn = _connect(db_path)
    try:
        dates = [
            row[0]
            for row in conn.execute("SELECT DISTINCT date FROM daily_prices ORDER BY date DESC")
        ]
        stale = dates[keep_trading_days:]
        if stale:
            conn.executemany("DELETE FROM daily_prices WHERE date = ?", [(d,) for d in stale])
            conn.commit()
    finally:
        conn.close()


# ---------- TWSE 歷史回補 ----------
# TPEX 沒有官方的免費歷史回補端點（已測試 stk_quote_result.php 的 d 參數會被忽略，
# 永遠只回傳今天的資料），這裡只處理 TWSE；TPEX 改用 FinMind TaiwanStockPrice
# 資料集回補（有免費額度限制，見 tradingnote_finmind.backfill_tpex_history_via_finmind），
# 沒有 FinMind token 或額度用完時，仍舊只能靠 record_snapshot 逐日累積。

def fetch_twse_historical_day(date_str):
    """date_str 格式 YYYYMMDD。回傳當天全部上市股票紀錄；非交易日回傳 None。"""
    url = f"{TWSE_MI_INDEX_URL}?date={date_str}&type=ALLBUT0999&response=json"
    try:
        data = http_get_json(url)
    except PriceFetchError:
        return None
    if data.get("stat") != "OK":
        return None

    tables = data.get("tables") or []
    quotes_table = next(
        (t for t in tables if (t.get("fields") or [None])[0] == "證券代號"), None
    )
    if quotes_table is None:
        return None

    records = []
    for row in quotes_table.get("data", []):
        try:
            code, name = row[0].strip(), row[1].strip()
            volume = to_int(row[2])
            trading_value = to_float(row[4])
            open_ = to_float(row[5])
            high = to_float(row[6])
            low = to_float(row[7])
            close = to_float(row[8])
            sign_match = _SIGN_RE.search(row[9] or "")
            sign = -1.0 if sign_match and sign_match.group(1) == "-" else 1.0
            magnitude = to_float(row[10]) or 0.0
        except (IndexError, AttributeError):
            continue
        if not code or close is None:
            continue
        change = sign * magnitude
        prev_close = close - change
        change_pct = (change / prev_close * 100) if prev_close else None
        records.append(
            {
                "ticker": code,
                "name": name,
                "market": "TWSE",
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "change": change,
                "change_pct": change_pct,
                "volume": volume,
                "trading_value": trading_value,
            }
        )
    return records


def backfill_twse_history(
    db_path,
    target_days=DEFAULT_BACKFILL_TARGET_DAYS,
    lookback_cap_days=None,
    delay_seconds=DEFAULT_BACKFILL_DELAY_SECONDS,
    on_progress=None,
):
    """從昨天往回走，補到有 target_days 個 TWSE 交易日為止。已存在的日期會跳過重打，
    可安全中斷後重跑。`on_progress` 只在真的打了 API 補到新的一天時才會被呼叫——已經
    存在的日期會靜默跳過，讓「每次啟動都呼叫一次」在資料已經連續的情況下幾乎沒有感知
    （不會印出/顯示任何進度），只有真的補缺口時才會動。同一行程內若已經有一個 backfill
    在跑，這個呼叫會先排隊等它做完（見 `_backfill_lock`），而不是同時打 API／寫入。

    `lookback_cap_days` 省略時會依 target_days 自動放大（台股一年約 247 個交易日，
    日曆天數約是交易日的 1.6 倍，含假日緩衝），讓使用者調高 target_days（例如設定頁
    的「回補天數」）時，掃描上限不會沒跟著放大而提早停在還沒補滿 target_days 的地方；
    下限固定沿用原本 DEFAULT_BACKFILL_LOOKBACK_CAP_DAYS，維持預設值（120 天）行為不變。"""
    if lookback_cap_days is None:
        lookback_cap_days = max(
            DEFAULT_BACKFILL_LOOKBACK_CAP_DAYS, int(target_days * 1.6)
        )
    with _backfill_lock:
        return _backfill_twse_history_locked(
            db_path, target_days, lookback_cap_days, delay_seconds, on_progress
        )


def _backfill_twse_history_locked(
    db_path, target_days, lookback_cap_days, delay_seconds, on_progress
):
    conn = _connect(db_path)
    try:
        existing_dates = {
            row[0]
            for row in conn.execute("SELECT DISTINCT date FROM daily_prices WHERE market = 'TWSE'")
        }

        done = 0
        cursor_date = date.today() - timedelta(days=1)
        scanned = 0
        while done < target_days and scanned < lookback_cap_days:
            iso_date = cursor_date.isoformat()
            date_str = cursor_date.strftime("%Y%m%d")
            scanned += 1

            if iso_date in existing_dates:
                done += 1
                cursor_date -= timedelta(days=1)
                continue

            records = fetch_twse_historical_day(date_str)
            time.sleep(delay_seconds)
            if records:
                conn.executemany(
                    """INSERT OR REPLACE INTO daily_prices
                       (date, ticker, market, name, close, change_pct, volume, trading_value)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            iso_date,
                            r["ticker"],
                            r["market"],
                            r["name"],
                            r["close"],
                            r["change_pct"],
                            r["volume"],
                            r["trading_value"],
                        )
                        for r in records
                    ],
                )
                conn.commit()
                existing_dates.add(iso_date)
                done += 1
                if on_progress:
                    on_progress(done, target_days)

            cursor_date -= timedelta(days=1)

        return done
    finally:
        conn.close()


# ---------- 個股資料查詢（LIFO：一律以 date DESC 為主要存取順序） ----------
# 本專案存取單一股票歷史資料的標準方式是 LIFO（最新日期優先）：即時快照若缺漏某檔股票
# （例如 API 當下沒回傳、或使用者查詢時剛好卡在重新整理中間），一律先往歷史資料庫要
# 「最新一筆」而不是任意一筆，確保資料連續性延伸到單檔股票查詢的層面。

def get_latest_ticker_record(db_path, ticker):
    """LIFO 查詢：回傳某檔股票在歷史資料庫中最新一筆紀錄，查無資料回傳 None。"""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """SELECT date, market, name, close, change_pct, volume, trading_value
               FROM daily_prices WHERE ticker = ? ORDER BY date DESC LIMIT 1""",
            (ticker,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    d, market, name, close, change_pct, volume, trading_value = row
    return {
        "date": d,
        "market": market,
        "name": name,
        "close": close,
        "change_pct": change_pct,
        "volume": volume,
        "trading_value": trading_value,
    }


def get_ticker_history(db_path, ticker, limit=None):
    """LIFO 查詢：回傳某檔股票的歷史紀錄，固定以 date DESC（最新優先）排序。"""
    conn = _connect(db_path)
    try:
        sql = (
            "SELECT date, close, change_pct, volume, trading_value "
            "FROM daily_prices WHERE ticker = ? ORDER BY date DESC"
        )
        params = [ticker]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return rows


def get_ticker_history_day_counts(db_path, tickers, market=None):
    """回傳 {ticker: 交易日數}（distinct date 計數），一次查詢取代逐檔各查一次，
    給批次判斷「哪些股票歷史資料已經足夠、可以跳過」用（見
    tradingnote_finmind.backfill_tpex_history_via_finmind：判斷某檔上櫃股票是否
    已達 target_days 天，達到就跳過不重打 FinMind）。market 給定時只計入該市場
    的紀錄；tickers 為空回傳空字典，不開連線。"""
    if not tickers:
        return {}
    conn = _connect(db_path)
    try:
        placeholders = ",".join("?" * len(tickers))
        sql = (
            f"SELECT ticker, COUNT(DISTINCT date) FROM daily_prices "
            f"WHERE ticker IN ({placeholders})"
        )
        params = list(tickers)
        if market:
            sql += " AND market = ?"
            params.append(market)
        sql += " GROUP BY ticker"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return dict(rows)


# ---------- 日期選擇（給資金流向頁的日曆式區間選擇器用，取代原本直接輸入天數；
# 天數（avg_days）語意見 compute_industry_flow：今天以前 N 個交易日，不含今天） ----------

def get_available_dates(db_path):
    """回傳 daily_prices 裡所有出現過的交易日（ISO 字串，由舊到新排序），給 GUI
    日期選擇器判斷「哪些日期有資料、該反白哪些日期」用；資料庫是空的回傳空清單。"""
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT date FROM daily_prices ORDER BY date").fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def trading_days_between(db_path, start_date, end_date=None):
    """回傳 start_date（含）到 end_date（不含，預設今天）之間有資料的交易日數，
    給日期選擇器把使用者選的「起始日期」換算成 compute_industry_flow／
    compute_volume_ratio_outliers 慣用的 avg_days（今天以前 N 個交易日）。"""
    end_date = end_date or date.today().isoformat()
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM daily_prices WHERE date >= ? AND date < ?",
            (start_date, end_date),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def default_start_date_for_days(db_path, avg_days):
    """回傳「今天以前 avg_days 個交易日」對應的起始日期（ISO字串），給日期選擇器的
    預設值用（例如舊有 QSpinBox 預設值 5／20 天，換算成日期選擇器的預設起始日）；
    實際可用天數不足 avg_days 天時，回傳資料庫裡最早的日期（等同「全部資料都選」），
    完全沒有資料時回傳今天。跟 trading_days_between 互為反函式：
    trading_days_between(db_path, default_start_date_for_days(db_path, N)) == N
    （資料足夠的情況下）。"""
    today_iso = date.today().isoformat()
    before_today = [d for d in get_available_dates(db_path) if d < today_iso]
    if not before_today:
        return today_iso
    index = max(0, len(before_today) - avg_days)
    return before_today[index]


# ---------- 產業分類 ----------

def fetch_industry_map():
    twse_rows = http_get_json(TWSE_INDUSTRY_URL)
    tpex_rows = http_get_json(TPEX_INDUSTRY_URL)

    mapping = {}
    for rec in twse_rows:
        code = (rec.get("公司代號") or "").strip()
        if not code:
            continue
        mapping[code] = {
            "name": (rec.get("公司簡稱") or "").strip(),
            "industry": industry_name(rec.get("產業別")),
            "market": "TWSE",
        }
    for rec in tpex_rows:
        code = (rec.get("SecuritiesCompanyCode") or "").strip()
        if not code:
            continue
        mapping[code] = {
            "name": (rec.get("CompanyAbbreviation") or "").strip(),
            "industry": industry_name(rec.get("SecuritiesIndustryCode")),
            "market": "TPEX",
        }
    return mapping


def refresh_industry_map(db_path):
    mapping = fetch_industry_map()
    now = datetime.now().isoformat()
    conn = _connect(db_path)
    try:
        conn.executemany(
            """INSERT OR REPLACE INTO industry_map (ticker, name, industry, market, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (ticker, info["name"], info["industry"], info["market"], now)
                for ticker, info in mapping.items()
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return mapping


def _load_industry_rows(db_path, max_age_days=INDUSTRY_MAP_MAX_AGE_DAYS):
    """回傳 industry_map 全部欄位（ticker, name, industry, market, updated_at），
    共用同一套 7 日 TTL 快取／過期重抓邏輯，讓 get_industry_map 與
    get_industry_directory 不用各自重寫一次。"""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ticker, name, industry, market, updated_at FROM industry_map"
        ).fetchall()
    finally:
        conn.close()

    if rows:
        oldest = min(row[4] for row in rows)
        if (datetime.now() - datetime.fromisoformat(oldest)).days < max_age_days:
            return rows

    try:
        mapping = refresh_industry_map(db_path)
    except PriceFetchError:
        if rows:
            return rows
        raise
    now = datetime.now().isoformat()
    return [
        (ticker, info["name"], info["industry"], info["market"], now)
        for ticker, info in mapping.items()
    ]


def get_industry_map(db_path, max_age_days=INDUSTRY_MAP_MAX_AGE_DAYS):
    rows = _load_industry_rows(db_path, max_age_days)
    return {ticker: industry for ticker, _name, industry, _market, _updated in rows}


def get_industry_directory(db_path, max_age_days=INDUSTRY_MAP_MAX_AGE_DAYS):
    """回傳 {ticker: {"name":, "industry":, "market":}}，供「個股」模組依族群列出
    全市場股票用（比 get_industry_map 多帶 name／market，不只有 industry）。"""
    rows = _load_industry_rows(db_path, max_age_days)
    return {
        ticker: {"name": name, "industry": industry, "market": market}
        for ticker, name, industry, market, _updated in rows
    }


# ---------- 產業資金流向分析 ----------

def compute_industry_flow(db_path, snapshot, avg_days=5):
    """avg_days 同時決定兩件事，讓 X／Y 兩軸的「N 日流向」定義一致：
    ① avg_change_pct：該產業近 avg_days 個交易日的累積漲跌%（以今日收盤對比 avg_days
       個交易日前收盤，用今日成交金額加權平均），取代單日漲跌%；
    ② volume_ratio：今日成交量對近 avg_days 日均量的比值（沿用原本邏輯）。
    兩者用同一組「有足夠歷史資料」的成分股計算，資料不足的產業兩者皆回傳 None（由呼叫端
    過濾不畫出），不會出現 X 有值但 Y 沒有值的不一致情況。"""
    industry_map = get_industry_map(db_path)

    groups = {}
    for price in snapshot.values():
        if price.close is None or price.trading_value is None:
            continue
        industry = industry_map.get(price.ticker)
        if not industry:
            continue
        g = groups.setdefault(
            industry,
            {"trading_value": 0.0, "ticker_prices": {}, "ticker_volumes": {}, "ticker_values": {}},
        )
        g["trading_value"] += price.trading_value
        g["ticker_prices"][price.ticker] = price.close
        g["ticker_volumes"][price.ticker] = price.volume or 0
        g["ticker_values"][price.ticker] = price.trading_value

    min_history_days = max(2, avg_days // 2)
    cutoff = (date.today() - timedelta(days=avg_days * 3)).isoformat()
    conn = _connect(db_path)
    try:
        # LIFO：以 date DESC 為主要存取順序，讓每檔股票的區間內紀錄天然由新到舊排列，
        # 下面直接取前 avg_days 筆即為「近 avg_days 日」，不需要再用 Python 額外排序；
        # 該切片的最後一筆（最舊）即為「avg_days 個交易日前」的收盤價基準。
        history_rows = conn.execute(
            """SELECT ticker, date, close, volume FROM daily_prices
               WHERE date < ? AND date >= ? ORDER BY date DESC""",
            (date.today().isoformat(), cutoff),
        ).fetchall()
    finally:
        conn.close()

    ticker_history = {}
    for ticker, day, close, volume in history_rows:
        ticker_history.setdefault(ticker, []).append((day, close, volume or 0))

    # 資金比重的分母用「全市場今日總成交金額」（含歷史資料不足、稍後會被過濾掉的產業），
    # 才能反映真實佔比；若只用 plotted 產業算分母，佔比會隨著哪些產業被過濾而跳動。
    total_market_value = sum(g["trading_value"] for g in groups.values())

    results = []
    for industry, g in groups.items():
        if g["trading_value"] <= 0:
            continue

        weighted_change, change_weight = 0.0, 0.0
        today_volume, baseline_volume = 0.0, 0.0
        for ticker, today_close in g["ticker_prices"].items():
            days = ticker_history.get(ticker, [])[:avg_days]
            if len(days) < min_history_days:
                continue
            baseline_close = days[-1][1]
            if not baseline_close:
                continue
            n_day_change_pct = (today_close - baseline_close) / baseline_close * 100
            trading_value = g["ticker_values"][ticker]
            weighted_change += n_day_change_pct * trading_value
            change_weight += trading_value

            baseline_volume += sum(v for _, _, v in days) / len(days)
            today_volume += g["ticker_volumes"][ticker]

        avg_change_pct = (weighted_change / change_weight) if change_weight > 0 else None
        volume_ratio = (today_volume / baseline_volume) if baseline_volume > 0 else None

        capital_share_pct = (
            (g["trading_value"] / total_market_value * 100) if total_market_value > 0 else 0.0
        )

        results.append(
            IndustryFlow(
                industry=industry,
                avg_change_pct=avg_change_pct,
                volume_ratio=volume_ratio,
                total_trading_value=g["trading_value"],
                capital_share_pct=capital_share_pct,
                stock_count=len(g["ticker_prices"]),
            )
        )

    results.sort(key=lambda r: r.total_trading_value, reverse=True)
    return results


# ---------- 產業成分股（依累積成交金額排序，作為市值代理指標） ----------
# 本專案沒有股本／發行股數資料（PriceInfo、daily_prices 都只有收盤價、成交量、成交
# 金額），無法計算真正市值，因此「前N大成分股」改以區間累積成交金額排序，資料來源是
# 即時 snapshot（今日）＋ daily_prices（歷史），不額外打 API，跟「個股」分頁列清單的
# 原則一致。

def get_industry_top_stocks_range(db_path, snapshot, industry, days, top_n=30):
    """回傳某產業前 top_n 大成分股，排序依據是近 days 個交易日（含今日）的
    「累積成交金額」，讓成分股排名跟「資金流向分析」頁（泡泡圖／資金動向清單）的
    資料區間口徑一致。今日成交金額／收盤來自即時 snapshot，
    days-1 天以前的歷史資料來自 daily_prices，抓法跟 compute_industry_flow 相同
    （today 不在 daily_prices 裡，用 date < today 排除，避免重複計入）。change_pct
    是區間累積漲跌%（區間起點收盤到今日收盤），跟清單「加權漲跌%」欄位定義一致。
    歷史筆數不足 days 天的 ticker 仍會列入、用實際可拿到的筆數計算，不像
    compute_industry_flow 會整檔排除——這裡只是排序用途，不要求嚴謹的樣本數。"""
    industry_map = get_industry_map(db_path)

    today_by_ticker = {}
    for price in snapshot.values():
        if price.close is None or price.trading_value is None:
            continue
        if industry_map.get(price.ticker) != industry:
            continue
        today_by_ticker[price.ticker] = price

    if not today_by_ticker:
        return []

    tickers = list(today_by_ticker)
    cutoff = (date.today() - timedelta(days=max(days, 1) * 3)).isoformat()
    conn = _connect(db_path)
    try:
        placeholders = ",".join("?" * len(tickers))
        rows = conn.execute(
            f"""SELECT ticker, date, close, trading_value FROM daily_prices
               WHERE ticker IN ({placeholders}) AND date < ? AND date >= ?
               ORDER BY date DESC""",
            (*tickers, date.today().isoformat(), cutoff),
        ).fetchall()
    finally:
        conn.close()

    ticker_history = {}
    for ticker, day, close, trading_value in rows:
        ticker_history.setdefault(ticker, []).append((day, close, trading_value or 0.0))

    results = []
    for ticker, price in today_by_ticker.items():
        history = ticker_history.get(ticker, [])[: max(days - 1, 0)]
        total_trading_value = price.trading_value + sum(v for _, _, v in history)
        baseline_close = history[-1][1] if history else price.close
        change_pct = (
            (price.close - baseline_close) / baseline_close * 100
            if baseline_close
            else None
        )
        results.append(
            {
                "ticker": ticker,
                "name": price.name,
                "close": price.close,
                "change_pct": change_pct,
                "trading_value": total_trading_value,
            }
        )

    results.sort(key=lambda r: r["trading_value"], reverse=True)
    return results[:top_n]


# ---------- 個股量比異常清單 ----------

VOLUME_RATIO_TIERS = ("1.5～2倍", "2～3倍", "3倍以上")


@dataclass
class VolumeRatioOutlier:
    ticker: str
    name: str
    market: str
    close: float | None
    change_pct: float | None
    today_volume: int
    avg_volume: float
    volume_ratio: float
    tier: str


def _volume_ratio_tier(ratio):
    if ratio >= 3:
        return "3倍以上"
    if ratio >= 2:
        return "2～3倍"
    return "1.5～2倍"


def compute_volume_ratio_outliers(db_path, snapshot, avg_days=5, min_ratio=1.5):
    """掃描全市場個股，找出「今日成交量 ÷ 近 avg_days 日均量」達到 min_ratio 倍以上的
    爆量股票，依比值分成三個級距（1.5～2倍／2～3倍／3倍以上）。

    跟 compute_industry_flow 的 volume_ratio 不同：那裡是整個產業加總後的量比，
    單一檔股票爆量會被同產業其他股票的量能稀釋掉；這裡逐檔股票各自比較自己的
    歷史均量，才抓得到「個股」層級的異常。查詢手法（LIFO 取近 avg_days 筆歷史）
    跟 compute_industry_flow／get_industry_top_stocks_range 一致。只納入
    industry_map 裡有分類的股票，藉此排除權證、ETF 等非個股商品。"""
    industry_map = get_industry_map(db_path)
    min_history_days = max(2, avg_days // 2)
    cutoff = (date.today() - timedelta(days=avg_days * 3)).isoformat()

    tickers_today = {}
    for price in snapshot.values():
        if price.close is None or price.volume is None:
            continue
        if price.ticker not in industry_map:
            continue
        tickers_today[price.ticker] = price

    if not tickers_today:
        return []

    tickers = list(tickers_today)
    conn = _connect(db_path)
    try:
        placeholders = ",".join("?" * len(tickers))
        rows = conn.execute(
            f"""SELECT ticker, date, volume FROM daily_prices
               WHERE ticker IN ({placeholders}) AND date < ? AND date >= ?
               ORDER BY date DESC""",
            (*tickers, date.today().isoformat(), cutoff),
        ).fetchall()
    finally:
        conn.close()

    ticker_history = {}
    for ticker, _day, volume in rows:
        ticker_history.setdefault(ticker, []).append(volume or 0)

    results = []
    for ticker, price in tickers_today.items():
        days = ticker_history.get(ticker, [])[:avg_days]
        if len(days) < min_history_days:
            continue
        avg_volume = sum(days) / len(days)
        if avg_volume <= 0:
            continue
        ratio = price.volume / avg_volume
        if ratio < min_ratio:
            continue

        results.append(
            VolumeRatioOutlier(
                ticker=ticker,
                name=price.name,
                market=price.market,
                close=price.close,
                change_pct=_change_pct(price),
                today_volume=price.volume,
                avg_volume=avg_volume,
                volume_ratio=ratio,
                tier=_volume_ratio_tier(ratio),
            )
        )

    results.sort(key=lambda r: r.volume_ratio, reverse=True)
    return results
