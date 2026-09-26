#!/usr/bin/env python3
"""tradingnote 歷史模組 - 120日歷史價格、產業分類、產業資金流向分析（不依賴任何介面）"""

import re
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from tradingnote_api_config import (
    TPEX_DAILY_QUOTES_URL, TPEX_INDUSTRY_URL, TWSE_INDUSTRY_URL, TWSE_MI_INDEX_URL,
)
from tradingnote_http import PriceFetchError, http_get_json, to_float, to_int

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
# 37／38 依櫃買中心「證券產業別代碼表」補為運動休閒／居家生活。
INDUSTRY_CODE_NAMES = {
    "01": "水泥工業", "02": "食品工業", "03": "塑膠工業", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "08": "玻璃陶瓷", "09": "造紙工業",
    "10": "鋼鐵工業", "11": "橡膠工業", "12": "汽車工業", "14": "建材營造",
    "15": "航運業", "16": "觀光事業", "17": "金融保險", "18": "貿易百貨",
    "19": "綜合", "20": "其他", "21": "化學工業", "22": "生技醫療業",
    "23": "油電燃氣業", "24": "半導體業", "25": "電腦及週邊設備業", "26": "光電業",
    "27": "通信網路業", "28": "電子零組件業", "29": "電子通路業", "30": "資訊服務業",
    "31": "其他電子業", "32": "文化創意業", "33": "農業科技業", "34": "電子商務業",
    "35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活",
    "80": "全額交割股", "91": "臺灣存託憑證",
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
    daily_change_pct: float | None = None
    turnover_ratio: float | None = None
    directional_flow_value: float | None = None
    popularity_score: float | None = None
    momentum_score: float | None = None
    volume_score: float | None = None
    composite_score: float | None = None
    actual_days: int = 0
    required_days: int = 0
    actual_volume_days: int = 0


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


def _snapshot_today(snapshot):
    """回傳 snapshot 裡最多股票共用的交易日（多數 PriceInfo.date 會是同一天，用眾數
    避免少數個股資料延遲／異常日期影響判斷；同 tradingnote_gui._snapshot_date，但
    這裡是核心模組不能 import GUI，故獨立宣告一份）。

    下面幾個「近 N 日流向」計算函式都要用這個當歷史資料查詢的分界，不能用
    date.today()——snapshot 是 TWSE/TPEX 目前實際發布的最新收盤，不一定等於今天的
    日曆日期（一早、假日、或對方資料延遲發布時，snapshot 可能還停在前一個交易日）。
    誤用 date.today() 當分界，遇到 snapshot 落後、且 avg_days 很小（例如「近2日」）
    時，算出來的「N 個交易日前」基準日會剛好撞到 snapshot 本身那天，變成同一天的
    收盤價互相比較，結果近似 0%，看起來就像「完全沒有漲跌」——這是泡泡圖選「近2日」
    偶爾看起來沒有股價漲幅的實際成因。snapshot 是空的就回退用 date.today()。"""
    dates = [p.date for p in snapshot.values() if p.date]
    if not dates:
        return date.today().isoformat()
    return Counter(dates).most_common(1)[0][0]


def _cross_sectional_percentile(value, values):
    """把同一批產業中的原始值轉成 0～100 百分位分數。

    使用平均名次處理同值，避免 min-max 被單一極端值拉扯；分數只表示當日／
    當次查詢的相對位置，不宣稱跨日期具有固定絕對意義。只有一個有效值時回傳
    中間分數 50，避免把孤立資料誤標成滿分。
    """
    if value is None or not values:
        return None
    if len(values) == 1:
        return 50.0
    lower_count = sum(other < value for other in values)
    equal_count = sum(other == value for other in values)
    average_rank = lower_count + (equal_count + 1) / 2
    return (average_rank - 1) / (len(values) - 1) * 100


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
    # 個股本益比／股價淨值比逐日累積（record_valuation_snapshot），供泡泡圖「估值」
    # 模式取「最新一筆」計算產業估值定位（見 compute_valuation_flow）。
    conn.execute(
        """CREATE TABLE IF NOT EXISTS valuation_history (
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            market TEXT,
            per REAL,
            pbr REAL,
            dividend_yield REAL,
            PRIMARY KEY (date, ticker)
        )"""
    )
    if "retrieved_at" not in {r[1] for r in conn.execute("PRAGMA table_info(valuation_history)")}:
        conn.execute("ALTER TABLE valuation_history ADD COLUMN retrieved_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_valuation_history_ticker_date "
        "ON valuation_history (ticker, date DESC)"
    )
    observations_exist = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='valuation_observations'"
    ).fetchone()
    conn.execute("""CREATE TABLE IF NOT EXISTS valuation_observations (
        date TEXT NOT NULL, ticker TEXT NOT NULL, market TEXT, per REAL, pbr REAL,
        dividend_yield REAL, retrieved_at TEXT NOT NULL,
        PRIMARY KEY (date, ticker, retrieved_at))""")
    if not observations_exist:
        conn.execute("""INSERT OR IGNORE INTO valuation_observations
            SELECT date, ticker, market, per, pbr, dividend_yield, retrieved_at
            FROM valuation_history WHERE retrieved_at IS NOT NULL""")
    conn.execute("CREATE TABLE IF NOT EXISTS analysis_revision (revision INTEGER NOT NULL)")
    conn.execute("INSERT INTO analysis_revision SELECT 0 WHERE NOT EXISTS (SELECT 1 FROM analysis_revision)")
    for table in ("daily_prices", "valuation_history", "valuation_observations", "industry_map"):
        for action in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS revision_{table}_{action} AFTER {action} ON {table} "
                "BEGIN UPDATE analysis_revision SET revision = revision + 1; END"
            )
    conn.commit()
    return conn


