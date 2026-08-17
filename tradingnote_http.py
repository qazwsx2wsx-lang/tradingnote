#!/usr/bin/env python3
"""tradingnote - 共用 HTTP／數值解析工具，供所有查價／查詢模組使用。

抽出自 tradingnote_core.py：原本 tradingnote_history.py／tradingnote_finmind.py
以「借用底線私有函式」的方式共用這幾個函式，tradingnote_taifex.py 則乾脆自己
重寫了一份（且與原版的空值判斷邏輯已經分岔）。統一放在這裡當作真正的公開共用
工具，各模組（含 tradingnote_core.py 自己）一律從這裡 import，不再各自持有
或重寫一份。
"""

import gzip
import http.client
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class PriceFetchError(Exception):
    pass


def http_get_json(url, timeout=10):
    """帶 `Accept-Encoding: gzip` 主動要求伺服器壓縮回應——TWSE/TPEX 全市場報價這類
    上千筆股票的 JSON 回應未壓縮時傳輸量大上數倍，壓縮可明顯縮短啟動時的網路等待。
    伺服器不支援時就是原樣回傳未壓縮內容（沒有 Content-Encoding: gzip header 就不
    解壓），行為與改動前相同。"""
    try:
        req = Request(url, headers={"Accept-Encoding": "gzip"})
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return json.loads(data)
    except HTTPError as e:
        raise PriceFetchError(f"連線失敗（HTTP {e.code}）：{e}") from e
    except URLError as e:
        raise PriceFetchError(f"連線失敗：{e}") from e
    except TimeoutError as e:
        # resp.read()／gzip.decompress 階段的逾時不會被 urlopen 包成 URLError（那只包
        # 連線建立階段的逾時），必須另外接住，否則會是未捕捉例外往上炸穿呼叫端。
        raise PriceFetchError(f"連線逾時：{e}") from e
    except http.client.HTTPException as e:
        # 例如 IncompleteRead（伺服器提早斷線、回應被截斷）：http.client.HTTPException
        # 不是 OSError 的子類別，不會被下面的 except 接住，若不特別攔截，會在背景執行緒
        # 內變成未捕捉例外，導致呼叫端（如啟動 preload 執行緒）收不到 "error" 訊息、
        # 進度條卡住不動、window 永遠不會顯示。
        raise PriceFetchError(f"連線失敗（回應中斷）：{e}") from e
    except (json.JSONDecodeError, OSError) as e:
        raise PriceFetchError(f"回傳資料格式錯誤：{e}") from e


def http_post_text(url, data, encoding="utf-8", timeout=30):
    """POST 一個 application/x-www-form-urlencoded 表單，回傳解碼後的文字（非 JSON）。
    給 TAIFEX 網站的歷史 CSV 下載端點（Big5 編碼，需帶 User-Agent 才不會被擋）用；
    例外包裝跟 http_get_json 一致，讓呼叫端統一接 PriceFetchError。data 可以是已
    urlencode 的字串或 bytes。"""
    body = data.encode("ascii") if isinstance(data, str) else data
    try:
        req = Request(
            url,
            data=body,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"},
        )
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode(encoding, "ignore")
    except HTTPError as e:
        raise PriceFetchError(f"連線失敗（HTTP {e.code}）：{e}") from e
    except URLError as e:
        raise PriceFetchError(f"連線失敗：{e}") from e
    except TimeoutError as e:
        raise PriceFetchError(f"連線逾時：{e}") from e
    except http.client.HTTPException as e:
        raise PriceFetchError(f"連線失敗（回應中斷）：{e}") from e
    except OSError as e:
        raise PriceFetchError(f"回傳資料格式錯誤：{e}") from e


def to_float(value):
    if value in (None, "", "-", "NULL"):
        return None
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return None


def to_int(value):
    f = to_float(value)
    return int(f) if f is not None else None
