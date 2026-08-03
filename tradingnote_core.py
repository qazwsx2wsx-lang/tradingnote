#!/usr/bin/env python3
"""tradingnote 核心模組 - 部位紀錄、台股價格查詢、損益分析（不依賴任何介面）"""

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

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


class PriceFetchError(Exception):
    pass


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

def _http_get_json(url):
    try:
        with urlopen(url, timeout=10) as resp:
            data = json.load(resp)
    except URLError as e:
        raise PriceFetchError(f"連線失敗：{e}") from e
    except json.JSONDecodeError as e:
        raise PriceFetchError(f"回傳資料格式錯誤：{e}") from e
    return data


def _roc_to_iso(roc_date):
    roc_date = str(roc_date).strip()
    if len(roc_date) < 6:
        return ""
    year = int(roc_date[:-4]) + 1911
    month = roc_date[-4:-2]
    day = roc_date[-2:]
    return f"{year:04d}-{month}-{day}"


def _to_float(value):
    if value is None:
        return None
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return None


def _to_int(value):
    f = _to_float(value)
    return int(f) if f is not None else None


def fetch_twse_all():
    rows = _http_get_json(TWSE_URL)
    result = {}
    for rec in rows:
        code = rec.get("Code", "").strip()
        if not code:
            continue
        result[code] = PriceInfo(
            ticker=code,
            name=rec.get("Name", "").strip(),
            open=_to_float(rec.get("OpeningPrice")),
            high=_to_float(rec.get("HighestPrice")),
            low=_to_float(rec.get("LowestPrice")),
            close=_to_float(rec.get("ClosingPrice")),
            change=_to_float(rec.get("Change")),
            volume=_to_int(rec.get("TradeVolume")),
            date=_roc_to_iso(rec.get("Date", "")),
            market="TWSE",
            trading_value=_to_float(rec.get("TradeValue")),
        )
    return result


def fetch_tpex_all():
    rows = _http_get_json(TPEX_URL)
    result = {}
    for rec in rows:
        code = rec.get("SecuritiesCompanyCode", "").strip()
        if not code:
            continue
        result[code] = PriceInfo(
            ticker=code,
            name=rec.get("CompanyName", "").strip(),
            open=_to_float(rec.get("Open")),
            high=_to_float(rec.get("High")),
            low=_to_float(rec.get("Low")),
            close=_to_float(rec.get("Close")),
            change=_to_float(rec.get("Change")),
            volume=_to_int(rec.get("TradingShares")),
            date=_roc_to_iso(rec.get("Date", "")),
            market="TPEX",
            trading_value=_to_float(rec.get("TransactionAmount")),
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
    rows = _http_get_json(TPEX_PERATIO_URL)
    result = {}
    for rec in rows:
        code = (rec.get("SecuritiesCompanyCode") or "").strip()
        if not code:
            continue
        result[code] = {
            "date": _roc_to_iso(rec.get("Date", "")),
            "per": _to_float(rec.get("PriceEarningRatio")),
            "pbr": _to_float(rec.get("PriceBookRatio")),
            "dividend_yield": _to_float(rec.get("YieldRatio")),
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


def get_market_snapshot(cache_path, force_refresh=False):
    p = Path(cache_path)
    if not force_refresh and p.exists():
        with p.open("r", encoding="utf-8") as f:
            cached = json.load(f)
        fetched_at = datetime.fromisoformat(cached["fetched_at"])
        if (datetime.now() - fetched_at).total_seconds() < CACHE_TTL_SECONDS:
            return {
                code: PriceInfo(**info) for code, info in cached["prices"].items()
            }

    try:
        twse = fetch_twse_all()
        tpex = fetch_tpex_all()
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
    return snapshot


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