# ---------- 每日快照累積（TPEX 逐日累積的基礎來源，另見下方 upsert_daily_prices／
# tradingnote_finmind.backfill_tpex_history_via_finmind 的 FinMind 補缺口機制） ----------

def get_data_revision(db_path):
    """Persistent revision, including direct SQL corrections and backfills."""
    conn = _connect(db_path)
    try:
        return conn.execute("SELECT revision FROM analysis_revision").fetchone()[0]
    finally:
        conn.close()


def load_analysis_history(db_path, snapshot, period=None, days=5, history_days=None):
    """Resolve one calendar and read prices without a calendar-day heuristic."""
    from tradingnote_flow import FlowPeriod
    period = (period or FlowPeriod("0001-01-01", days)).resolve(db_path, snapshot)
    needed = period.actual_dates + period.prior_dates[-max(history_days or days, period.trading_days):]
    lower = min(needed, default=period.end_date)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ticker, date, close, trading_value, volume FROM daily_prices "
            "WHERE date >= ? AND date < ? ORDER BY date DESC", (lower, period.end_date)
        ).fetchall()
    finally:
        conn.close()
    history = {}
    for ticker, day, close, value, volume in rows:
        history.setdefault(ticker, []).append((day, close, value, volume))
    return period, history


def analysis_snapshot(db_path, snapshot, period):
    """For an explicit historical cutoff, never reuse a future snapshot close."""
    if not any(p.date > period.end_date for p in snapshot.values()):
        return snapshot
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ticker, date, close, change_pct, volume, trading_value FROM daily_prices "
            "WHERE date <= ? ORDER BY date DESC", (period.end_date,)
        ).fetchall()
    finally:
        conn.close()
    latest = {}
    for ticker, day, close, pct, volume, value in rows:
        latest.setdefault(ticker, (day, close, pct, volume, value))
    result = dict(snapshot)
    for ticker, price in snapshot.items():
        if price.date <= period.end_date:
            continue
        record = latest.get(ticker)
        if record is None:
            result[ticker] = replace(price, close=None, change=None, volume=None, trading_value=None)
            continue
        day, close, pct, volume, value = record
        change = close * pct / (100 + pct) if close is not None and pct is not None and pct != -100 else None
        result[ticker] = replace(price, date=day, close=close, change=change, volume=volume, trading_value=value)
    return result


