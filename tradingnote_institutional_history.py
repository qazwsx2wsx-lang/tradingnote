"""三大法人歷史資料：逐日回補、類股彙總與「法人資金流去哪？」指標預先計算。

跟 tradingnote_institutional.py（只抓最新一筆快照、30 分鐘檔案快取）平行：這裡
用可帶日期的 T86／BFI82U／櫃買新版端點，把每個交易日寫進 history.db，讓近 5／20
日、加速流入、連續買賣天數等跨日指標有歷史可算。

擁有的表（ARCHITECTURE.md「一個模組管一張表」）：
  daily_institutional  個股三大法人買賣超股數（上市＋上櫃）
  market_summary       交易所公布的三大法人買賣金額（元，含 ETF），每個市場一列
  institutional_calendar 已確認的非交易日，避免每次回補都重打一次
  sector_map           類股成分股（industry_map＋sector_overrides.json 覆寫的結果）
  sector_metrics       類股 × 法人 × 日期 的預先計算指標（GUI「法人資金流去哪？」頁讀這張表）
  stock_metrics        個股 × 法人 × 日期 的當日金額、20 日累計、連續買賣天數

價格來自 tradingnote_history 擁有的 daily_prices（只透過它公開的讀寫函式寫入）。
金額單位：sector_metrics／stock_metrics 為「億元」；market_summary 為「元」。
"""

import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import tradingnote_history as history
from tradingnote_api_config import (
    TPEX_INSTI_DAILY_URL,
    TPEX_INSTI_SUMMARY_URL,
    TWSE_BFI82U_URL,
    TWSE_T86_HISTORY_URL,
)
from tradingnote_http import PriceFetchError, http_get_json, to_float, to_int

INVESTORS = ("all", "foreign", "trust", "dealer")
DEFAULT_TARGET_DAYS = 60
DEFAULT_DELAY_SECONDS = 3.0
TAIPEI = ZoneInfo("Asia/Taipei")
SECTOR_OVERRIDES_PATH = Path(__file__).resolve().parent / "sector_overrides.json"
AMOUNT_UNIT = 1e8  # 億


# ---------- DB ----------

