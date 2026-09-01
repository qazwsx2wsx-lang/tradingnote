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
import os
import time
import threading
import uuid
from datetime import datetime
from pathlib import Path


_FILE_LOCKS = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _file_lock(path):
    key = str(Path(path).resolve())
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(key, threading.RLock())


def _atomic_json_write(path, payload):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    temporary = p.with_name(f".{p.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, p)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def load_fresh_file_cache(cache_path, ttl_seconds):
    """讀 cache_path 的 JSON 快取檔（格式須含 "fetched_at" 欄位）。檔案不存在
    或已超過 ttl_seconds 回傳 None（呼叫端應重新抓取，成功後用 write_file_cache
    寫回）；否則回傳整份內容（含 fetched_at，呼叫端自行取用其餘欄位）。"""
    p = Path(cache_path)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            cached = json.load(f)
        if not isinstance(cached, dict):
            return None
        fetched_at = datetime.fromisoformat(cached["fetched_at"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if (datetime.now() - fetched_at).total_seconds() < ttl_seconds:
        return cached
    return None


def load_stale_file_cache(cache_path):
    """不管新鮮度，檔案存在就讀回來；不存在回傳 None。給重抓 API 失敗時的
    fallback 用（寧可回傳舊資料也不要整個噴錯）。"""
    p = Path(cache_path)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            cached = json.load(f)
        return cached if isinstance(cached, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def write_file_cache(cache_path, payload):
    """payload 是除了 fetched_at 以外要存的內容（例如 {"prices": {...}} 或
    {"rows": [...]}），寫入時自動補上目前時間當 fetched_at。"""
    p = Path(cache_path)
    with _file_lock(p):
        _atomic_json_write(
            p,
            {"fetched_at": datetime.now().isoformat(), **payload},
        )


def load_keyed_store(store_path):
    """讀整份「以 key 分開存放、永久保留」的 JSON 檔（例如逐股票各自一筆記錄，
    跟上面 load_fresh_file_cache／write_file_cache 假設整份檔案只有一個
    fetched_at、命中一次全部更新的用途不同）。檔案不存在回傳空 dict；不判斷
    新鮮度——是否重新查詢、要不要覆寫某個 key 由呼叫端決定，這裡只負責讀寫。"""
    p = Path(store_path)
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            store = json.load(f)
        return store if isinstance(store, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_keyed_entry(store_path, key, payload):
    """更新 store_path 這份 keyed JSON 檔裡 key 對應的一筆記錄（其餘 key 不動、
    不會被覆寫掉），payload 是要存的內容，自動補上目前時間當 fetched_at。
    每次呼叫都整份讀回、改一筆、整份寫回；呼叫頻率低（例如使用者選取某筆
    部位才查一次）不需要更精細的鎖定或部分寫入機制。"""
    p = Path(store_path)
    with _file_lock(p):
        store = load_keyed_store(p)
        store[key] = {"fetched_at": datetime.now().isoformat(), **payload}
        _atomic_json_write(p, store)


class TTLCache:
    """行程內記憶體 TTL 快取。key 可以是任意 hashable（只需要單一份快取的
    情境固定用 None 當 key，例如整份市場一起快取的資料；需要依查詢條件分開
    快取的情境用 tuple 之類的複合 key，例如 (dataset, ticker, lookback_days)）。
    get_or_fetch 命中未過期快取就直接回傳；否則呼叫 fetch_fn() 取得新值、存入
    快取後回傳；同一個 key 同時只有一個 fetch_fn() 會實際執行。每次存取也會
    清理已過期項目，避免長時間執行時舊快取無限累積。fetch_fn() 拋出例外時不會
    被快取，例外直接往上拋，下次呼叫照樣重試（只快取成功的結果）。"""

    def __init__(self, ttl_seconds):
        self.ttl_seconds = ttl_seconds
        self._store = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._inflight = set()

    def _remove_expired_locked(self, now):
        """刪除已過期項目；呼叫端必須持有 self._lock。"""
        expired_keys = [
            key
            for key, (fetched_at, _value) in self._store.items()
            if now - fetched_at >= self.ttl_seconds
        ]
        for key in expired_keys:
            self._store.pop(key, None)

    def get_or_fetch(self, key, fetch_fn):
        # 同一個 key 若已經有背景查詢進行中，等待它完成後重試命中快取，
        # 避免使用者連點同一檔股票時重複消耗 API 額度。
        while True:
            with self._condition:
                now = time.monotonic()
                self._remove_expired_locked(now)
                cached = self._store.get(key)
                if cached is not None:
                    return cached[1]
                if key not in self._inflight:
                    self._inflight.add(key)
                    break
                self._condition.wait()

        try:
            value = fetch_fn()
        except Exception:
            with self._condition:
                self._inflight.discard(key)
                self._condition.notify_all()
            raise

        with self._condition:
            self._store[key] = (time.monotonic(), value)
            self._inflight.discard(key)
            self._condition.notify_all()
        return value
