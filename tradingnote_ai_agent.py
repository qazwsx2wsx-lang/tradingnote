"""tradingnote - 「AI 助理」模組：串接 Gemini API（google-genai 自動函式呼叫），
讓使用者用自然語言問問題，由模型自行判斷要呼叫哪個查詢工具（股票代號查詢／
股價／本益比／三大法人買賣超），再組出回答。跟 tradingnote_finmind.py 一樣
不依賴任何介面，CLI／GUI 都可以各自 import。

每次對話都會打 Gemini API（要網路，免費額度用完後要收費），底層資料查詢仍是
同一批 FinMind／歷史資料庫函式，所以（上市股票的部分）也會計入 tradingnote_finmind
的每小時用量統計；上櫃股票查本益比／殖利率改走 TPEX 官方端點，不計入這個統計。
"""

import time

from google import genai
from google.genai import errors, types

from tradingnote_finmind import fetch_institutional_investors, fetch_valuation
from tradingnote_history import get_latest_ticker_record

# "-latest" 是滾動別名，永遠指向目前的穩定版模型，避免像 gemini-2.5-flash
# 這種固定版號哪天被 Google 收掉、新用戶打不到而整支功能掛掉。
# 用 flash-lite 而非一般 flash：免費額度的每分鐘／每日請求數上限較高，
# 減少查詢時撞到 429（RESOURCE_EXHAUSTED）的機率。
MODEL = "gemini-flash-lite-latest"

# Google 未保證這是固定值（依帳號使用層級、模型調整），這裡只是給「AI 助理」
# 頁一個粗略提醒用的參考值；實際額度以 Google AI Studio 主控台顯示的為準。
GEMINI_RPM_HINT = 15

# 每次呼叫 chat.send_message（使用者送出一則訊息）的時間戳記，只存在記憶體中，
# 重啟程式會歸零，不代表 Gemini 帳號其他來源的真實用量。跟 tradingnote_finmind
# 的 _call_timestamps 是同一套做法，但這裡算的是「使用者訊息數」而非「實際打
# 出去的 API 次數」：google-genai 自動函式呼叫時，模型可能為了呼叫一個工具再
# 決定回答，一次 send_message 底層可能觸發不只一次 API 往返，所以實際額度消耗
# 可能比這個數字略高。
_call_timestamps = []


def get_call_count(window_seconds=60):
    """回傳過去 window_seconds 秒內（預設 60 秒，對齊「每分鐘請求數」額度）
    呼叫過幾次 Gemini API；順便把過期的時間戳記清掉，避免無限累積。"""
    global _call_timestamps
    cutoff = time.time() - window_seconds
    _call_timestamps = [t for t in _call_timestamps if t >= cutoff]
    return len(_call_timestamps)

SYSTEM_PROMPT = (
    "你是 TradingNote 記帳程式裡的股市問答助理，協助使用者用自然語言查詢台股資訊。"
    "使用者提到公司名稱而非代號時，先用 search_ticker 工具查出代號，"
    "確認唯一或最可能的一檔後再查其他資料；如果有多檔符合，列出來請使用者確認。"
    "回答一律使用繁體中文，直接列出關鍵數字，不需要免責聲明或客套話。"
    "工具查無資料時，如實告知查無資料，不要編造數字。"
)


class AgentError(Exception):
    """呼叫 Gemini API 或工具本身失敗時丟出，訊息可直接顯示給使用者。"""


