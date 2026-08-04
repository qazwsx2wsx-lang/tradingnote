# tradingnote 交接摘要

## 專案位置
`/Users/zhengyufan/Claude/tradingnote`（獨立新專案，與同層的 `/Users/zhengyufan/Claude/trade-journal` 完全無關、無資料共用）。已初始化 git，remote 是 `origin` → `https://github.com/qazwsx2wsx-lang/tradingnote.git`。

## 這是什麼
台股部位紀錄＋產業資金流向＋個股基本面查詢＋期貨盤後行情＋AI 問答的桌面程式，架構參考 `/Users/zhengyufan/Claude/GG/ARCHITECTURE.md`（數獨小遊戲）的「核心邏輯與介面分離」原則：核心模組不依賴任何 UI，CLI 與 GUI 各自 import。**`ARCHITECTURE.md` 目前已經落後於程式碼**（沒有「期貨」「AI 助理」分頁、`tradingnote_ai_agent.py`、`tradingnote_taifex.py` 的說明），下次有空建議一併補上；這份 HANDOFF 是目前實際狀態的快速摘要，發現兩者衝突時以程式碼實際行為為準。

```
tradingnote/
├── tradingnote_core.py       # 核心：部位模型、JSON 持久化、TWSE/TPEX 查價、損益計算、settings.json
├── tradingnote_history.py    # 核心：120日歷史 SQLite、產業分類、產業資金流向分析
├── tradingnote_finmind.py    # 核心：FinMind API（本益比／殖利率／三大法人買賣超），僅 GUI 使用
├── tradingnote_taifex.py     # 核心：TAIFEX 官方期貨每日交易行情（盤後，免金鑰），僅 GUI「期貨」分頁使用
├── tradingnote_ai_agent.py   # 核心：Gemini API 自動函式呼叫問答，僅 GUI「AI 助理」分頁使用
├── tradingnote.py             # CLI（無「個股」「期貨」「AI 助理」模組對應指令）
├── tradingnote_gui.py         # GUI：PySide6 + pyqtgraph（原本是 Tkinter + matplotlib，已整個換掉）
├── 啟動TradingNote.command    # 雙擊啟動 GUI，固定用 /opt/homebrew/bin/python3.12
├── ARCHITECTURE.md             # 本專案架構文件（已過時，見上方說明）
└── data/                       # 執行時自動建立，git 已忽略（.gitignore）
    ├── positions.json          # 使用者紀錄的部位
    ├── price_cache.json        # 全市場收盤價快取（30分鐘 TTL）
    ├── settings.json           # auto_check_continuity、finmind_token、gemini_api_key（GUI「設定」分頁寫入）
    └── history.db              # SQLite：120日歷史價格 + 產業分類快取
```

## GUI 現況：六個分頁
1. **資金流向分析**（預設頁）：pyqtgraph 泡泡圖，可觸控板/滾輪縮放平移，點擊泡泡看該產業前十大成分股。
2. **部位紀錄**：`QTableWidget`，新增/刪除/查價/重新整理。
3. **個股**：`QTreeWidget` 依產業族群列出全市場約1700檔股票（現價/漲跌%，資料來自本地快取），雙擊某檔叫 FinMind API 查最新本益比/殖利率/股價淨值比/三大法人買賣超。
4. **期貨**（這次對話換掉的部分，見下方「這次對話的變動」）：TX（臺股期貨）／MTX（小型臺指期貨）近月合約盤後行情，資料來自 TAIFEX 官方免金鑰端點，一天更新一次，非即時。
5. **AI 助理**：Gemini API（`gemini-flash-lite-latest`）自然語言問答，模型自動判斷呼叫股票查詢/本益比/法人買賣等工具，需要「設定」分頁填入 Gemini API Key。
6. **設定**：自動檢測開關、回補天數、回補歷史資料按鈕、FinMind API Token、Gemini API Key（皆密碼遮罩＋顯示切換、`editingFinished` 自動存檔）。

