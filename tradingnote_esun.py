"""玉山證券 Market Data SDK 即時報價（不依賴 Qt，SDK 以注入方式取得，方便測試）。

來源是 ~/Documents/Vibecoding/esun/quote_cli.py 的 LiveBoard，搬進來後拆成：
- 純函式：設定檢查／組 ConfigParser、報價欄位整理、日盤夜盤時段判斷。
- QuoteSession：管理監控清單、WebSocket 推播（日盤／股票）與期貨夜盤 REST 輪詢，
  更新時呼叫 on_update(symbol)；不印任何東西，由 GUI 自己畫。

`esun_marketdata` 是玉山提供的 wheel（不在 PyPI），沒裝時 load_sdk_class() 回傳 None，
GUI 顯示「尚未安裝」而不是讓整個 app 起不來。所有網路呼叫都會阻塞，呼叫端必須放背景執行緒。
"""

from __future__ import annotations

import json
import threading
from configparser import ConfigParser
from datetime import datetime, time as dtime

DEFAULT_ENTRY = "https://esuntradingapi-simulation.esunsec.com.tw/api/v1"
DEFAULT_ENVIRONMENT = "SIMULATION"
AFTERHOURS_POLL_SECONDS = 5

DAY_SESSION = (dtime(8, 45), dtime(13, 45))
# 9:00~15:00：夜盤已收盤且日盤尚在交易，不顯示夜盤那一行
NIGHT_HIDE_WINDOW = (dtime(9, 0), dtime(15, 0))

KIND_FUTOPT = "futopt"
KIND_STOCK = "stock"

# settings.json 內的欄位名稱（api_key／secret 以明文存放，與 finmind_token 相同）
SETTING_KEYS = (
    "esun_api_key",
    "esun_api_secret",
    "esun_cert_path",
    "esun_account",
    "esun_environment",
    "esun_entry",
)


def esun_configured(settings):
    """金鑰、密鑰、憑證路徑都填了才算設定完成（帳號對報價不是必要）。"""
    return all(
        str(settings.get(key, "")).strip()
        for key in ("esun_api_key", "esun_api_secret", "esun_cert_path")
    )


def build_config(settings):
    """由 settings 組出 SDK 要的 ConfigParser（欄位對應 config.simulation.ini）。"""
    config = ConfigParser()
    config["Core"] = {
        "Entry": str(settings.get("esun_entry") or DEFAULT_ENTRY).strip(),
        "Environment": str(settings.get("esun_environment") or DEFAULT_ENVIRONMENT).strip(),
    }
    config["Cert"] = {"Path": str(settings.get("esun_cert_path", "")).strip()}
    config["Api"] = {
        "Key": str(settings.get("esun_api_key", "")).strip(),
        "Secret": str(settings.get("esun_api_secret", "")).strip(),
    }
    config["User"] = {"Account": str(settings.get("esun_account", "")).strip()}
    return config


def load_sdk_class():
    """回傳 esun_marketdata.sdk.SDK；沒安裝 wheel 時回傳 None。"""
    try:
        from esun_marketdata.sdk import SDK
    except ImportError:
        return None
    return SDK


def day_session_status(now=None):
    now = (now or datetime.now()).time()
    start, end = DAY_SESSION
    return "交易中" if start <= now <= end else "已收盤"


def should_show_night_session(now=None):
    now = (now or datetime.now()).time()
    hide_start, hide_end = NIGHT_HIDE_WINDOW
    return not (hide_start <= now < hide_end)


def quote_fields(data):
    """把 SDK 回傳的報價 dict 整理成固定欄位；沒資料或查詢失敗時 ok=False。"""
    if not data:
        return {"ok": False, "message": "尚無資料"}
    if data.get("statusCode"):
        return {"ok": False, "message": f"查詢失敗：{data.get('message')}"}
    total = data.get("total") or {}
    return {
        "ok": True,
        "symbol": data.get("symbol"),
        "name": data.get("name") or data.get("symbol"),
        "last": data.get("lastPrice", data.get("closePrice")),
        "change": data.get("change"),
        "pct": data.get("changePercent"),
        "volume": total.get("tradeVolume", data.get("tradeVolume")),
    }