def volume_day_count(price, history, period, volume_days):
    if price.date != period.end_date:
        return 0
    required = set(period.prior_dates[-volume_days:])
    return sum(row[0] in required and row[3] is not None for row in history)


def analysis_values(price, history, period, volume_days):
    selected = {row[0]: row for row in history}
    dates = period.actual_dates
    valid_today = price.date == period.end_date and price.date in dates and price.close is not None
    actual = int(valid_today) + sum(
        day in selected and selected[day][1] is not None
        for day in dates if day != period.end_date
    )
    sufficient = period.sufficient and actual == period.trading_days
    change = None
    if sufficient:
        if period.trading_days == 1:
            change = _change_pct(price)
        else:
            baseline = selected[dates[0]][1]
            if baseline and baseline > 0:
                change = (price.close / baseline - 1) * 100
    required_prior = period.prior_dates[-volume_days:]
    prior = [selected[d] for d in required_prior if d in selected]
    avg_volume = None
    if valid_today and len(prior) == volume_days and all(row[3] is not None for row in prior):
        avg_volume = sum(row[3] for row in prior) / volume_days
    value = (price.trading_value or 0) if valid_today else 0
    value += sum(selected[d][2] or 0 for d in dates if d != period.end_date and d in selected)
    return change, avg_volume, value, actual, sufficient


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


def upsert_valuation_history(db_path, rows):
    """寫入一批 (date, ticker, market, per, pbr, dividend_yield) tuple 到
    valuation_history（INSERT OR REPLACE），供逐日累積（record_valuation_snapshot）
    用。rows 為空時直接 return，不開連線。"""
    if not rows:
        return
    conn = _connect(db_path)
    try:
        observations = [(*row, datetime.now().isoformat()) for row in rows]
        conn.executemany(
            "INSERT OR REPLACE INTO valuation_observations VALUES (?, ?, ?, ?, ?, ?, ?)", observations
        )
        conn.executemany(
            """INSERT OR REPLACE INTO valuation_history
               (date, ticker, market, per, pbr, dividend_yield, retrieved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""", observations
        )
        conn.commit()
    finally:
        conn.close()


def record_valuation_snapshot(db_path, valuation_map, market, as_of_date=None):
    """保存來源交易日；無來源日期且未明確指定日期時跳過，禁止猜測日期。"""
    rows = [
        (v.get("date") or as_of_date, ticker, market, v.get("per"), v.get("pbr"), v.get("dividend_yield"))
        for ticker, v in valuation_map.items() if v.get("date") or as_of_date
    ]
    upsert_valuation_history(db_path, rows)


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


def parse_tpex_daily_quotes(payload):
    """解析櫃買新版 dailyQuotes 回應（「上櫃股票行情」表），格式同
    fetch_twse_historical_day 的 records；非交易日（表格為空）回傳 None。"""
    if str(payload.get("stat", "")).lower() != "ok":
        return None
    table = next(
        (t for t in payload.get("tables") or [] if (t.get("fields") or [None])[0] == "代號"),
        None,
    )
    if table is None or not table.get("data"):
        return None
    records = []
    for row in table["data"]:
        try:
            code, name = str(row[0]).strip(), str(row[1]).strip()
            close = to_float(row[2])
            change = to_float(row[3])
            volume = to_int(row[8])
            trading_value = to_float(row[9])
        except IndexError:
            continue
        if not code or close is None:
            continue
        prev_close = close - change if change is not None else None
        change_pct = change / prev_close * 100 if prev_close else None
        records.append(
            {
                "ticker": code,
                "name": name,
                "market": "TPEX",
                "close": close,
                "change": change,
                "change_pct": change_pct,
                "volume": volume,
                "trading_value": trading_value,
            }
        )
    return records or None


def fetch_tpex_historical_day(date_str):
    """date_str 格式 YYYYMMDD。回傳當天全部上櫃證券紀錄；非交易日回傳 None。
    跟 openapi 的 TPEX_URL 不同，這個端點可以查歷史日，不必依賴 FinMind 額度。"""
    url = f"{TPEX_DAILY_QUOTES_URL}?date={date_str[:4]}/{date_str[4:6]}/{date_str[6:]}&type=EW&response=json"
    try:
        data = http_get_json(url, timeout=30)
    except PriceFetchError:
        return None
    return parse_tpex_daily_quotes(data)


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

