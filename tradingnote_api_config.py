"""tradingnote 對外 API 端點集中設定檔。

集中列出全部會實際呼叫的外部資料來源網址。官方端點改版、換路徑或停用時，
只需要改這一個檔案，不用到底下六個模組（core／history／institutional／
taifex／finmind／refresh_concepts）裡逐一尋找、逐一改。本檔本身不 import
任何其他 tradingnote 模組，純常數，避免循環 import。

FinMind API Token（目前唯一需要金鑰／額度的資料來源）不在這裡——它是使用者
個人憑證，不是端點網址，存在 data/settings.json 的 `finmind_token` 欄位
（見 tradingnote_core.py 的 DEFAULT_SETTINGS）。GUI「設定」分頁目前還沒有
輸入框，只能手動編輯該檔（見 STATUS.md「已知缺口」）。
"""

# ---- TWSE（證券交易所）官方 OpenAPI／網頁端點，免金鑰 ----

# 全市場上市股票當日收盤價
TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
# 上市股票本益比／殖利率／股價淨值比
TWSE_VALUATION_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
# 上市公司基本資料（含官方產業別），用於產業分類與 refresh_concepts.py
TWSE_INDUSTRY_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
# 大盤每日成交資訊（判斷交易日、回補歷史用）
TWSE_MI_INDEX_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
# 三大法人（外資／投信／自營商）買賣超，上市股票
TWSE_INSTITUTIONAL_URL = (
    "https://www.twse.com.tw/rwd/zh/fund/T86?response=json&selectType=ALL"
)

# ---- TPEx（櫃買中心）官方 OpenAPI／網頁端點，免金鑰 ----

# 全市場上櫃股票當日收盤價
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
# 上櫃股票本益比／殖利率／股價淨值比
TPEX_PERATIO_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
# 上櫃公司基本資料（含官方產業別）
TPEX_INDUSTRY_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
# 三大法人買賣超，上櫃股票
TPEX_INSTITUTIONAL_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
# 產業價值鏈資訊平台首頁，refresh_concepts.py 爬概念股分類用
TPEX_CHAIN_ROOT_URL = "https://ic.tpex.org.tw/"

# ---- FinMind（api.finmindtrade.com），需 token（見上方說明） ----

# 本益比／三大法人／融資融券等個股籌碼面資料集，免費額度 600 次／小時
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

# ---- TAIFEX（期貨交易所）官方 OpenAPI／網頁端點，免金鑰 ----

# 期貨每日交易行情（盤後 EOD，非即時）
TAIFEX_DAILY_FUTURES_URL = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"
# 大額交易人未沖銷部位，僅回傳最近一個交易日
TAIFEX_LARGE_TRADERS_FUTURES_URL = (
    "https://openapi.taifex.com.tw/v1/OpenInterestOfLargeTradersFutures"
)
# 大額交易人未沖銷部位歷史 CSV 下載端點（非 openapi，POST 表單、Big5 編碼）
TAIFEX_LARGE_TRADERS_HISTORY_URL = "https://www.taifex.com.tw/cht/3/largeTraderFutDown"
# 股票期貨契約代碼對照標的股票清單
TAIFEX_SSF_LIST_URL = "https://openapi.taifex.com.tw/v1/SSFLists"
