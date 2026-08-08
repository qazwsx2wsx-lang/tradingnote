#!/usr/bin/env python3
"""tradingnote 核心模組 - 部位紀錄、台股價格查詢、損益分析（不依賴任何介面）"""

import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from tradingnote_http import PriceFetchError, http_get_json, to_float, to_int

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TPEX_PERATIO_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"

CACHE_TTL_SECONDS = 30 * 60
# 上櫃股票本益比／殖利率／股價淨值比，實測跟 FinMind 同一天數值完全一致（FinMind
# 本來就是轉手同一份原始資料），改用這個官方端點可以避免消耗 FinMind 的 600 次／
# 小時額度。TTL 沿用跟 CACHE_TTL_SECONDS 一樣的 30 分鐘，純粹是擋短時間內重複查詢，
# 這份資料本來就是一天只更新一次。
TPEX_VALUATION_CACHE_TTL_SECONDS = 30 * 60

TICKER_RE = re.compile(r"^[0-9A-Za-z]{4,6}$")

DEFAULT_SETTINGS = {
    "auto_check_continuity": True,
    # TWSE 歷史回補要補到幾個交易日為止（見 tradingnote_history.py 的
    # DEFAULT_BACKFILL_TARGET_DAYS，兩邊預設值需一致）。只影響 TWSE——TPEX
    # 沒有可用的免費歷史回補端點，只能靠每日快照逐日累積。
    "backfill_target_days": 120,
    # FinMind（finmindtrade.com）API token，用於「個股」模組查詢本益比／法人買賣，
    # 留空仍可打 API 但額度極低、部分資料集查不到。CLI／GUI 共用同一份 settings.json。
    "finmind_token": "",
    # Gemini（Google AI）API key，用於「AI 助理」分頁的自然語言問答（Gemini
    # 自動函式呼叫，呼叫 FinMind／歷史資料庫查詢函式）；留空則該分頁無法使用。
    "gemini_api_key": "",
}


@dataclass
class Position:
    ticker: str
    shares: int
    entry_price: float
    entry_date: str
    note: str = ""
    name: str = ""
    market: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    # 新增或編輯這筆部位的那一天（YYYY-MM-DD），不是股票的交易日。舊資料沒有這個
    # 欄位時 load_positions 用 dataclass 預設值補空字串，GUI／CLI 顯示成 "-"。
    updated_at: str = ""


@dataclass
class PriceInfo:
    ticker: str
    name: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    change: float | None
    volume: int | None
    date: str
    market: str
    trading_value: float | None = None


@dataclass
class PnLResult:
    current_price: float
    current_value: float
    cost_basis: float
    unrealized_pnl: float
    pnl_pct: float


# ---------- 設定：JSON 持久化 ----------

def load_settings(path):
    p = Path(path)
    if not p.exists():
        return dict(DEFAULT_SETTINGS)
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return {**DEFAULT_SETTINGS, **raw}


def save_settings(path, settings):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


# ---------- 紀錄：部位 CRUD 與 JSON 持久化 ----------

def load_positions(path):
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return [Position(**item) for item in raw]


def save_positions(path, positions):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump([asdict(pos) for pos in positions], f, ensure_ascii=False, indent=2)


def validate_position_input(ticker, shares, entry_price, entry_date):
    if not TICKER_RE.match(ticker):
        raise ValueError("股票代號格式不正確（應為 4-6 碼英數字）。")
    if shares <= 0:
        raise ValueError("股數必須大於 0。")
    if entry_price <= 0:
        raise ValueError("成本價必須大於 0。")
    try:
        date.fromisoformat(entry_date)
    except ValueError:
        raise ValueError("日期格式不正確（應為 YYYY-MM-DD）。")


def add_position(positions, ticker, shares, entry_price, entry_date, note="", snapshot=None):
    ticker = ticker.strip().upper()
    validate_position_input(ticker, shares, entry_price, entry_date)
    name, market = "", None
    if snapshot is not None:
        price = lookup_price(ticker, snapshot)
        if price is not None:
            name, market = price.name, price.market
    pos = Position(
        ticker=ticker,
        shares=shares,
        entry_price=entry_price,
        entry_date=entry_date,
        note=note,
        name=name,
        market=market,
        updated_at=date.today().isoformat(),
    )
    positions.append(pos)
    return pos


