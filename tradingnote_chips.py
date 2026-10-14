"""籌碼資料：融資融券逐日餘額與集保戶股權分散表（只負責抓取與存檔）。

跟 tradingnote_institutional_history.py 平行：每個交易日一次抓全市場，寫進 history.db，
讓之後的資金流向分析有歷史可算。這個模組目前只存資料，不參與任何分析。

擁有的表（ARCHITECTURE.md「一個模組管一張表」）：
  daily_margin   個股融資融券逐日餘額（上市＋上櫃，單位：張）
  tdcc_holders   集保戶股權分散表（個股 × 持股分級 × 週資料日；人數、股數、占比）
  tdcc_checked   集保歷史逐檔查詢已完成的（週資料日, 代號），含「查無資料」，讓回補可中斷續跑

融資融券只補 daily_prices 已有的交易日（交易日曆來源），所以不需要自己判斷休市。
集保 CSV 只提供最新一週，歷史只能從開始儲存那天起累積。
持股分級 1–15 為股數區間，12 起為 400 張（400,001 股）以上、15 為 1,000 張以上，
16 為差異數調整、17 為合計。
"""

import csv
import http.client
import http.cookiejar
import io
import re
import sqlite3
import time
import urllib.parse
from pathlib import Path
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

import tradingnote_history as history
from tradingnote_api_config import TDCC_HOLDERS_URL, TDCC_QUERY_URL, TPEX_MARGIN_URL, TWSE_MARGIN_URL
from tradingnote_http import PriceFetchError, http_get_json, to_float, to_int

DEFAULT_TARGET_DAYS = 60
DEFAULT_DELAY_SECONDS = 3.0
BIG_HOLDER_LEVEL = 12   # 持股分級 ≥12＝400 張以上
HUGE_HOLDER_LEVEL = 15  # 持股分級 15＝1,000 張以上
TOTAL_LEVEL = 17


# ---------- DB ----------

