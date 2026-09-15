"""TWSE／TPEx 全市場三大法人買賣超與族群聚合。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
import threading

from tradingnote_api_config import TPEX_INSTITUTIONAL_URL, TWSE_INSTITUTIONAL_URL
from tradingnote_cache import fetch_with_file_cache, load_stale_file_cache
from tradingnote_http import PriceFetchError, http_get_json, to_int


INSTITUTIONAL_CACHE_TTL_SECONDS = 30 * 60
_loaded_file_cache = {}
_loaded_file_cache_lock = threading.Lock()


@dataclass(frozen=True)
class InstitutionalTickerFlow:
    ticker: str
    name: str
    date: str
    market: str
    foreign_net: int
    trust_net: int
    dealer_net: int


@dataclass(frozen=True)
class InstitutionalGroupFlow:
    group: str
    foreign_value: float
    trust_value: float
    dealer_value: float
    covered_stocks: int
    date: str

    @property
    def total_value(self):
        return self.foreign_value + self.trust_value + self.dealer_value


def _http_get_json_retry(url):
    """交易所大型 JSON 偶爾提早斷線；只在本資料集立即重試一次。"""
    try:
        return http_get_json(url, timeout=20)
    except PriceFetchError:
        return http_get_json(url, timeout=30)


def _roc_compact_to_iso(value):
    text = str(value or "").strip()
    if len(text) != 7 or not text.isdigit():
        return ""
    return f"{int(text[:3]) + 1911:04d}-{text[3:5]}-{text[5:7]}"


def fetch_twse_institutional():
    """取得最新上市股票三大法人日報；外資含外資自營商。"""
    payload = _http_get_json_retry(TWSE_INSTITUTIONAL_URL)
    if payload.get("stat") != "OK":
        raise PriceFetchError(f"TWSE 三大法人資料不可用：{payload.get('stat', '未知狀態')}")
    date_text = str(payload.get("date") or "")
    data_date = (
        f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:8]}"
        if len(date_text) == 8
        else ""
    )
    result = []
    for row in payload.get("data", []):
        if len(row) < 19:
            continue
        ticker = str(row[0]).strip()
        if not ticker:
            continue
        result.append(
            InstitutionalTickerFlow(
                ticker=ticker,
                name=str(row[1]).strip(),
                date=data_date,
                market="TWSE",
                foreign_net=(to_int(row[4]) or 0) + (to_int(row[7]) or 0),
                trust_net=to_int(row[10]) or 0,
                dealer_net=to_int(row[11]) or 0,
            )
        )
    return result


def _first_int(row, *keys):
    for key in keys:
        if key in row:
            return to_int(row.get(key)) or 0
    return 0


def fetch_tpex_institutional():
    """取得最新上櫃股票三大法人日報。"""
    rows = _http_get_json_retry(TPEX_INSTITUTIONAL_URL)
    result = []
    for row in rows:
        ticker = str(row.get("SecuritiesCompanyCode") or "").strip()
        if not ticker:
            continue
        result.append(
            InstitutionalTickerFlow(
                ticker=ticker,
                name=str(row.get("CompanyName") or "").strip(),
                date=_roc_compact_to_iso(row.get("Date")),
                market="TPEX",
                foreign_net=_first_int(
                    row,
                    "ForeignInvestorsInclude MainlandAreaInvestors-Difference",
                    "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference",
                ),
                trust_net=_first_int(
                    row, "SecuritiesInvestmentTrustCompanies-Difference"
                ),
                dealer_net=_first_int(row, "Dealers-Difference"),
            )
        )
    return result


def _fetch_institutional_snapshot():
    with ThreadPoolExecutor(max_workers=2) as executor:
        twse_future = executor.submit(fetch_twse_institutional)
        tpex_future = executor.submit(fetch_tpex_institutional)
        return twse_future.result() + tpex_future.result()


def get_cached_institutional_snapshot(cache_path, force_refresh=False):
    """取得全市場法人資料；失敗時回退舊快取，避免阻斷行情更新。"""
    return fetch_with_file_cache(
        cache_path, INSTITUTIONAL_CACHE_TTL_SECONDS, _fetch_institutional_snapshot,
        force_refresh=force_refresh,
        serialize=lambda rows: {"rows": [asdict(row) for row in rows]},
        deserialize=lambda cached: [
            InstitutionalTickerFlow(**row) for row in cached.get("rows", [])
        ],
    )


def load_cached_institutional_snapshot(cache_path):
    """只讀現有快取，不連網；供 GUI 重畫時使用。"""
    path = Path(cache_path)
    try:
        signature = (path.stat().st_mtime_ns, path.stat().st_size)
    except OSError:
        signature = None
    with _loaded_file_cache_lock:
        loaded = _loaded_file_cache.get(str(path.resolve()))
        if loaded is not None and loaded[0] == signature:
            return loaded[1]
    cached = load_stale_file_cache(cache_path)
    if cached is None:
        return []
    rows = [InstitutionalTickerFlow(**row) for row in cached.get("rows", [])]
    with _loaded_file_cache_lock:
        _loaded_file_cache[str(path.resolve())] = (signature, rows)
    return rows


def aggregate_institutional_by_group(rows, groups, snapshot):
    """以實際淨買賣股數乘當日收盤價，估算各法人在每個族群的淨額。"""
    by_ticker = {row.ticker: row for row in rows}
    dates = [row.date for row in rows if row.date]
    data_date = max(dates, default="")
    result = []
    for group, members in groups.items():
        foreign_value = trust_value = dealer_value = 0.0
        covered = 0
        for ticker in members:
            row = by_ticker.get(ticker)
            price = snapshot.get(ticker)
            if row is None or price is None or price.close is None or row.date != price.date or row.date != data_date:
                continue
            close = float(price.close)
            foreign_value += row.foreign_net * close
            trust_value += row.trust_net * close
            dealer_value += row.dealer_net * close
            covered += 1
        if covered:
            result.append(
                InstitutionalGroupFlow(
                    group=group,
                    foreign_value=foreign_value,
                    trust_value=trust_value,
                    dealer_value=dealer_value,
                    covered_stocks=covered,
                    date=data_date,
                )
            )
    return result