def find_position(positions, position_id):
    for pos in positions:
        if pos.id == position_id or pos.id.startswith(position_id):
            return pos
    return None


def update_position(positions, position_id, shares, entry_price, entry_date, note="", snapshot=None):
    """修改既有部位的股數／成本價／日期／備註，代號不可變更（換股票語意上該用
    刪除+新增，不是編輯）。找不到這個 id 時丟 ValueError，訊息可直接顯示給使用者。"""
    pos = find_position(positions, position_id)
    if pos is None:
        raise ValueError("找不到這個部位。")
    validate_position_input(pos.ticker, shares, entry_price, entry_date)
    pos.shares = shares
    pos.entry_price = entry_price
    pos.entry_date = entry_date
    pos.note = note
    if snapshot is not None:
        price = lookup_price(pos.ticker, snapshot)
        if price is not None:
            pos.name, pos.market = price.name, price.market
    pos.updated_at = date.today().isoformat()
    return pos


def remove_position(positions, position_id):
    pos = find_position(positions, position_id)
    if pos is None:
        return False
    positions.remove(pos)
    return True


# ---------- 查價：台股 TWSE／TPEX 價格擷取 ----------

def _roc_to_iso(roc_date):
    roc_date = str(roc_date).strip()
    if len(roc_date) < 6:
        return ""
    year = int(roc_date[:-4]) + 1911
    month = roc_date[-4:-2]
    day = roc_date[-2:]
    return f"{year:04d}-{month}-{day}"


def fetch_twse_all():
    rows = http_get_json(TWSE_URL)
    result = {}
    for rec in rows:
        code = rec.get("Code", "").strip()
        if not code:
            continue
        result[code] = PriceInfo(
            ticker=code,
            name=rec.get("Name", "").strip(),
            open=to_float(rec.get("OpeningPrice")),
            high=to_float(rec.get("HighestPrice")),
            low=to_float(rec.get("LowestPrice")),
            close=to_float(rec.get("ClosingPrice")),
            change=to_float(rec.get("Change")),
            volume=to_int(rec.get("TradeVolume")),
            date=_roc_to_iso(rec.get("Date", "")),
            market="TWSE",
            trading_value=to_float(rec.get("TradeValue")),
        )
    return result


def fetch_tpex_all():
    rows = http_get_json(TPEX_URL)
    result = {}
    for rec in rows:
        code = rec.get("SecuritiesCompanyCode", "").strip()
        if not code:
            continue
        result[code] = PriceInfo(
            ticker=code,
            name=rec.get("CompanyName", "").strip(),
            open=to_float(rec.get("Open")),
            high=to_float(rec.get("High")),
            low=to_float(rec.get("Low")),
            close=to_float(rec.get("Close")),
            change=to_float(rec.get("Change")),
            volume=to_int(rec.get("TradingShares")),
            date=_roc_to_iso(rec.get("Date", "")),
            market="TPEX",
            trading_value=to_float(rec.get("TransactionAmount")),
        )
    return result


# ---------- 上櫃股票估值指標（TPEX 官方，取代 FinMind 查詢） ----------
# tpex_mainboard_peratio_analysis 一次回傳全部上櫃股票的 PER／PBR／殖利率，不像
# FinMind 得每檔股票各打一次、還要算進 600 次／小時額度，所以整份快取在行程記憶體
# 裡（_tpex_valuation_cache），而不是像 FinMind 那樣以 (dataset, ticker) 為單位。

_tpex_valuation_cache = None  # (fetched_at: datetime, {ticker: {"date":, "per":, "pbr":, "dividend_yield":}})


def fetch_tpex_valuation_all():
    """打一次 tpex_mainboard_peratio_analysis，回傳 {ticker: {"date":, "per":,
    "pbr":, "dividend_yield":}}。查無獲利無法計算本益比的公司不會出現在回傳的
    資料裡（該公司的資料集本來就沒有這一列）。"""
    rows = http_get_json(TPEX_PERATIO_URL)
    result = {}
    for rec in rows:
        code = (rec.get("SecuritiesCompanyCode") or "").strip()
        if not code:
            continue
        result[code] = {
            "date": _roc_to_iso(rec.get("Date", "")),
            "per": to_float(rec.get("PriceEarningRatio")),
            "pbr": to_float(rec.get("PriceBookRatio")),
            "dividend_yield": to_float(rec.get("YieldRatio")),
        }
    return result