def _connect(db_path):
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_institutional (
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            market TEXT NOT NULL,
            name TEXT,
            foreign_net_shares INTEGER NOT NULL,
            trust_net_shares INTEGER NOT NULL,
            dealer_net_shares INTEGER NOT NULL,
            PRIMARY KEY (date, ticker)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_institutional_ticker_date
            ON daily_institutional (ticker, date DESC);
        CREATE TABLE IF NOT EXISTS market_summary (
            date TEXT NOT NULL,
            market TEXT NOT NULL,
            foreign_amt REAL NOT NULL,
            trust_amt REAL NOT NULL,
            dealer_amt REAL NOT NULL,
            PRIMARY KEY (date, market)
        );
        CREATE TABLE IF NOT EXISTS institutional_calendar (
            date TEXT PRIMARY KEY,
            is_trading INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sector_map (
            ticker TEXT PRIMARY KEY,
            name TEXT,
            market TEXT,
            sector TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sector_map_sector ON sector_map (sector);
        CREATE TABLE IF NOT EXISTS sector_metrics (
            date TEXT NOT NULL,
            sector TEXT NOT NULL,
            investor TEXT NOT NULL,
            day_amt REAL NOT NULL,
            sum5 REAL NOT NULL,
            sum20 REAL NOT NULL,
            accel REAL,
            streak INTEGER NOT NULL,
            change5_pct REAL,
            trading_value REAL NOT NULL,
            stock_count INTEGER NOT NULL,
            window_days INTEGER NOT NULL,
            PRIMARY KEY (date, sector, investor)
        );
        CREATE INDEX IF NOT EXISTS idx_sector_metrics_sector
            ON sector_metrics (sector, investor, date DESC);
        CREATE TABLE IF NOT EXISTS stock_metrics (
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            investor TEXT NOT NULL,
            day_amt REAL NOT NULL,
            sum20 REAL NOT NULL,
            streak INTEGER NOT NULL,
            PRIMARY KEY (date, ticker, investor)
        );
        """
    )
    return conn


# ---------- 回應解析（純函式，方便測試） ----------

def _clean_label(text):
    return str(text or "").replace("　", "").replace("*", "").strip()


def parse_t86(payload):
    """T86 → [(ticker, name, foreign, trust, dealer)]；非交易日／未公布回傳 None。
    外資＝外陸資(不含外資自營商)＋外資自營商，跟 tradingnote_institutional 一致。"""
    if payload.get("stat") != "OK" or not payload.get("data"):
        return None
    rows = []
    for row in payload["data"]:
        if len(row) < 19:
            continue
        ticker = str(row[0]).strip()
        if not ticker:
            continue
        rows.append((
            ticker,
            str(row[1]).strip(),
            (to_int(row[4]) or 0) + (to_int(row[7]) or 0),
            to_int(row[10]) or 0,
            to_int(row[11]) or 0,
        ))
    return rows or None


def parse_tpex_insti(payload):
    """櫃買 dailyTrade(sect=EW) → 同 parse_t86 格式。欄位依序為：外資(不含自營)
    2-4、外資自營商 5-7、外資合計 8-10、投信 11-13、自營(自行) 14-16、自營(避險)
    17-19、自營合計 20-22、三大法人合計 23。"""
    if str(payload.get("stat", "")).lower() != "ok":
        return None
    table = next((t for t in payload.get("tables") or [] if t.get("data")), None)
    if table is None:
        return None
    rows = []
    for row in table["data"]:
        if len(row) < 24:
            continue
        ticker = str(row[0]).strip()
        if not ticker:
            continue
        rows.append((
            ticker,
            str(row[1]).strip(),
            to_int(row[10]) or 0,
            to_int(row[13]) or 0,
            to_int(row[22]) or 0,
        ))
    return rows or None


def _summary_by_label(rows):
    return {_clean_label(r[0]): to_float(r[-1]) or 0.0 for r in rows if r}


def parse_bfi82u(payload):
    """BFI82U → (foreign, trust, dealer) 元；非交易日回傳 None。"""
    if payload.get("stat") != "OK" or not payload.get("data"):
        return None
    m = _summary_by_label(payload["data"])
    foreign = m.get("外資及陸資(不含外資自營商)", 0.0) + m.get("外資自營商", 0.0)
    dealer = m.get("自營商(自行買賣)", 0.0) + m.get("自營商(避險)", 0.0)
    return foreign, m.get("投信", 0.0), dealer


def parse_tpex_summary(payload):
    """櫃買 insti/summary → (foreign, trust, dealer) 元；非交易日回傳 None。"""
    if str(payload.get("stat", "")).lower() != "ok":
        return None
    table = next((t for t in payload.get("tables") or [] if t.get("data")), None)
    if table is None:
        return None
    m = _summary_by_label(table["data"])
    return m.get("外資及陸資合計", 0.0), m.get("投信", 0.0), m.get("自營商合計", 0.0)


# ---------- 指標計算（純函式；單元測試見 test_tradingnote_institutional_history.py） ----------

def stock_amount(net_shares, close):
    """個股買賣超金額（億）＝ 張數 × 1000 × 收盤價 ÷ 1e8 ＝ 股數 × 收盤價 ÷ 1e8。"""
    return net_shares * close / AMOUNT_UNIT


def window_sum(values, n):
    """最後 n 個交易日的累計（不足 n 天就用現有天數）。values 由舊到新。"""
    return float(sum(values[-n:])) if n > 0 else 0.0


def acceleration(values, n=5):
    """加速流入（億/天）＝ 近 n 日平均每日買超 − 前 n 日（第 n+1～2n 日）平均。
    資料不足 2n 天時回傳 None（前段平均沒有意義，不硬算）。"""
    if len(values) < 2 * n:
        return None
    recent = values[-n:]
    previous = values[-2 * n:-n]
    return sum(recent) / n - sum(previous) / n


def streak(values):
    """連續買賣天數：從最新一天往回數同號的天數，買超為正、賣超為負；最新一天
    剛好為 0（或沒有資料）回傳 0。"""
    if not values or values[-1] == 0:
        return 0
    positive = values[-1] > 0
    count = 0
    for value in reversed(values):
        if value == 0 or (value > 0) != positive:
            break
        count += 1
    return count if positive else -count


def compound_change_pct(daily_pcts):
    """把逐日漲跌幅(%)複利成區間漲跌幅(%)。"""
    factor = 1.0
    for pct in daily_pcts:
        factor *= 1 + pct / 100
    return (factor - 1) * 100


def equal_weight_change(stock_changes):
    """類股區間漲跌＝成分股區間漲跌幅的等權平均；沒有成分股資料回傳 None。"""
    valid = [c for c in stock_changes if c is not None]
    return sum(valid) / len(valid) if valid else None


# ---------- 類股對照 ----------

def load_sector_overrides(path=SECTOR_OVERRIDES_PATH):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, {}, set()
    stocks = {str(k): str(v) for k, v in (data.get("stocks") or {}).items()}
    renames = {str(k): str(v) for k, v in (data.get("rename_sectors") or {}).items()}
    excluded = {str(s) for s in data.get("exclude_sectors") or []}
    return stocks, renames, excluded


def build_sector_map(directory, overrides_path=SECTOR_OVERRIDES_PATH):
    """directory 為 history.get_industry_directory() 的結果（已排除 ETF／權證，
    只含上市櫃公司）。回傳 {ticker: (name, market, sector)}。"""
    stocks, renames, excluded = load_sector_overrides(overrides_path)
    result = {}
    for ticker, info in directory.items():
        sector = stocks.get(ticker) or renames.get(info["industry"], info["industry"])
        if not sector or sector in excluded:
            continue
        result[ticker] = (info["name"], info["market"], sector)
    return result


def refresh_sector_map(db_path, overrides_path=SECTOR_OVERRIDES_PATH):
    mapping = build_sector_map(history.get_industry_directory(db_path), overrides_path)
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM sector_map")
        conn.executemany(
            "INSERT INTO sector_map (ticker, name, market, sector) VALUES (?, ?, ?, ?)",
            [(t, n, m, s) for t, (n, m, s) in mapping.items()],
        )
        conn.commit()
    finally:
        conn.close()
    return mapping


# ---------- 抓取與回補 ----------

def taipei_today():
    return datetime.now(TAIPEI).date()


def _get(url, delay_seconds):
    try:
        return http_get_json(url, timeout=30)
    finally:
        time.sleep(delay_seconds)


def fetch_day(db_path, day, delay_seconds=DEFAULT_DELAY_SECONDS, log=print):
    """抓一個日曆日的全部資料並寫入。回傳 True＝交易日已寫入、False＝非交易日或
    尚未公布。任一必要端點失敗會拋 PriceFetchError，該日不會被標記完成，下次重跑。"""
    ymd = day.strftime("%Y%m%d")
    slash = day.strftime("%Y/%m/%d")
    iso = day.isoformat()

    twse_rows = parse_t86(_get(
        f"{TWSE_T86_HISTORY_URL}?date={ymd}&selectType=ALL&response=json", delay_seconds))
    if twse_rows is None:
        return False
    twse_summary = parse_bfi82u(_get(
        f"{TWSE_BFI82U_URL}?type=day&dayDate={ymd}&response=json", delay_seconds))
    tpex_rows = parse_tpex_insti(_get(
        f"{TPEX_INSTI_DAILY_URL}?type=Daily&sect=EW&date={slash}&response=json", delay_seconds))
    tpex_summary = parse_tpex_summary(_get(
        f"{TPEX_INSTI_SUMMARY_URL}?type=Daily&date={slash}&response=json", delay_seconds))
    if twse_summary is None or tpex_rows is None or tpex_summary is None:
        raise PriceFetchError(f"{iso} 上市有法人資料，但 BFI82U／櫃買資料尚未齊全")

    price_counts = _price_counts(db_path, iso)
    for market, fetcher in (("TWSE", history.fetch_twse_historical_day),
                            ("TPEX", history.fetch_tpex_historical_day)):
        if price_counts.get(market, 0) >= 500:
            continue
        records = fetcher(ymd)
        time.sleep(delay_seconds)
        if not records:
            raise PriceFetchError(f"{iso} {market} 收盤行情抓取失敗")
        history.upsert_daily_prices(db_path, [
            (iso, r["ticker"], market, r["name"], r["close"], r["change_pct"],
             r["volume"], r["trading_value"])
            for r in records
        ])

    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM daily_institutional WHERE date = ?", (iso,))
        conn.executemany(
            """INSERT OR REPLACE INTO daily_institutional
               (date, ticker, market, name, foreign_net_shares, trust_net_shares, dealer_net_shares)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(iso, t, "TWSE", n, f, tr, d) for t, n, f, tr, d in twse_rows]
            + [(iso, t, "TPEX", n, f, tr, d) for t, n, f, tr, d in tpex_rows],
        )
        # market_summary 最後寫：兩個市場都有這張表的列，才算這天「完成」
        conn.executemany(
            "INSERT OR REPLACE INTO market_summary VALUES (?, ?, ?, ?, ?)",
            [(iso, "TWSE", *twse_summary), (iso, "TPEX", *tpex_summary)],
        )
        conn.execute("INSERT OR REPLACE INTO institutional_calendar VALUES (?, 1)", (iso,))
        conn.commit()
    finally:
        conn.close()
    log(f"{iso}：上市 {len(twse_rows)} 檔、上櫃 {len(tpex_rows)} 檔")
    return True


def _price_counts(db_path, iso):
    conn = history._connect(db_path)
    try:
        return dict(conn.execute(
            "SELECT market, COUNT(*) FROM daily_prices WHERE date = ? GROUP BY market", (iso,)
        ).fetchall())
    finally:
        conn.close()


def completed_dates(db_path):
    conn = _connect(db_path)
    try:
        return [r[0] for r in conn.execute(
            "SELECT date FROM market_summary GROUP BY date HAVING COUNT(*) = 2 ORDER BY date"
        )]
    finally:
        conn.close()


def _non_trading_dates(conn):
    return {r[0] for r in conn.execute(
        "SELECT date FROM institutional_calendar WHERE is_trading = 0")}


def backfill(db_path, target_days=DEFAULT_TARGET_DAYS, delay_seconds=DEFAULT_DELAY_SECONDS,
             end_date=None, log=print, failures=None):
    """從 end_date（預設台北今天）往回走，補到最近 target_days 個交易日都有資料。
    已完成的日期、已確認的非交易日會直接跳過，可中斷重跑；排程每天跑同一支即可
    自動補上漏掉的日子。回傳這次新寫入的日期清單（寫入後會重算指標）。
    傳入 failures（list）時，抓取失敗而被略過的日期會 append 進去，讓呼叫端能區分
    「已是最新」與「抓失敗」。"""
    today = taipei_today()
    cursor = end_date or today
    done = set(completed_dates(db_path))
    conn = _connect(db_path)
    try:
        skip = _non_trading_dates(conn)
    finally:
        conn.close()

    found = 0
    written = []
    lookback_cap = int(target_days * 1.6) + 15
    for _ in range(lookback_cap):
        if found >= target_days:
            break
        iso = cursor.isoformat()
        if iso in done:
            found += 1
        elif cursor.weekday() < 5 and iso not in skip:
            try:
                ok = fetch_day(db_path, cursor, delay_seconds, log)
            except PriceFetchError as exc:
                log(f"{iso}：略過（{exc}），下次重跑會再試")
                if failures is not None:
                    failures.append(iso)
                ok = None
            if ok:
                found += 1
                written.append(iso)
            elif ok is False:
                log(f"{iso}：非交易日或尚未公布")
                if cursor < today:
                    _mark_non_trading(db_path, iso)
        cursor -= timedelta(days=1)

    if written:
        refresh_sector_map(db_path)
        compute_metrics(db_path)
    return written


def _mark_non_trading(db_path, iso):
    conn = _connect(db_path)
    try:
        conn.execute("INSERT OR REPLACE INTO institutional_calendar VALUES (?, 0)", (iso,))
        conn.commit()
    finally:
        conn.close()


# ---------- 指標預先計算 ----------

def compute_metrics(db_path, keep_days=None):
    """用 daily_institutional × daily_prices × sector_map 重算全部日期的
    sector_metrics／stock_metrics（整批重寫，資料量是 N 天 × 約 2000 檔，數秒內完成）。"""
    dates = completed_dates(db_path)
    if keep_days:
        dates = dates[-keep_days:]
    if not dates:
        return 0
    conn = _connect(db_path)
    try:
        sectors = {t: s for t, s in conn.execute("SELECT ticker, sector FROM sector_map")}
        if not sectors:
            conn.close()
            refresh_sector_map(db_path)
            conn = _connect(db_path)
            sectors = {t: s for t, s in conn.execute("SELECT ticker, sector FROM sector_map")}
        placeholders = ",".join("?" * len(dates))
        inst = {}
        for d, t, f, tr, de in conn.execute(
            f"""SELECT date, ticker, foreign_net_shares, trust_net_shares, dealer_net_shares
                FROM daily_institutional WHERE date IN ({placeholders})""", dates):
            if t in sectors:
                inst[(d, t)] = (f, tr, de)
        prices = {}
        for d, t, close, pct, value in conn.execute(
            f"""SELECT date, ticker, close, change_pct, trading_value
                FROM daily_prices WHERE date IN ({placeholders})""", dates):
            if t in sectors and close is not None:
                prices[(d, t)] = (close, pct, value or 0.0)

        # 個股逐日金額（億）：沒有法人列＝當天 0；沒有收盤價＝無法估算，當 0
        members = {}
        for t, sector in sectors.items():
            members.setdefault(sector, []).append(t)
        stock_series = {t: {inv: [] for inv in INVESTORS} for t in sectors}
        sector_series = {}
        sector_rows, stock_rows = [], []
        for i, d in enumerate(dates):
            day_sector = {}
            for t, sector in sectors.items():
                price = prices.get((d, t))
                shares = inst.get((d, t), (0, 0, 0))
                amounts = [stock_amount(s, price[0]) if price else 0.0 for s in shares]
                amounts = [sum(amounts)] + amounts
                series = stock_series[t]
                for inv, amt in zip(INVESTORS, amounts):
                    series[inv].append(amt)
                agg = day_sector.setdefault(sector, {"amt": [0.0] * 4, "value": 0.0, "count": 0})
                if price:
                    agg["count"] += 1
                    agg["value"] += price[2] / AMOUNT_UNIT
                    for k in range(4):
                        agg["amt"][k] += amounts[k]
                    for inv, amt in zip(INVESTORS, amounts):
                        stock_rows.append((d, t, inv, amt, window_sum(series[inv], 20),
                                           streak(series[inv])))
            window = dates[max(0, i - 4):i + 1]
            for sector, agg in day_sector.items():
                if not agg["count"]:
                    continue
                per_inv = sector_series.setdefault(sector, {inv: [] for inv in INVESTORS})
                # 補齊這個類股之前沒出現的日子，讓序列跟 dates 對齊
                for inv in INVESTORS:
                    per_inv[inv].extend([0.0] * (i - len(per_inv[inv])))
                change5 = equal_weight_change(
                    _stock_window_change(t, window, prices) for t in members[sector]
                ) if len(window) == 5 else None
                for k, inv in enumerate(INVESTORS):
                    values = per_inv[inv]
                    values.append(agg["amt"][k])
                    sector_rows.append((
                        d, sector, inv, agg["amt"][k], window_sum(values, 5),
                        window_sum(values, 20), acceleration(values, 5), streak(values),
                        change5, agg["value"], agg["count"], min(len(values), 20),
                    ))
        conn.execute("DELETE FROM sector_metrics")
        conn.execute("DELETE FROM stock_metrics")
        conn.executemany(
            "INSERT INTO sector_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", sector_rows)
        conn.executemany("INSERT INTO stock_metrics VALUES (?, ?, ?, ?, ?, ?)", stock_rows)
        conn.commit()
        return len(sector_rows)
    finally:
        conn.close()


def _stock_window_change(ticker, window, prices):
    pcts = []
    for d in window:
        price = prices.get((d, ticker))
        if price is None or price[1] is None:
            return None
        pcts.append(price[1])
    return compound_change_pct(pcts)


# ---------- 給桌面 GUI（tradingnote_flow.FlowAnalysisService）用的區間彙總 ----------

def data_signature(db_path):
    """歷史法人資料的變動簽章，供呼叫端當快取 key（daily_institutional 沒有
    analysis_revision trigger，避免每次寫入都讓價格分析快取失效）。"""
    conn = _connect(db_path)
    try:
        return tuple(conn.execute("SELECT COUNT(*), MAX(date) FROM market_summary").fetchone())
    finally:
        conn.close()


def aggregate_group_flow_for_dates(db_path, groups, dates):
    """把 dates 期間每天的「股數 × 當日收盤」加總後依 groups 聚合，回傳
    tradingnote_institutional.InstitutionalGroupFlow（元）。只要 dates 有任何一天
    沒有歷史法人資料就回傳 None，讓呼叫端退回最新快照，不拿不完整的區間充數。"""
    from tradingnote_institutional import InstitutionalGroupFlow

    dates = tuple(dates)
    if not dates or not set(dates) <= set(completed_dates(db_path)):
        return None
    placeholders = ",".join("?" * len(dates))
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            f"""SELECT i.ticker, SUM(i.foreign_net_shares * p.close),
                       SUM(i.trust_net_shares * p.close), SUM(i.dealer_net_shares * p.close)
                FROM daily_institutional i
                JOIN daily_prices p ON p.date = i.date AND p.ticker = i.ticker
                WHERE i.date IN ({placeholders}) AND p.close IS NOT NULL
                GROUP BY i.ticker""",
            dates,
        ).fetchall()
    finally:
        conn.close()
    by_ticker = {t: (f, tr, d) for t, f, tr, d in rows}
    result = []
    for group, members in groups.items():
        covered = [by_ticker[t] for t in members if t in by_ticker]
        if covered:
            result.append(InstitutionalGroupFlow(
                group=group,
                foreign_value=sum(c[0] for c in covered),
                trust_value=sum(c[1] for c in covered),
                dealer_value=sum(c[2] for c in covered),
                covered_stocks=len(covered),
                date=dates[-1],
            ))
    return result