def _connect(db_path):
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS daily_margin (
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            market TEXT NOT NULL,
            margin_buy INTEGER NOT NULL,
            margin_sell INTEGER NOT NULL,
            margin_repay INTEGER NOT NULL,
            margin_balance INTEGER NOT NULL,
            short_sell INTEGER NOT NULL,
            short_buy INTEGER NOT NULL,
            short_repay INTEGER NOT NULL,
            short_balance INTEGER NOT NULL,
            offset_qty INTEGER NOT NULL,
            PRIMARY KEY (date, ticker)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_margin_ticker_date
            ON daily_margin (ticker, date DESC);
        CREATE TABLE IF NOT EXISTS tdcc_holders (
            data_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            level INTEGER NOT NULL,
            holders INTEGER NOT NULL,
            shares INTEGER NOT NULL,
            pct REAL NOT NULL,
            PRIMARY KEY (data_date, ticker, level)
        );
        CREATE INDEX IF NOT EXISTS idx_tdcc_holders_ticker
            ON tdcc_holders (ticker, data_date DESC);
        CREATE TABLE IF NOT EXISTS tdcc_checked (
            data_date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            PRIMARY KEY (data_date, ticker)
        );
        """
    )
    return conn


# ---------- 融資融券 ----------

def _n(value):
    return to_int(str(value).replace(",", "")) or 0


def parse_twse_margin(payload):
    """MI_MARGN → [(ticker, mb, ms, mr, mbal, ss, sb, sr, sbal, offset)]；
    非交易日／未公布回傳 None。欄位順序：融資買進、賣出、現金償還、前日餘額、今日餘額、
    限額；融券買進、賣出、現券償還、前日餘額、今日餘額、限額；資券互抵。
    融券的「賣出」是放空、「買進」是回補，這裡統一成 short_sell／short_buy。"""
    if payload.get("stat") != "OK":
        return None
    rows = []
    for table in payload.get("tables") or []:
        fields = table.get("fields") or []
        if "代號" not in fields or len(fields) < 15:
            continue
        for row in table.get("data") or []:
            ticker = str(row[0]).strip().replace("　", "")
            if not ticker or len(row) < 15:
                continue
            rows.append((
                ticker, _n(row[2]), _n(row[3]), _n(row[4]), _n(row[6]),
                _n(row[9]), _n(row[8]), _n(row[10]), _n(row[12]), _n(row[14]),
            ))
    return rows or None


def parse_tpex_margin(payload):
    """櫃買融資融券餘額 → 同 parse_twse_margin 的 tuple。欄位：代號、名稱、前資餘額、資買、
    資賣、現償、資餘額、資屬證金、資使用率、資限額、前券餘額、券賣、券買、券償、券餘額…資券相抵。"""
    rows = []
    for table in payload.get("tables") or []:
        fields = table.get("fields") or []
        if "代號" not in fields or len(fields) < 19:
            continue
        for row in table.get("data") or []:
            ticker = str(row[0]).strip()
            if not ticker or len(row) < 19:
                continue
            rows.append((
                ticker, _n(row[3]), _n(row[4]), _n(row[5]), _n(row[6]),
                _n(row[11]), _n(row[12]), _n(row[13]), _n(row[14]), _n(row[18]),
            ))
    return rows or None


def fetch_margin_day(db_path, iso, delay_seconds=DEFAULT_DELAY_SECONDS, log=print):
    """抓一個交易日的上市＋上櫃融資融券並寫入。回傳 True＝已寫入、False＝尚未公布。
    任何一個市場失敗都不寫（避免半套資料），該日下次重跑。"""
    ymd = iso.replace("-", "")
    slash = iso.replace("-", "/")
    twse = parse_twse_margin(http_get_json(
        f"{TWSE_MARGIN_URL}?date={ymd}&selectType=STOCK&response=json", timeout=30))
    time.sleep(delay_seconds)
    if twse is None:
        return False
    tpex = parse_tpex_margin(http_get_json(
        f"{TPEX_MARGIN_URL}?date={slash}&response=json", timeout=30))
    time.sleep(delay_seconds)
    if tpex is None:
        raise PriceFetchError(f"{iso} 上市有融資融券資料，但櫃買尚未公布")
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM daily_margin WHERE date = ?", (iso,))
        conn.executemany(
            "INSERT OR REPLACE INTO daily_margin VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(iso, r[0], "TWSE", *r[1:]) for r in twse]
            + [(iso, r[0], "TPEX", *r[1:]) for r in tpex],
        )
        conn.commit()
    finally:
        conn.close()
    log(f"{iso} 融資融券：上市 {len(twse)} 檔、上櫃 {len(tpex)} 檔")
    return True


def margin_dates(db_path):
    """上市、上櫃都已存入的日期（由舊到新）。"""
    conn = _connect(db_path)
    try:
        return [r[0] for r in conn.execute(
            "SELECT date FROM daily_margin GROUP BY date HAVING COUNT(DISTINCT market) = 2 ORDER BY date")]
    finally:
        conn.close()


def backfill_margin(db_path, target_days=DEFAULT_TARGET_DAYS, delay_seconds=DEFAULT_DELAY_SECONDS,
                    log=print, failures=None):
    """補齊最近 target_days 個交易日（以 daily_prices 的日期為交易日曆）的融資融券。
    已完成的日期跳過，可中斷重跑；回傳這次新寫入的日期。"""
    done = set(margin_dates(db_path))
    written = []
    for iso in history.get_available_dates(db_path)[-target_days:]:
        if iso in done:
            continue
        try:
            ok = fetch_margin_day(db_path, iso, delay_seconds, log)
        except PriceFetchError as exc:
            log(f"{iso} 融資融券：略過（{exc}），下次重跑會再試")
            if failures is not None:
                failures.append(iso)
            continue
        if ok:
            written.append(iso)
        else:
            log(f"{iso} 融資融券：尚未公布")
    return written


def margin_history(db_path, ticker, days=60):
    """個股近 days 天的融資融券餘額（由舊到新）：[(date, margin_balance, short_balance)]。"""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT date, margin_balance, short_balance FROM daily_margin "
            "WHERE ticker = ? ORDER BY date DESC LIMIT ?", (ticker, days)).fetchall()
    finally:
        conn.close()
    return rows[::-1]


# ---------- 集保戶股權分散表 ----------

def parse_tdcc_csv(text):
    """集保 CSV → [(data_date ISO, ticker, level, holders, shares, pct)]。"""
    rows = []
    for rec in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if len(rec) < 6 or not rec[0].strip().isdigit():
            continue
        d = rec[0].strip()
        level = to_int(rec[2])
        if level is None:
            continue
        rows.append((f"{d[:4]}-{d[4:6]}-{d[6:8]}", rec[1].strip(), level,
                     _n(rec[3]), _n(rec[4]), to_float(rec[5]) or 0.0))
    return rows


def _fetch_tdcc_text(url=TDCC_HOLDERS_URL):
    try:
        with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as resp:
            return resp.read().decode("utf-8-sig")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise PriceFetchError(f"集保資料抓取失敗：{exc}") from exc


def tdcc_dates(db_path):
    conn = _connect(db_path)
    try:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT data_date FROM tdcc_holders ORDER BY data_date")]
    finally:
        conn.close()


def fetch_tdcc_snapshot(db_path, text=None, log=print):
    """抓（或解析傳入的）最新一週集保分散表並存檔；只留下 daily_prices 有出現過的代號，
    排除權證等衍生品。該週已存在則不重抓寫入。回傳該週資料日（ISO），資料為空回傳 None。"""
    text = text if text is not None else _fetch_tdcc_text()
    rows = parse_tdcc_csv(text)
    if not rows:
        return None
    data_date = max(r[0] for r in rows)
    if data_date in tdcc_dates(db_path):
        log(f"集保 {data_date}：已存在")
        return data_date
    conn = history._connect(db_path)
    try:
        known = {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM daily_prices")}
    finally:
        conn.close()
    keep = [r for r in rows if r[1] in known] if known else rows
    conn = _connect(db_path)
    try:
        conn.executemany("INSERT OR REPLACE INTO tdcc_holders VALUES (?,?,?,?,?,?)", keep)
        conn.commit()
    finally:
        conn.close()
    log(f"集保 {data_date}：{len({r[1] for r in keep})} 檔")
    return data_date


def holder_summary(db_path, ticker):
    """個股各週大戶概況（由舊到新）：[{date, holders, big_pct, huge_pct}]。
    holders＝總股東人數（合計列）、big_pct＝400 張以上占比、huge_pct＝1,000 張以上占比。"""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT data_date, level, holders, pct FROM tdcc_holders WHERE ticker = ? "
            "ORDER BY data_date", (ticker,)).fetchall()
    finally:
        conn.close()
    by_date = {}
    for date, level, holders, pct in rows:
        d = by_date.setdefault(date, {"date": date, "holders": 0, "big_pct": 0.0, "huge_pct": 0.0})
        # 合計列通常是 17；沒有「差異數調整」列的週，合計就在 16
        if level == TOTAL_LEVEL or (level == TOTAL_LEVEL - 1 and holders and not d["holders"]):
            d["holders"] = holders
        if BIG_HOLDER_LEVEL <= level <= HUGE_HOLDER_LEVEL:
            d["big_pct"] += pct
        if level == HUGE_HOLDER_LEVEL:
            d["huge_pct"] += pct
    return list(by_date.values())


# ---------- 集保歷史回補（官網逐檔逐週查詢） ----------

_TOKEN_RE = re.compile(r'SYNCHRONIZER_TOKEN" value="([^"]*)')
_DATE_RE = re.compile(r'<option value="(\d{8})"')
_ROW_RE = re.compile(
    r'<tr>\s*<td align="center">(\d+)</td>\s*<td[^>]*>[^<]*</td>\s*'
    r'<td align="right">([^<]*)</td>\s*<td align="right">([^<]*)</td>\s*'
    r'<td align="right">([^<]*)</td>', re.S)
MAX_CONSECUTIVE_FAILURES = 5


def parse_tdcc_query(html, iso, ticker):
    """官網查詢結果 HTML → [(data_date, ticker, level, holders, shares, pct)]；查無資料回傳 []。"""
    rows = []
    for level, holders, shares, pct in _ROW_RE.findall(html):
        rows.append((iso, ticker, int(level), _n(holders), _n(shares), to_float(pct.replace(",", "")) or 0.0))
    return rows


class TdccQueryClient:
    """集保官網查詢的 session：表單 token 是一次性的，每次回應帶下一個，所以請求要循序進行。"""

    def __init__(self, timeout=30):
        self._opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self._opener.addheaders = [("User-Agent", "Mozilla/5.0")]
        self._timeout = timeout
        self._token = None
        self._dates = []
        self._load_form()

    def _load_form(self):
        try:
            page = self._opener.open(TDCC_QUERY_URL, timeout=self._timeout).read().decode("utf-8", "replace")
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise PriceFetchError(f"集保查詢頁連線失敗：{exc}") from exc
        match = _TOKEN_RE.search(page)
        if not match:
            raise PriceFetchError("集保查詢頁格式改變（找不到表單 token）")
        self._token = match.group(1)
        self._dates = _DATE_RE.findall(page)

    @property
    def dates(self):
        """官網可查的週資料日（YYYYMMDD，新到舊）。"""
        return list(self._dates)

    def query(self, ticker, ymd):
        """查一檔一週；回傳 parse_tdcc_query 的列（查無資料為 []）。失敗拋 PriceFetchError。"""
        if self._token is None:
            self._load_form()
        data = urllib.parse.urlencode({
            "SYNCHRONIZER_TOKEN": self._token, "SYNCHRONIZER_URI": "/portal/zh/smWeb/qryStock",
            "method": "submit", "firDate": self._dates[0] if self._dates else ymd,
            "scaDate": ymd, "sqlMethod": "StockNo", "stockNo": ticker, "stockName": "",
        }).encode()
        self._token = None  # token 已用掉；回應沒帶新的就下次重新載入表單
        try:
            page = self._opener.open(TDCC_QUERY_URL, data, timeout=self._timeout).read().decode("utf-8", "replace")
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise PriceFetchError(f"集保查詢連線失敗：{exc}") from exc
        match = _TOKEN_RE.search(page)
        if match:
            self._token = match.group(1)
        iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
        rows = parse_tdcc_query(page, iso, ticker)
        if rows:
            return rows
        if "查無此資料" in page:
            return []
        raise PriceFetchError("集保查詢回應格式無法辨識（可能被限流或 token 失效）")


def pending_tdcc_pairs(db_path, tickers, dates):
    """還沒查過的 [(ticker, YYYYMMDD)]：依代號分組（同一檔連續查完），週由新到舊。
    已有資料（CSV 或先前查詢）或已確認查無資料的組合不再出現。"""
    conn = _connect(db_path)
    try:
        have = {(r[0], r[1]) for r in conn.execute("SELECT DISTINCT data_date, ticker FROM tdcc_holders")}
        have |= {(r[0], r[1]) for r in conn.execute("SELECT data_date, ticker FROM tdcc_checked")}
    finally:
        conn.close()
    pairs = []
    for ticker in tickers:
        for ymd in dates:
            iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
            if (iso, ticker) not in have:
                pairs.append((ticker, ymd))
    return pairs


def backfill_tdcc_history(db_path, tickers, cancel_event=None, progress=None,
                          delay_seconds=1.0, client=None, weeks=None):
    """逐檔逐週補集保歷史。可隨時由 cancel_event 中斷，下次呼叫會從沒查過的組合繼續
    （每查完一筆就已寫入並標記）。progress(done, total, label) 的 done／total 含先前已完成的，
    所以續跑時進度條從原本的位置接續。連續失敗 MAX_CONSECUTIVE_FAILURES 次就拋
    PriceFetchError（避免被限流時空轉）。weeks 只補最近幾週（None＝官網全部約一年）。回傳 {"done", "total", "stopped", "failed"}。"""
    client = client or TdccQueryClient()
    dates = client.dates[:weeks] if weeks else client.dates  # 官網日期新到舊
    tickers = list(tickers)
    total = len(tickers) * len(dates)
    pairs = pending_tdcc_pairs(db_path, tickers, dates)
    done = total - len(pairs)
    failed = streak = 0
    stopped = False
    if progress:
        progress(done, total, "準備中")
    for ticker, ymd in pairs:
        if cancel_event is not None and cancel_event.is_set():
            stopped = True
            break
        iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
        try:
            rows = client.query(ticker, ymd)
            streak = 0
        except PriceFetchError as exc:
            failed += 1
            streak += 1
            if streak >= MAX_CONSECUTIVE_FAILURES:
                raise PriceFetchError(f"連續 {streak} 次查詢失敗，已停止：{exc}") from exc
            time.sleep(delay_seconds * 3)
            continue
        conn = _connect(db_path)
        try:
            conn.executemany("INSERT OR REPLACE INTO tdcc_holders VALUES (?,?,?,?,?,?)", rows)
            conn.execute("INSERT OR REPLACE INTO tdcc_checked VALUES (?, ?)", (iso, ticker))
            conn.commit()
        finally:
            conn.close()
        done += 1
        if progress:
            progress(done, total, f"{ticker}　{iso}")
        time.sleep(delay_seconds)
    return {"done": done, "total": total, "stopped": stopped, "failed": failed}


def big_holder_changes(db_path, tickers, start_date, end_date):
    """流向區間內 400 張以上大戶持股占比的變化（百分點）。

    集保是週資料，所以取「end_date 以前最新的一週」對照「start_date 之前最新的一週」
    （區間開始前的基準）。回傳 {ticker: {"latest","base","delta","trail"}} 與 (base_date, end_date)；
    trail＝基準週到最新週之間每一週的 [(日期, 占比)]（由舊到新），看得出是持續增加還是先升後降。
    任一週沒有資料就回傳 ({}, None)。"""
    tickers = list(tickers)
    conn = _connect(db_path)
    try:
        end = conn.execute(
            "SELECT MAX(data_date) FROM tdcc_holders WHERE data_date <= ?", (end_date,)).fetchone()[0]
        base = conn.execute(
            "SELECT MAX(data_date) FROM tdcc_holders WHERE data_date < ?", (start_date,)).fetchone()[0]
        if not end or not base or end == base or not tickers:
            return {}, None
        weeks = [r[0] for r in conn.execute(
            "SELECT DISTINCT data_date FROM tdcc_holders WHERE data_date BETWEEN ? AND ? ORDER BY data_date",
            (base, end))]
        marks = ",".join("?" * len(tickers))
        pct = {}
        for date in weeks:
            pct[date] = dict(conn.execute(
                f"SELECT ticker, SUM(pct) FROM tdcc_holders WHERE data_date = ? "
                f"AND level BETWEEN {BIG_HOLDER_LEVEL} AND {HUGE_HOLDER_LEVEL} "
                f"AND ticker IN ({marks}) GROUP BY ticker", (date, *tickers)).fetchall())
    finally:
        conn.close()
    result = {
        t: {"latest": pct[end][t], "base": pct[base][t], "delta": pct[end][t] - pct[base][t],
            "trail": [(d, round(pct[d][t], 2)) for d in weeks if t in pct[d]]}
        for t in tickers if t in pct[end] and t in pct[base]
    }
    return result, (base, end)


def group_big_holder_changes(db_path, groups, start_date, end_date):
    """族群層級的 400 張以上大戶持股占比變化（百分點），用股數加權：
    占比＝成分股大戶股數合計 ÷ 成分股集保總股數合計，只算「區間前後兩週都有資料」的成分股，
    兩個時點用同一批股票，避免成員不同造成假變化。

    groups：{族群名: 代號集合}。回傳 ({族群: {"delta","latest","base","covered","total"}}, (base, end))；
    沒有基準週或最新週時回傳 ({}, None)。covered／total＝有資料的成分股數／成分股數。"""
    members = {t for group in groups.values() for t in group}
    conn = _connect(db_path)
    try:
        end = conn.execute(
            "SELECT MAX(data_date) FROM tdcc_holders WHERE data_date <= ?", (end_date,)).fetchone()[0]
        base = conn.execute(
            "SELECT MAX(data_date) FROM tdcc_holders WHERE data_date < ?", (start_date,)).fetchone()[0]
        if not end or not base or end == base or not members:
            return {}, None
        stock = {}
        for date in (base, end):
            stock[date] = {
                t: (big or 0, total or 0)
                for t, big, total in conn.execute(
                    f"SELECT ticker, SUM(CASE WHEN level BETWEEN {BIG_HOLDER_LEVEL} AND {HUGE_HOLDER_LEVEL} "
                    f"THEN shares ELSE 0 END), SUM(shares) FROM tdcc_holders "
                    f"WHERE data_date = ? AND level < 16 GROUP BY ticker", (date,))
                if t in members
            }
    finally:
        conn.close()
    result = {}
    for name, tickers in groups.items():
        both = [t for t in tickers if stock[base].get(t, (0, 0))[1] > 0 and stock[end].get(t, (0, 0))[1] > 0]
        if not both:
            continue
        ratios = {}
        for date in (base, end):
            big = sum(stock[date][t][0] for t in both)
            total = sum(stock[date][t][1] for t in both)
            ratios[date] = big * 100.0 / total
        result[name] = {"delta": ratios[end] - ratios[base], "latest": ratios[end],
                        "base": ratios[base], "covered": len(both), "total": len(tickers)}
    return result, (base, end)