class QuoteSession:
    """監控清單＋即時資料。state[symbol] = {'kind', 'day', 'ah'}。

    執行緒：WebSocket 回呼與輪詢執行緒都會呼叫 on_update，GUI 端要自己轉回 Qt 主執行緒。
    add／remove／connect 會做網路呼叫（阻塞），請在背景執行緒呼叫。"""

    def __init__(self, sdk, on_update=None):
        self.sdk = sdk
        self.on_update = on_update or (lambda symbol: None)
        self.state = {}
        self.lock = threading.Lock()
        self._ws_clients = {}
        self._poll_stop = threading.Event()
        self._poll_thread = None
        self._tickers_cache = {}

    # ---- 查詢 ----

    def _get_tickers(self, ftype):
        if ftype not in self._tickers_cache:
            res = self.sdk.rest_client.futopt.intraday.tickers(type=ftype)
            self._tickers_cache[ftype] = res.get("data", [])
        return self._tickers_cache[ftype]

    def search_keyword(self, keyword):
        """依名稱關鍵字搜尋期貨／選擇權代碼，回傳 [(symbol, name, type, endDate)]，依到期日排序。"""
        results = []
        for ftype in ("FUTURE", "OPTION"):
            for item in self._get_tickers(ftype):
                if keyword in item.get("name", ""):
                    results.append(
                        (item.get("symbol"), item.get("name"), ftype, item.get("endDate", ""))
                    )
        results.sort(key=lambda row: row[3])
        return results

    def classify_symbol(self, symbol):
        """'futopt'、'stock'，兩邊都查無回傳 None。"""
        if not self.sdk.rest_client.futopt.intraday.quote(symbol=symbol).get("statusCode"):
            return KIND_FUTOPT
        if not self.sdk.rest_client.stock.intraday.quote(symbol=symbol).get("statusCode"):
            return KIND_STOCK
        return None

    # ---- 監控清單 ----

    def _get_ws(self, kind):
        if kind not in self._ws_clients:
            ws = self.sdk.websocket_client
            client = ws.futopt if kind == KIND_FUTOPT else ws.stock
            client.on("message", self._on_message)
            client.connect()
            self._ws_clients[kind] = client
        return self._ws_clients[kind]

    def _on_message(self, raw):
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if msg.get("event") not in ("snapshot", "aggregates"):
            return
        data = msg.get("data") or {}
        symbol = data.get("symbol")
        if not symbol or "lastPrice" not in data:
            return
        with self.lock:
            entry = self.state.get(symbol)
            if entry is None:
                return
            entry["day"] = data
        self.on_update(symbol)

    def add(self, symbol, kind=None):
        """加入監控。kind 已知（例如部位的股票）可省下分類用的 REST 查詢。
        回傳 (kind, None) 成功／已在清單，(None, 原因) 失敗。"""
        with self.lock:
            if symbol in self.state:
                return self.state[symbol]["kind"], None
        kind = kind or self.classify_symbol(symbol)
        if kind is None:
            return None, f"查無代碼「{symbol}」（期貨／選擇權、股票皆查無）"

        with self.lock:
            self.state[symbol] = {"kind": kind, "day": None, "ah": None}
        self._get_ws(kind).subscribe({"channel": "aggregates", "symbol": symbol})

        if kind == KIND_FUTOPT:
            ah = self.sdk.rest_client.futopt.intraday.quote(symbol=symbol, session="AFTERHOURS")
            with self.lock:
                if symbol in self.state:
                    self.state[symbol]["ah"] = ah
            self._ensure_poll_running()
        self.on_update(symbol)
        return kind, None

    def remove(self, symbol):
        with self.lock:
            entry = self.state.pop(symbol, None)
        if entry is None:
            return False
        client = self._ws_clients.get(entry["kind"])
        if client is not None:
            client.unsubscribe({"channel": "aggregates", "symbol": symbol})
        return True

    # ---- 夜盤輪詢 ----

    def _ensure_poll_running(self):
        if self._poll_thread is None or not self._poll_thread.is_alive():
            self._poll_stop.clear()
            self._poll_thread = threading.Thread(target=self._poll_afterhours, daemon=True)
            self._poll_thread.start()

    def _poll_afterhours(self):
        while not self._poll_stop.is_set():
            with self.lock:
                symbols = [s for s, e in self.state.items() if e["kind"] == KIND_FUTOPT]
            if not symbols:
                return
            for symbol in symbols:
                try:
                    ah = self.sdk.rest_client.futopt.intraday.quote(
                        symbol=symbol, session="AFTERHOURS"
                    )
                except Exception:
                    continue  # 單次輪詢失敗（斷線等）不中斷，下一輪再試
                with self.lock:
                    if symbol in self.state:
                        self.state[symbol]["ah"] = ah
                self.on_update(symbol)
            self._poll_stop.wait(AFTERHOURS_POLL_SECONDS)

    # ---- 讀取 ----

    def snapshot(self):
        with self.lock:
            return {symbol: dict(entry) for symbol, entry in self.state.items()}

    def last_price(self, symbol):
        """日盤／股票的最新價（float）；沒有資料回傳 None。"""
        with self.lock:
            entry = self.state.get(symbol)
            day = entry["day"] if entry else None
        fields = quote_fields(day)
        if not fields["ok"]:
            return None
        try:
            return float(fields["last"])
        except (TypeError, ValueError):
            return None

    def stop(self):
        self._poll_stop.set()
        for client in self._ws_clients.values():
            try:
                client.disconnect()
            except Exception:
                pass
        self._ws_clients.clear()
        with self.lock:
            self.state.clear()


def connect_session(settings, on_update=None, sdk_class=None):
    """登入並回傳 QuoteSession。沒裝 SDK 或設定不全會丟 RuntimeError（訊息可直接顯示給使用者）。"""
    sdk_class = sdk_class or load_sdk_class()
    if sdk_class is None:
        raise RuntimeError(
            "尚未安裝玉山 Market Data SDK（esun_marketdata wheel），請先 pip install 玉山提供的 .whl。"
        )
    if not esun_configured(settings):
        raise RuntimeError("請先在「設定」分頁填入玉山 API 金鑰、密鑰與憑證路徑。")
    sdk = sdk_class(build_config(settings))
    sdk.login()
    return QuoteSession(sdk, on_update=on_update)