def get_tpex_valuation(ticker):
    """LIFO 風格的單檔查詢，但底層是整份市場快取：命中 30 分鐘內的快取就不重打
    API，快取過期時一次重抓全市場、之後這一輪的其他上櫃股票查詢都直接吃快取。
    查無資料（該股票不在 tpex_mainboard_peratio_analysis 裡）回傳 None。"""
    global _tpex_valuation_cache
    if _tpex_valuation_cache is not None:
        fetched_at, data = _tpex_valuation_cache
        if (datetime.now() - fetched_at).total_seconds() < TPEX_VALUATION_CACHE_TTL_SECONDS:
            return data.get(ticker)

    data = fetch_tpex_valuation_all()
    _tpex_valuation_cache = (datetime.now(), data)
    return data.get(ticker)


def get_market_snapshot(cache_path, force_refresh=False, on_progress=None):
    """on_progress(done, total, label) 在 force_refresh 真的重打 API 時，於每個階段
    開始前被呼叫一次（TWSE／TPEX 並行抓取、寫入快取共 2 個階段），讓呼叫端能畫出
    進度條；走快取路徑（未過期或 API 失敗回退）時不會呼叫，跟 backfill_twse_history
    的 on_progress 只在真的有動作時才觸發是一樣的原則。"""
    def _progress(done, total, label):
        if on_progress:
            on_progress(done, total, label)

    p = Path(cache_path)
    if not force_refresh and p.exists():
        with p.open("r", encoding="utf-8") as f:
            cached = json.load(f)
        fetched_at = datetime.fromisoformat(cached["fetched_at"])
        if (datetime.now() - fetched_at).total_seconds() < CACHE_TTL_SECONDS:
            return {
                code: PriceInfo(**info) for code, info in cached["prices"].items()
            }

    total_steps = 2
    try:
        _progress(0, total_steps, "正在取得台股報價（TWSE／TPEX）...")
        # TWSE／TPEX 是兩個獨立、互不相依的報價端點，並行送出可省下一次網路
        # 往返的等待時間（原本是先等 TWSE 回來才打 TPEX）。任一邊拋出的
        # PriceFetchError 會在 .result() 被重新拋出，走到下面既有的快取回退邏輯。
        with ThreadPoolExecutor(max_workers=2) as executor:
            twse_future = executor.submit(fetch_twse_all)
            tpex_future = executor.submit(fetch_tpex_all)
            twse_raw = twse_future.result()
            tpex = tpex_future.result()
        twse = _twse_all_with_staleness_fallback(twse_raw)
        _progress(1, total_steps, "正在寫入快取...")
    except PriceFetchError:
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                cached = json.load(f)
            return {code: PriceInfo(**info) for code, info in cached["prices"].items()}
        raise

    snapshot = {**twse, **tpex}
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "fetched_at": datetime.now().isoformat(),
                "prices": {code: asdict(info) for code, info in snapshot.items()},
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    _progress(2, total_steps, "快取寫入完成")
    return snapshot