def compute_group_flow(db_path, snapshot, groups, avg_days=5, market_universe=None, period=None):
    """群組與成分股共用含截止日 N 筆收盤；1 日採官方當日漲跌。

    量比另需截止日以前完整 N 筆成交量，不將缺值當成零。
    漲幅以有完整價格期間的成分股當日成交金額加權。
    """
    period, ticker_history = load_analysis_history(db_path, snapshot, period, avg_days)
    snapshot = analysis_snapshot(db_path, snapshot, period)
    avg_days = period.trading_days
    normalized_groups = {
        str(name): set(members) for name, members in groups.items() if members
    }
    ticker_groups = {}
    for group_name, members in normalized_groups.items():
        for ticker in members:
            ticker_groups.setdefault(ticker, []).append(group_name)
    if market_universe is None:
        market_universe = set(get_industry_map(db_path))
    else:
        market_universe = set(market_universe)

    groups = {}
    for price in snapshot.values():
        if price.date != period.end_date or price.close is None or price.trading_value is None:
            continue
        group_names = ticker_groups.get(price.ticker)
        if not group_names:
            continue
        for group_name in group_names:
            g = groups.setdefault(
                group_name,
                {
                    "trading_value": 0.0,
                    "ticker_prices": {},
                    "ticker_volumes": {},
                    "ticker_values": {},
                    "ticker_dates": {},
                },
            )
            g["trading_value"] += price.trading_value
            g["ticker_prices"][price.ticker] = price.close
            g["ticker_volumes"][price.ticker] = price.volume or 0
            g["ticker_values"][price.ticker] = price.trading_value
            g["ticker_dates"][price.ticker] = price.date

    # 資金比重的分母用「全市場今日總成交金額」（含歷史資料不足、稍後會被過濾掉的產業），
    # 才能反映真實佔比；若只用 plotted 產業算分母，佔比會隨著哪些產業被過濾而跳動。
    # 概念分類可重疊，因此分母不能把各群組成交額相加（會重複計入同一檔股票）。
    # 一律直接用全市場個股母體的今日成交額；產業模式與舊版結果相同，概念模式則
    # 表示「該概念涵蓋全市場多少成交額」，各概念百分比加總允許超過 100%。
    total_market_value = sum(
        price.trading_value or 0.0
        for ticker, price in snapshot.items()
        if ticker in market_universe and price.date == period.end_date
        and price.close is not None
        and price.trading_value is not None
    )

    results = []
    for industry, g in groups.items():
        if g["trading_value"] <= 0:
            continue

        available_counts = []
        volume_counts = []
        weighted_change, change_weight = 0.0, 0.0
        daily_weighted_change = 0.0
        daily_change_weight = 0.0
        today_volume, baseline_volume = 0.0, 0.0
        baseline_trading_value = 0.0
        for ticker, today_close in g["ticker_prices"].items():
            price = snapshot[ticker]
            history = ticker_history.get(ticker, [])
            n_day_change_pct, avg_volume, _, actual, _ = analysis_values(price, history, period, avg_days)
            available_counts.append(actual)
            volume_counts.append(volume_day_count(price, history, period, avg_days))
            if n_day_change_pct is None:
                continue
            trading_value = g["ticker_values"][ticker]
            weighted_change += n_day_change_pct * trading_value
            change_weight += trading_value

            price = snapshot.get(ticker)
            daily_change_pct = _change_pct(price) if price is not None else None
            if daily_change_pct is not None:
                daily_weighted_change += daily_change_pct * trading_value
                daily_change_weight += trading_value

            if avg_volume is not None:
                baseline_volume += avg_volume
                prior = [row for row in history if row[0] in period.prior_dates[-avg_days:]]
                baseline_trading_value += sum(row[2] or 0 for row in prior) / avg_days
                today_volume += g["ticker_volumes"][ticker]

        avg_change_pct = (weighted_change / change_weight) if change_weight > 0 else None
        daily_change_pct = (
            daily_weighted_change / daily_change_weight if daily_change_weight > 0 else None
        )
        volume_ratio = (today_volume / baseline_volume) if baseline_volume > 0 else None
        turnover_ratio = (
            g["trading_value"] / baseline_trading_value
            if baseline_trading_value > 0
            else None
        )
        # 公開日資料沒有逐筆主動買賣方向。用「成交額 × 當日漲跌幅」作為方向強度
        # 的保守代理值：正值代表量價偏流入，負值代表量價偏流出；GUI 會清楚標示
        # 這是推估值，避免被誤讀為交易所公布的真實淨流入。
        directional_flow_value = (
            g["trading_value"] * daily_change_pct / 100
            if daily_change_pct is not None
            else None
        )

        capital_share_pct = (
            (g["trading_value"] / total_market_value * 100) if total_market_value > 0 else 0.0
        )

        results.append(
            IndustryFlow(
                industry=industry,
                actual_days=min(available_counts, default=0),
                required_days=period.trading_days,
                actual_volume_days=min(volume_counts, default=0),
                avg_change_pct=avg_change_pct,
                volume_ratio=volume_ratio,
                total_trading_value=g["trading_value"],
                capital_share_pct=capital_share_pct,
                stock_count=len(g["ticker_prices"]),
                daily_change_pct=daily_change_pct,
                turnover_ratio=turnover_ratio,
                directional_flow_value=directional_flow_value,
            )
        )

    # 三個原始指標的單位不同（百分比、倍數、資金比重），先各自在產業橫斷面
    # 轉成 0～100 百分位，再計算綜合熱度，避免任何一個數值尺度主導結果。
    popularity_values = [r.capital_share_pct for r in results]
    momentum_values = [r.avg_change_pct for r in results if r.avg_change_pct is not None]
    volume_values = [r.volume_ratio for r in results if r.volume_ratio is not None]
    for result in results:
        result.popularity_score = _cross_sectional_percentile(
            result.capital_share_pct, popularity_values
        )
        result.momentum_score = _cross_sectional_percentile(
            result.avg_change_pct, momentum_values
        )
        result.volume_score = _cross_sectional_percentile(
            result.volume_ratio, volume_values
        )
        scores = (
            result.popularity_score,
            result.momentum_score,
            result.volume_score,
        )
        if all(score is not None for score in scores):
            result.composite_score = sum(scores) / len(scores)

    results.sort(key=lambda r: r.total_trading_value, reverse=True)
    return results