# ---------- 讀取（給 ui/pages/institutional_flow_page.py；不含任何 Qt） ----------

INVESTOR_LABELS = {"all": "合計", "foreign": "外資", "trust": "投信", "dealer": "自營商"}
SECTOR_COLUMNS = ("sector", "day_amt", "sum5", "sum20", "accel", "streak", "change5_pct",
                  "trading_value", "stock_count", "window_days")
BIG_MOVE_YI = 20  # 盤後摘要：單一類股超過 20 億用「大買／大賣」


def _rows(db_path, sql, params=()):
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def metric_dates(db_path):
    """有預先計算指標的交易日，新到舊。"""
    return [r["date"] for r in _rows(
        db_path, "SELECT DISTINCT date FROM sector_metrics ORDER BY date DESC")]


def sector_rows(db_path, date, investor):
    """某日某法人全部類股指標，依當日買賣超由大到小。"""
    return _rows(
        db_path,
        f"SELECT {', '.join(SECTOR_COLUMNS)} FROM sector_metrics "
        "WHERE date = ? AND investor = ? ORDER BY day_amt DESC",
        (date, investor),
    )


def market_amounts(db_path, date):
    """交易所公布的三大法人買賣差額（億，上市＋上櫃，含 ETF）。"""
    rows = _rows(db_path, "SELECT * FROM market_summary WHERE date = ?", (date,))
    result = {inv: sum(r[f"{inv}_amt"] for r in rows) / AMOUNT_UNIT
              for inv in ("foreign", "trust", "dealer")}
    result["all"] = sum(result.values())
    return result