def _previous_business_day(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d


# TWSE STOCK_DAY_ALL 正常會在收盤（13:30）後數小時內公佈當天資料；官方沒有
# SLA，這裡抓一個保守值。此時刻之前，今天的資料本來就可能還沒公佈，「最新僅到
# 昨天」是正常狀態；此時刻之後仍然只到昨天，才視為真的卡住。
TWSE_PUBLISH_CUTOFF_HOUR = 15


def _staleness_limit(today, max_lag_business_days, now=None):
    """回傳「可接受的最舊資料日期」。現在時刻若已過 TWSE_PUBLISH_CUTOFF_HOUR，
    今天的資料理論上早該公佈，不再給予 max_lag_business_days 的寬限（否則
    這個寬限會在收盤後的每一刻都掩蓋掉整整一個營業日的落後，等於永遠不會觸發
    ——這正是 8/4 晚上 STOCK_DAY_ALL 卡在前一天資料、卻沒被偵測到的成因）；
    未過此時刻則沿用原本「容忍 max_lag_business_days 個營業日」的寬限，涵蓋
    今天資料本來就還沒公佈的正常情況。"""
    now = now or datetime.now()
    if now.hour >= TWSE_PUBLISH_CUTOFF_HOUR:
        return today
    limit = today
    for _ in range(max_lag_business_days):
        limit = _previous_business_day(limit)
    return limit


def snapshot_staleness_warnings(snapshot, today=None, max_lag_business_days=1, now=None):
    """檢查 snapshot 裡每個市場（TWSE／TPEX 分開看，避免其中一邊卡住被另一邊
    的正常資料稀釋掉）目前最新的資料日期，是否落後超過可接受範圍（見
    `_staleness_limit`：白天容忍 max_lag_business_days 個營業日，過了官方通常
    公佈時間後不再容忍，例如 TWSE STOCK_DAY_ALL 卡住連續好幾天不更新，就是這次
    8/3 資料寫錯的起因）。只用「跳過六日」判斷營業日，不含國定假日行事曆，遇到
    連假可能誤報，可接受。回傳警告文字清單，沒有異常則回傳空 list。"""
    today = today or date.today()
    limit = _staleness_limit(today, max_lag_business_days, now=now)

    by_market = {}
    for p in snapshot.values():
        if p.date:
            by_market.setdefault(p.market, set()).add(p.date)

    warnings = []
    for market, dates in sorted(by_market.items()):
        latest = max(dates)
        try:
            latest_d = date.fromisoformat(latest)
        except ValueError:
            continue
        if latest_d < limit:
            warnings.append(
                f"{market} 資料可能落後：最新僅到 {latest}（今天 {today.isoformat()}）"
            )
    return warnings


def _twse_all_with_staleness_fallback(
    twse, max_lag_business_days=1, today=None, lookback_days=10, now=None
):
    """twse 是 fetch_twse_all()（STOCK_DAY_ALL）的回傳值。若最新資料日期落後超過
    可接受範圍（跟 snapshot_staleness_warnings 用同一套 `_staleness_limit` 門檻：
    白天容忍 max_lag_business_days 個營業日，過了官方通常公佈時間後不再容忍），
    改用 MI_INDEX（tradingnote_history.fetch_twse_historical_day，backfill 歷史
    回補用的同一個端點，已實測比 STOCK_DAY_ALL 更新得快）往回找最近一個有資料的
    交易日取代，避免「現價」長期卡在舊資料（就是 2026-08-03 那次事故的成因：
    STOCK_DAY_ALL 卡住不更新，但 MI_INDEX 當時已經有正確的當天資料；8/4 晚上又
    卡了一次，才發現原本的門檻在收盤後永遠不會觸發，見 `_staleness_limit`）。
    這裡用函式內的延後 import，避免跟會 import 這個模組的 tradingnote_history
    在載入階段形成循環 import；只有真的判定落後太久時才會用到。找不到更新資料
    就原樣回傳 twse（不會比原本更差）。"""
    if not twse:
        return twse
    today = today or date.today()
    limit = _staleness_limit(today, max_lag_business_days, now=now)

    dates = {p.date for p in twse.values() if p.date}
    if not dates:
        return twse
    try:
        latest_d = date.fromisoformat(max(dates))
    except ValueError:
        return twse
    if latest_d >= limit:
        return twse

    from tradingnote_history import fetch_twse_historical_day

    cursor = today
    for _ in range(lookback_days):
        records = fetch_twse_historical_day(cursor.strftime("%Y%m%d"))
        if records:
            return {
                r["ticker"]: PriceInfo(
                    ticker=r["ticker"],
                    name=r["name"],
                    open=r.get("open"),
                    high=r.get("high"),
                    low=r.get("low"),
                    close=r["close"],
                    change=r.get("change"),
                    volume=r["volume"],
                    date=cursor.isoformat(),
                    market="TWSE",
                    trading_value=r["trading_value"],
                )
                for r in records
            }
        cursor -= timedelta(days=1)
    return twse


def lookup_price(ticker, snapshot):
    return snapshot.get(ticker.strip().upper())


# ---------- 分析：損益計算 ----------

def compute_pnl(position, price):
    if price is None or price.close is None:
        return None
    current_value = position.shares * price.close
    cost_basis = position.shares * position.entry_price
    unrealized_pnl = current_value - cost_basis
    pnl_pct = (price.close - position.entry_price) / position.entry_price * 100
    return PnLResult(
        current_price=price.close,
        current_value=current_value,
        cost_basis=cost_basis,
        unrealized_pnl=unrealized_pnl,
        pnl_pct=pnl_pct,
    )