def _build_tools(directory, history_db_path, finmind_token):
    """回傳一般 Python function（不是裝飾過的物件），google-genai 的自動函式
    呼叫會直接讀 type hint／docstring 產生 schema，並在偵測到模型要呼叫時
    自動執行、把結果送回模型——不用自己寫 tool_use/tool_result 的迴圈。"""

    def search_ticker(query: str) -> str:
        """依公司名稱或代號關鍵字，在台股名冊裡搜尋股票代號、名稱、產業別。

        Args:
            query: 公司名稱或代號的關鍵字，例如「台積電」或「2330」。
        """
        keyword = query.strip()
        matches = [
            (ticker, info["name"], info["industry"])
            for ticker, info in directory.items()
            if keyword in ticker or (info["name"] and keyword in info["name"])
        ][:10]
        if not matches:
            return "查無符合的股票。"
        return "\n".join(
            f"{ticker} {name}（{industry or '未分類'}）"
            for ticker, name, industry in matches
        )

    def get_price(ticker: str) -> str:
        """查詢某檔股票最新一筆收盤價、漲跌幅、成交量，資料來自歷史資料庫
        （每日收盤後更新，非盤中即時報價）。

        Args:
            ticker: 股票代號，例如 2330。
        """
        record = get_latest_ticker_record(history_db_path, ticker)
        if record is None:
            return "查無歷史股價資料。"
        change_pct = record["change_pct"]
        change_text = f"{change_pct:+.2f}%" if change_pct is not None else "-"
        volume_text = f"{record['volume']:,}" if record["volume"] is not None else "-"
        return (
            f"日期：{record['date']}　收盤：{record['close']}　"
            f"漲跌：{change_text}　成交量：{volume_text}"
        )

    def get_valuation(ticker: str) -> str:
        """查詢某檔股票最新的本益比 PER、股價淨值比 PBR、殖利率。上市股票查
        FinMind API，上櫃股票改查 TPEX 官方端點（不佔用 FinMind 額度）。

        Args:
            ticker: 股票代號，例如 2330。
        """
        market = (directory.get(ticker) or {}).get("market")
        valuation = fetch_valuation(ticker, finmind_token, market=market)
        if valuation is None:
            return "查無本益比／殖利率資料。"
        per = valuation["per"]
        pbr = valuation["pbr"]
        yield_pct = valuation["dividend_yield"]
        return (
            f"日期：{valuation['date']}　PER：{per if per is not None else 'N/A'}　"
            f"PBR：{pbr if pbr is not None else 'N/A'}　"
            f"殖利率：{yield_pct if yield_pct is not None else 'N/A'}%"
        )

    def get_institutional_investors(ticker: str) -> str:
        """透過 FinMind API 查詢某檔股票最新交易日的三大法人
        （外資、投信、自營商）買賣超。

        Args:
            ticker: 股票代號，例如 2330。
        """
        institutional = fetch_institutional_investors(ticker, finmind_token)
        if institutional is None:
            return "查無三大法人買賣超資料。"
        lines = [f"日期：{institutional['date']}（單位：股）"]
        for row in institutional["breakdown"]:
            lines.append(f"{row['label']}：淨買超 {row['net']:+,}")
        return "\n".join(lines)

    return [search_ticker, get_price, get_valuation, get_institutional_investors]


def run_agent_turn(api_key, history, user_text, directory, history_db_path, finmind_token):
    """主入口：帶著目前對話歷史 history（chat.get_history() 回傳的
    google.genai.types.Content 清單，新對話傳 None）送出這一輪的 user_text。
    回傳 (reply_text, new_history)；new_history 可直接存起來給下一輪用。
    失敗時丟 AgentError，訊息可直接顯示，此時歷史不會被更動。"""
    if not api_key:
        raise AgentError("尚未設定 Gemini API Key（請至「設定」分頁填入）。")

    client = genai.Client(api_key=api_key)
    tools = _build_tools(directory, history_db_path, finmind_token)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=tools,
    )

    try:
        chat = client.chats.create(model=MODEL, config=config, history=history)
        _call_timestamps.append(time.time())
        response = chat.send_message(user_text)
    except errors.ClientError as e:
        code = getattr(e, "code", None)
        if code in (400, 401, 403):
            raise AgentError("Gemini API Key 無效，請至「設定」分頁確認。") from e
        if code == 429:
            raise AgentError(
                "Gemini API 已達免費額度上限（每分鐘請求數），請稍等一下再試。"
            ) from e
        raise AgentError(f"呼叫 Gemini API 失敗：{e}") from e
    except errors.APIError as e:
        raise AgentError(f"呼叫 Gemini API 失敗：{e}") from e

    reply = response.text or "（沒有文字回覆）"
    return reply, chat.get_history()