def sector_history(db_path, sector, investor, date, days=30):
    """類股截至 date 最近 days 個交易日的每日買賣超 [(date, 億)]，舊到新。"""
    rows = _rows(
        db_path,
        "SELECT date, day_amt FROM sector_metrics WHERE sector = ? AND investor = ? "
        "AND date <= ? ORDER BY date DESC LIMIT ?",
        (sector, investor, date, days),
    )
    return [(r["date"], r["day_amt"]) for r in reversed(rows)]


def sector_stocks(db_path, sector, investor, date):
    """成分股明細（收盤、漲跌、當日買超、外資連買、20 日累計），依當日買超排序。
    daily_prices 屬於 tradingnote_history，這裡只讀取不寫入。"""
    return _rows(
        db_path,
        """SELECT m.ticker, m.name, p.close, p.change_pct, s.day_amt, s.sum20,
                  COALESCE(f.streak, 0) AS foreign_streak
           FROM sector_map m
           JOIN stock_metrics s ON s.ticker = m.ticker AND s.date = ? AND s.investor = ?
           LEFT JOIN stock_metrics f
                  ON f.ticker = m.ticker AND f.date = s.date AND f.investor = 'foreign'
           LEFT JOIN daily_prices p ON p.ticker = m.ticker AND p.date = s.date
           WHERE m.sector = ?
           ORDER BY s.day_amt DESC""",
        (date, investor, sector),
    )