def compute_industry_flow(db_path, snapshot, avg_days=5):
    """相容舊介面的官方產業流向；底層改走任意群組聚合器。"""
    industry_map = get_industry_map(db_path)
    groups = {}
    for ticker, industry in industry_map.items():
        groups.setdefault(industry, set()).add(ticker)
    return compute_group_flow(
        db_path,
        snapshot,
        groups,
        avg_days=avg_days,
        market_universe=industry_map,
    )


@dataclass
class IndustryValuationFlow:
    industry: str
    latest_valuation: float | None
    money_flow_ratio: float | None
    total_trading_value: float
    capital_share_pct: float
    stock_count: int


def _weighted_median(pairs):
    """pairs 為 [(value, weight), ...]；回傳加權中位數（權重累積到總權重一半時的
    value）。權重須為正數；輸入為空回傳 None。"""
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    total_weight = sum(w for _, w in pairs)
    if total_weight <= 0:
        return None
    half = total_weight / 2
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= half:
            return value
    return pairs[-1][0]


def compute_group_valuation_flow(
    db_path,
    snapshot,
    groups,
    short_days=5,
    long_days=20,
    metric="per",
    market_universe=None,
    period=None,
):
    """泡泡圖「估值」模式：X＝群組成分股「最新一筆」metric（本益比 PER 或股價
    淨值比 PBR，用今日成交金額加權中位數彙總到群組層級，見 _weighted_median 的
    取捨說明）；Y＝資金流入強度（近
    short_days 日均成交金額 ÷ 近 long_days 日均成交金額，短天期均額相對長天期均額
    墊高＝錢開始流入，比「今日單日 ÷ N 日均額」更不怕單日爆量雜訊）。找的是 X 低、
    Y 高的左上角（便宜、但錢已經在進）。

    X 只需要每檔股票「當下最新一筆」valuation_history 紀錄即可（不需要一段歷史去
    算百分位），只要今天（或最近一次）的 record_valuation_snapshot 有抓到就算數，
    TWSE／TPEX 都適用；不像先前的「自身歷史百分位」版本要等 backfill 補到
    MIN_VALUATION_HISTORY_POINTS 天以上才有值，那個版本在整批回補跑完前，
    大部分產業都會因為資料不足被濾掉不顯示，體驗上像是「泡泡圖沒蓋到所有族群」。"""
    assert metric in ("per", "pbr")
    normalized_groups = {
        str(name): set(members) for name, members in groups.items() if members
    }
    ticker_groups = {}
    for group_name, members in normalized_groups.items():
        for ticker in members:
            ticker_groups.setdefault(ticker, []).append(group_name)
    if market_universe is None:
        market_universe = set(get_industry_map(db_path))
    else:
        market_universe = set(market_universe)

    groups = {}
    for price in snapshot.values():
        if price.close is None or price.trading_value is None:
            continue
        group_names = ticker_groups.get(price.ticker)
        if not group_names:
            continue
        for group_name in group_names:
            g = groups.setdefault(
                group_name,
                {"trading_value": 0.0, "ticker_values": {}, "ticker_dates": {}},
            )
            g["trading_value"] += price.trading_value
            g["ticker_values"][price.ticker] = price.trading_value
            g["ticker_dates"][price.ticker] = price.date

    total_market_value = sum(
        price.trading_value or 0.0
        for ticker, price in snapshot.items()
        if ticker in market_universe
        and price.close is not None
        and price.trading_value is not None
    )

    min_history_days = max(short_days, long_days // 2)
    # 分界用 snapshot_today（snapshot 實際代表的交易日）而非 date.today()，理由見
    # _snapshot_today docstring。
    today_iso = period.end_date if period is not None else _snapshot_today(snapshot)
    cutoff = (date.fromisoformat(today_iso) - timedelta(days=long_days * 3)).isoformat()

    conn = _connect(db_path)
    try:
        value_rows = conn.execute(
            """SELECT ticker, date, trading_value FROM daily_prices
               WHERE date < ? AND date >= ? ORDER BY date DESC""",
            (today_iso, cutoff),
        ).fetchall()
        column = "per" if metric == "per" else "pbr"
        valuation_rows = conn.execute(
            f"""SELECT ticker, date, {column} FROM valuation_observations
               WHERE {column} IS NOT NULL AND date <= ?
               AND substr(retrieved_at, 1, 10) <= ? ORDER BY date DESC, retrieved_at DESC""",
            (today_iso, today_iso),
        ).fetchall()
    finally:
        conn.close()

    ticker_value_history = {}
    for ticker, day, tv in value_rows:
        ticker_value_history.setdefault(ticker, []).append((day, tv or 0.0))

    # valuation_rows 已經是 date DESC，同一檔股票第一次出現的即為目前最新一筆
    # （若今天已經跑過 record_valuation_snapshot，這筆就是今天的資料）。
    ticker_latest_valuation = {}
    for ticker, _day, value in valuation_rows:
        if ticker not in ticker_latest_valuation:
            ticker_latest_valuation[ticker] = value

    results = []
    for industry, g in groups.items():
        if g["trading_value"] <= 0:
            continue

        valuation_pairs = []
        short_sum, long_sum = 0.0, 0.0
        eligible_count = 0
        for ticker, tv in g["ticker_values"].items():
            latest_valuation = ticker_latest_valuation.get(ticker)
            if latest_valuation is None:
                continue
            # 個股自己的 snapshot 日期可能比多數股票（today_iso 眾數）更舊（少數個股
            # 資料延遲發布），這裡逐檔用該股自己的日期過濾歷史列，避免「今天」跟
            # 「N 天前」不小心撞到同一天，見 _snapshot_today docstring。
            ticker_date = g["ticker_dates"][ticker]
            days = [
                tv
                for day, tv in ticker_value_history.get(ticker, [])
                if day < ticker_date
            ]
            if len(days) < min_history_days:
                continue

            short_window = days[:short_days]
            long_window = days[:long_days]
            short_sum += sum(short_window) / len(short_window)
            long_sum += sum(long_window) / len(long_window)

            valuation_pairs.append((latest_valuation, tv))
            eligible_count += 1

        # 用成交金額加權「中位數」而非加權平均：本益比是比值，少數獲利趨近於零、
        # 本益比被推到數百甚至數千倍的個股（例如虧損邊緣、稅後淨利極小的公司），
        # 只要當天成交金額不小，就能把加權平均整個拉爆、跟該產業實際水準脫節；
        # 中位數不受這種極端值影響，維持「產業裡有代表性水準的那個數字」。
        latest_valuation_avg = _weighted_median(valuation_pairs)
        money_flow_ratio = (short_sum / long_sum) if long_sum > 0 else None

        capital_share_pct = (
            (g["trading_value"] / total_market_value * 100) if total_market_value > 0 else 0.0
        )

        results.append(
            IndustryValuationFlow(
                industry=industry,
                latest_valuation=latest_valuation_avg,
                money_flow_ratio=money_flow_ratio,
                total_trading_value=g["trading_value"],
                capital_share_pct=capital_share_pct,
                stock_count=eligible_count,
            )
        )

    results.sort(key=lambda r: r.total_trading_value, reverse=True)
    return results


def compute_valuation_flow(db_path, snapshot, short_days=5, long_days=20, metric="per"):
    """相容舊介面的官方產業估值流向；底層改走任意群組聚合器。"""
    industry_map = get_industry_map(db_path)
    groups = {}
    for ticker, industry in industry_map.items():
        groups.setdefault(industry, set()).add(ticker)
    return compute_group_valuation_flow(
        db_path,
        snapshot,
        groups,
        short_days=short_days,
        long_days=long_days,
        metric=metric,
        market_universe=industry_map,
    )


# ---------- 產業成分股（依累積成交金額排序，作為市值代理指標） ----------
# 本專案沒有股本／發行股數資料（PriceInfo、daily_prices 都只有收盤價、成交量、成交
# 金額），無法計算真正市值，因此「前N大成分股」改以區間累積成交金額排序，資料來源是
# 即時 snapshot（今日）＋ daily_prices（歷史），不額外打 API，跟「個股」分頁列清單的
# 原則一致。

def get_group_top_stocks_range(
    db_path, snapshot, members, days, top_n=30, volume_avg_days=5, period=None
):
    """Shared close-to-close return; one day uses the published daily change."""
    period, history = load_analysis_history(db_path, snapshot, period, days, volume_avg_days)
    snapshot = analysis_snapshot(db_path, snapshot, period)
    conn = _connect(db_path)
    try:
        valuations = conn.execute(
            "SELECT ticker, per FROM valuation_observations WHERE date <= ? AND substr(retrieved_at, 1, 10) <= ? ORDER BY date DESC, retrieved_at DESC",
            (period.end_date, period.end_date),
        ).fetchall()
    finally:
        conn.close()
    per = {}
    for ticker, value in valuations:
        per.setdefault(ticker, value)
    results = []
    for ticker in members:
        price = snapshot.get(ticker)
        if price is None:
            continue
        change, avg_volume, value, actual, sufficient = analysis_values(
            price, history.get(ticker, []), period, volume_avg_days
        )
        results.append(dict(
            ticker=ticker, name=price.name, close=price.close, change_pct=change,
            trading_value=value, today_volume=price.volume, avg_volume=avg_volume,
            per=per.get(ticker), actual_days=actual, required_days=period.trading_days,
            sufficient=sufficient, start_date=period.actual_dates[0] if period.actual_dates else period.start_date, end_date=period.end_date,
            data_date=price.date,
            actual_volume_days=volume_day_count(price, history.get(ticker, []), period, volume_avg_days),
            required_volume_days=volume_avg_days,
        ))
    results.sort(key=lambda row: row["trading_value"], reverse=True)
    return results[:top_n]


def get_industry_top_stocks_range(
    db_path, snapshot, industry, days, top_n=30, volume_avg_days=5, period=None
):
    """相容舊介面的官方產業成分股查詢。"""
    industry_map = get_industry_map(db_path)
    members = {
        ticker for ticker, ticker_industry in industry_map.items()
        if ticker_industry == industry
    }
    return get_group_top_stocks_range(
        db_path,
        snapshot,
        members,
        days,
        top_n=top_n,
        volume_avg_days=volume_avg_days,
        period=period,
    )


# ---------- 全市場個股資金流入排行 ----------

@dataclass
class StockCapitalFlow:
    """單一股票在指定區間內的資金流向代理指標。

    專案目前沒有逐筆主動買賣資料，因此這裡的「資金流入」以區間累積成交金額
    代表資金集中程度；不是法人淨買超，也不是全市場真正的資金淨流入。
    """

    ticker: str
    name: str
    market: str
    industry: str
    close: float | None
    change_pct: float | None
    trading_value: float
    today_trading_value: float
    today_volume: int | None
    avg_volume: float | None
    volume_ratio: float | None
    capital_share_pct: float
    actual_days: int = 0
    required_days: int = 0
    actual_volume_days: int = 0


def compute_stock_capital_flow(db_path, snapshot, avg_days=20, top_n=50, volume_avg_days=5, period=None):
    period, _ = load_analysis_history(db_path, snapshot, period, avg_days)
    snapshot = analysis_snapshot(db_path, snapshot, period)
    industry_map = get_industry_map(db_path)
    rows = get_group_top_stocks_range(db_path, snapshot, industry_map, avg_days,
                                    top_n=len(snapshot), volume_avg_days=volume_avg_days, period=period)
    total = sum(p.trading_value or 0 for p in snapshot.values() if p.date == period.end_date)
    results = []
    for row in rows:
        price = snapshot[row["ticker"]]
        avg = row["avg_volume"]
        results.append(StockCapitalFlow(
            ticker=price.ticker, name=price.name, market=price.market,
            actual_days=row["actual_days"], required_days=row["required_days"],
            actual_volume_days=row["actual_volume_days"],
            industry=industry_map[price.ticker], close=price.close, change_pct=row["change_pct"],
            trading_value=row["trading_value"], today_trading_value=(price.trading_value or 0) if price.date == period.end_date else 0,
            today_volume=price.volume, avg_volume=avg,
            volume_ratio=price.volume / avg if avg and price.volume is not None else None,
            capital_share_pct=(price.trading_value or 0) / total * 100 if total and price.date == period.end_date else 0,
        ))
    return results[:top_n]


# ---------- 個股量比異常清單 ----------

VOLUME_RATIO_TIERS = ("1.5～2倍", "2～3倍", "3倍以上")


@dataclass
class VolumeRatioOutlier:
    ticker: str
    name: str
    industry: str
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


def compute_volume_ratio_outliers(db_path, snapshot, avg_days=5, min_ratio=1.5, period=None, stock_rows=None):
    rows = stock_rows if stock_rows is not None else compute_stock_capital_flow(
        db_path, snapshot, avg_days, len(snapshot), avg_days, period
    )
    results = [VolumeRatioOutlier(
        ticker=r.ticker, name=r.name, industry=r.industry, market=r.market,
        close=r.close, change_pct=r.change_pct, today_volume=r.today_volume,
        avg_volume=r.avg_volume, volume_ratio=r.volume_ratio, tier=_volume_ratio_tier(r.volume_ratio),
    ) for r in rows if r.volume_ratio is not None and r.volume_ratio >= min_ratio]
    return sorted(results, key=lambda r: r.volume_ratio, reverse=True)