## 這次對話的變動：期貨資料源從 Fugle 即時報價換成 TAIFEX 官方盤後行情
- **原因**：「期貨」分頁原本串接 Fugle 的 `data-futopt` 即時行情（`tradingnote_fugle.py`，已刪除），但使用者實測啟動軟體出現異常；查證 `developer.fugle.tw` 定價文件（`pricing.md`）後發現 futopt（期貨/選擇權）完全不在免費方案內——連歷史/盤後資料都沒有，只有 intraday 且需要付費 Developer 方案（NT$1,499/月起）才能呼叫，這正是異常的原因（免費 key 呼叫回 403）。
- **改法**：新增 `tradingnote_taifex.py`，改打 TAIFEX 官方公開、免金鑰的「期貨每日交易行情」端點（`https://openapi.taifex.com.tw/v1/DailyMarketReportFut`），一次回傳全市場所有期貨契約（含價差單），從中篩出 TX／MTX 近月合約的「一般」（日盤）與「盤後」（夜盤）兩筆收盤彙總。已用真實 API 呼叫驗證過欄位與近月判斷邏輯正確。
- **GUI 影響**：拿掉 Fugle API Key 輸入框、搜尋框、自選合約清單（`fugle_watchlist`）、15 秒背景輪詢計時器；「期貨」分頁改為隨主要「重新整理」按鈕（`force_refresh`／`_apply_refresh_result`）一起更新，因為資料一天只變一次。
- **順便做的另一件事**：「重新整理」彈窗（`RefreshDialog`）原本只有文字狀態、沒有進度條，這次加了 `QProgressBar`，透過 `get_market_snapshot()` 新增的 `on_progress(done, total, label)` callback 回報 TWSE 抓取／TPEX 抓取／寫入快取／寫入歷史資料庫四個階段。
- **已提交**：`tradingnote_core.py`／`tradingnote_gui.py`／`tradingnote_taifex.py` 已 commit（`dba11b5`），尚未 push 到 `origin/main`。**這份 HANDOFF.md 本身、以及 `ARCHITECTURE.md` 的補充都還沒 commit**，因為前者原本夾帶一組明碼 Fugle API Key（已在這次更新中移除——Fugle 整合已刪除，這組 key 現在沒有任何程式碼會用到；若還需要保留這組 key 做其他用途，請改存在不會進 git 的地方，例如 `data/settings.json` 或另一個已被 `.gitignore` 忽略的檔案）。

## 尚未做 / 刻意留白
- 沒有寫任何自動化測試（unit test），驗證方式都是手動跑 + headless（`QT_QPA_PLATFORM=offscreen`）smoke test。
- 只有 EOD（收盤）/ 盤後資料，非即時報價（刻意選擇：TWSE/TPEX 即時 API 需要 session/referer 處理；Fugle 免費方案不支援期貨；兩者皆放棄即時）。
- CLI 沒有「個股」「期貨」「AI 助理」模組的對應指令，這三個目前只有 GUI 在用。
- `ARCHITECTURE.md` 需要補上「期貨」「AI 助理」分頁、`tradingnote_taifex.py`、`tradingnote_ai_agent.py`、`google-genai` 依賴的說明（目前只寫到 FinMind 為止）。
- 沒有 `requirements.txt`／venv，GUI 依賴（`PySide6`、`pyqtgraph`、`google-genai`）直接裝在 Homebrew python3.12 的使用者站台目錄。

## 如果要繼續開發，建議先讀
1. 這份 `HANDOFF.md`（目前狀態最新，但 `ARCHITECTURE.md` 細節部分落後）
2. `/Users/zhengyufan/Claude/tradingnote/tradingnote_gui.py`（GUI 全貌）
3. `/Users/zhengyufan/Claude/tradingnote/tradingnote_taifex.py`（期貨盤後資料，這次新模組，程式碼很短）
4. `/Users/zhengyufan/Claude/tradingnote/tradingnote_ai_agent.py`（AI 助理，程式碼很短）