def summary_text(rows_by_investor):
    """依各法人當日買賣超絕對值最大的類股組一句摘要；同動作同類股合併，例如
    「外資、投信大賣半導體業，自營商加碼航運業」。rows_by_investor：{investor: sector_rows}。"""
    groups = {}
    for inv in ("foreign", "trust", "dealer"):
        rows = rows_by_investor.get(inv) or []
        if not rows:
            continue
        top = max(rows, key=lambda r: abs(r["day_amt"]))
        amount = top["day_amt"]
        if abs(amount) < 0.5:
            continue
        if amount >= BIG_MOVE_YI:
            verb = "大買"
        elif amount > 0:
            verb = "加碼"
        elif amount <= -BIG_MOVE_YI:
            verb = "大賣"
        else:
            verb = "調節"
        groups.setdefault(f"{verb}{top['sector']}", []).append(INVESTOR_LABELS[inv])
    if not groups:
        return "三大法人今日類股進出不明顯"
    return "，".join(f"{'、'.join(who)}{action}" for action, who in groups.items())


def tu_yang_tag(foreign, trust, threshold=0.05):
    """外資（洋）與投信（土）同一期間的方向：同買／同賣／對作；任一方幾乎沒動回傳 None。"""
    if abs(foreign) < threshold or abs(trust) < threshold:
        return None
    if foreign > 0 and trust > 0:
        return "土洋同買"
    if foreign < 0 and trust < 0:
        return "土洋同賣"
    return "土洋對作"


def streak_label(n):
    if n > 0:
        return f"連買 {n} 天"
    if n < 0:
        return f"連賣 {-n} 天"
    return "—"
