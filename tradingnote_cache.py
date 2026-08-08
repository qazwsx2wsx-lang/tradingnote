"""tradingnote - 共用快取工具：檔案（JSON）TTL 快取與行程內記憶體 TTL 快取。

抽出自 tradingnote_core.py（get_market_snapshot 的 price_cache.json）與
tradingnote_taifex.py（get_cached_daily_futures_report 的 futures_cache.json）
——兩者原本各自重寫一份「讀檔案→檢查 fetched_at 是否在 TTL 內→過期或不存在就
重抓→寫回檔案，API 失敗時退回舊快取」的邏輯，只有 payload 的 key 名稱
（"prices" vs "rows"）不同；以及 tradingnote_core.py（_tpex_valuation_cache）
與 tradingnote_finmind.py（_dataset_cache）各自重寫一份「(fetched_at, value)
命中未過期直接回傳、否則重新呼叫並存入」的行程內記憶體快取，只有 key 是固定
單一值還是 (dataset, ticker, lookback_days) 這種複合 key 的差別。統一放在這裡
當作真正的共用工具，各模組一律從這裡 import，不再各自持有或重寫一份。
"""

import json
import time
from datetime import datetime
from pathlib import Path


def load_fresh_file_cache(cache_path, ttl_seconds):
    """讀 cache_path 的 JSON 快取檔（格式須含 "fetched_at" 欄位）。檔案不存在
    或已超過 ttl_seconds 回傳 None（呼叫端應重新抓取，成功後用 write_file_cache
    寫回）；否則回傳整份內容（含 fetched_at，呼叫端自行取用其餘欄位）。"""
    p = Path(cache_path)
    if not p.exists():
        return None
    with p.open("r", encoding="utf-8") as f:
        cached = json.load(f)
    fetched_at = datetime.fromisoformat(cached["fetched_at"])
    if (datetime.now() - fetched_at).total_seconds() < ttl_seconds:
        return cached
    return None


def load_stale_file_cache(cache_path):
    """不管新鮮度，檔案存在就讀回來；不存在回傳 None。給重抓 API 失敗時的
    fallback 用（寧可回傳舊資料也不要整個噴錯）。"""
    p = Path(cache_path)
    if not p.exists():
        return None
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_file_cache(cache_path, payload):
    """payload 是除了 fetched_at 以外要存的內容（例如 {"prices": {...}} 或
    {"rows": [...]}），寫入時自動補上目前時間當 fetched_at。"""
    p = Path(cache_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(
            {"fetched_at": datetime.now().isoformat(), **payload},
            f,
            ensure_ascii=False,
            indent=2,
        )


class TTLCache:
    """行程內記憶體 TTL 快取。key 可以是任意 hashable（只需要單一份快取的
    情境固定用 None 當 key，例如整份市場一起快取的資料；需要依查詢條件分開
    快取的情境用 tuple 之類的複合 key，例如 (dataset, ticker, lookback_days)）。
    get_or_fetch 命中未過期快取就直接回傳；否則呼叫 fetch_fn() 取得新值、存入
    快取後回傳。fetch_fn() 拋出例外時不會被快取，例外直接往上拋，下次呼叫
    照樣重試（只快取成功的結果）。"""

    def __init__(self, ttl_seconds):
        self.ttl_seconds = ttl_seconds
        self._store = {}

    def get_or_fetch(self, key, fetch_fn):
        cached = self._store.get(key)
        if cached is not None:
            fetched_at, value = cached
            if time.time() - fetched_at < self.ttl_seconds:
                return value
        value = fetch_fn()
        self._store[key] = (time.time(), value)
        return value
