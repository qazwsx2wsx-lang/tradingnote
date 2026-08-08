# tradingnote 交接摘要

## 必讀簡介
新 session 開頭只需要讀這一段（原則上不超過 20 行）；下面「詳細內容」是備查用，需要細節才展開讀，省 token。**每次交接前，把最新、最需要注意的事更新到這一段，並保持精簡**——細節寫進下方對應章節，不要塞在這裡。

- **這是什麼**：台股部位紀錄＋產業資金流向＋個股基本面查詢＋期貨盤後行情＋AI 問答的桌面程式（PySide6 + pyqtgraph）。位置：`/Users/zhengyufan/Claude/tradingnote`，跟同層 `trade-journal` 無關。
- **Git 狀態**：`main` branch，領先 `origin/main` 4 個 commit（`d886e2c` 之後，尚未 push）：`d93cb86`（HTTP 共用工具重構）、`b417810`（資金流向頁泡泡圖/量比異常清單）、`d2ffe90`（啟動 loading 進度）、以及本次對話最後一個 commit（快取結構重構＋期貨資料日期顯示，見下方「變更歷史 → 2026-08-07」）。四個 commit 原本混在一起、跨 session 疊加在同一批未 commit 異動裡，本次對話重建成互相獨立、各自可編譯／可 import 驗證過的 commit（拆分方法：反轉/重放已知 diff hunk，見對話紀錄，不重新贅述於此）。工作目錄本身已乾淨，commit 尚未 push 到 `origin`。
- **只是要接著開發新功能**：讀到這裡就夠了。要動到 GUI 分頁結構、期貨模組、或想知道更早的變更緣由，才需要往下展開「詳細內容」。

---

## 詳細內容（需要才展開閱讀）

### 專案位置
`/Users/zhengyufan/Claude/tradingnote`（獨立新專案，與同層的 `/Users/zhengyufan/Claude/trade-journal` 完全無關、無資料共用）。已初始化 git，remote 是 `origin` → `https://github.com/qazwsx2wsx-lang/tradingnote.git`。

### 這是什麼
台股部位紀錄＋產業資金流向＋個股基本面查詢＋期貨盤後行情＋AI 問答的桌面程式，架構參考 `/Users/zhengyufan/Claude/GG/ARCHITECTURE.md`（數獨小遊戲）的「核心邏輯與介面分離」原則：核心模組不依賴任何 UI，CLI 與 GUI 各自 import。**`ARCHITECTURE.md` 目前已經落後於程式碼**（沒有「期貨」「AI 助理」分頁、`tradingnote_ai_agent.py`、`tradingnote_taifex.py`、`tradingnote_http.py` 的說明），下次有空建議一併補上；這份 HANDOFF 是目前實際狀態的快速摘要，發現兩者衝突時以程式碼實際行為為準。

```
tradingnote/
├── tradingnote_core.py       # 核心：部位模型、JSON 持久化、TWSE/TPEX 查價、損益計算、settings.json
├── tradingnote_history.py    # 核心：120日歷史 SQLite、產業分類、產業資金流向分析、個股量比異常清單
├── tradingnote_finmind.py    # 核心：FinMind API（本益比／殖利率／三大法人買賣超），僅 GUI 使用
├── tradingnote_taifex.py     # 核心：TAIFEX 官方期貨每日交易行情（盤後，免金鑰），僅 GUI「期貨」分頁使用
├── tradingnote_ai_agent.py   # 核心：Gemini API 自動函式呼叫問答，僅 GUI「AI 助理」分頁使用
├── tradingnote_http.py       # 核心：共用 HTTP／數值解析工具（新，見上方「未 commit 的異動」）
├── tradingnote_cache.py      # 核心：共用檔案／記憶體 TTL 快取工具（新，見上方「未 commit 的異動」）
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

### GUI 現況：六個分頁
1. **資金流向分析**（預設頁）：pyqtgraph 泡泡圖（大小＝資金比重%），可觸控板/滾輪縮放平移，點擊泡泡看該產業前十大成分股；下方有「資金動向清單」（產業層級）跟「個股量比異常清單」（個股層級，2026-08-05 新增）兩個表格。
2. **部位紀錄**：`QTableWidget`，新增/刪除/查價/重新整理。
3. **個股**：`QTreeWidget` 依產業族群列出全市場約1700檔股票（現價/漲跌%，資料來自本地快取），雙擊某檔叫 FinMind API 查最新本益比/殖利率/股價淨值比/三大法人買賣超（`StockDetailDialog`，「個股量比異常清單」雙擊也共用同一個對話框）。
4. **期貨**：TX（臺股期貨）／MTX（小型臺指期貨）近月合約盤後行情，資料來自 TAIFEX 官方免金鑰端點，一天更新一次，非即時。
5. **AI 助理**：Gemini API（`gemini-flash-lite-latest`）自然語言問答，模型自動判斷呼叫股票查詢/本益比/法人買賣等工具，需要「設定」分頁填入 Gemini API Key。
6. **設定**：自動檢測開關、回補天數、回補歷史資料按鈕、FinMind API Token、Gemini API Key（皆密碼遮罩＋顯示切換、`editingFinished` 自動存檔）。

### 變更歷史

#### （日期不明，先前某 session 寫下；2026-08-07 本次對話審閱並 commit）：抽出共用 `tradingnote_http.py`
- **背景**：`tradingnote_core.py`／`tradingnote_history.py`／`tradingnote_finmind.py` 各自重複一份 HTTP JSON 抓取＋數值解析（`_http_get_json`／`_to_float`／`_to_int`），`tradingnote_taifex.py` 則乾脆自己重寫一份、空值判斷邏輯還跟原版分岔。統一抽成 `tradingnote_http.py`（`http_get_json`／`to_float`／`to_int`／`PriceFetchError`），四個模組一律從這裡 import。
- **驗證**（本次對話補做，先前 session 沒有留下驗證紀錄）：`python3 -m py_compile` 全部通過；用 `/opt/homebrew/bin/python3.12` import `tradingnote_core`／`tradingnote_taifex`／`tradingnote_finmind`／`tradingnote_history`／`tradingnote.py` 成功；headless GUI smoke test（見下方兩則 2026-08-07 change log）在這個版本之上疊加其他改動後仍正常運作，未發現行為差異。
- **已 commit**：`d93cb86`。

#### 2026-08-07：優化快取結構——抽出共用 `tradingnote_cache.py`
- **動機**：專案裡有四份各自重寫的 TTL 快取邏輯：`tradingnote_core.get_market_snapshot`（`price_cache.json`）與 `tradingnote_taifex.get_cached_daily_futures_report`（`futures_cache.json`）兩份幾乎一模一樣的「讀檔案→檢查 `fetched_at` 是否在 TTL 內→過期或不存在就重抓→寫回檔案，API 失敗時退回舊快取」；`tradingnote_core._tpex_valuation_cache`（單一全域 `(fetched_at, data)` tuple）與 `tradingnote_finmind._dataset_cache`（以 `(dataset, ticker, lookback_days)` 為 key 的 dict）兩份幾乎一模一樣的「命中未過期快取直接回傳、否則重新呼叫並存入」行程內記憶體快取。比照先前 `tradingnote_http.py` 的做法（見「2026-08-05 之前」的未 commit 異動 #3）抽成共用模組。
- **改法**：新增 `tradingnote_cache.py`，提供：
  - `load_fresh_file_cache(cache_path, ttl_seconds)` / `load_stale_file_cache(cache_path)` / `write_file_cache(cache_path, payload)`：檔案 JSON TTL 快取的讀／不管新鮮度讀／寫，payload 是除了 `fetched_at` 以外要存的內容（`{"prices": {...}}` 或 `{"rows": [...]}`），呼叫端自己決定 key 名稱。
  - `TTLCache` class：行程內記憶體 TTL 快取，`get_or_fetch(key, fetch_fn)`，key 用 `None` 代表只有單一份快取（`_tpex_valuation_cache` 的用法）、用 tuple 代表依查詢條件分開快取（`_dataset_cache` 的用法）；`fetch_fn()` 拋例外不會被快取，語意跟改之前完全一致。
  - `tradingnote_core.py`：`get_market_snapshot` 改用 `load_fresh_file_cache`/`load_stale_file_cache`/`write_file_cache`；`_tpex_valuation_cache` 改成 `TTLCache` 實例，`get_tpex_valuation` 改成一行 `get_or_fetch`。
  - `tradingnote_taifex.py`：`get_cached_daily_futures_report` 同樣改用檔案快取三個函式，拿掉自己的 `json`/`Path`/`datetime` import（不再需要）。
  - `tradingnote_finmind.py`：`_dataset_cache` 改成 `TTLCache` 實例，`_fetch_dataset` 內部抓取邏輯包成 closure `_do_fetch`，交給 `get_or_fetch` 處理快取判斷（`_call_timestamps.append` 仍只在 closure 真的被呼叫、也就是快取未命中時才執行，用量統計語意不變）。
- **驗證（單元層級）**：`python3 -m py_compile` 全部通過；用真正的 `/opt/homebrew/bin/python3.12`（專案指定直譯器，不是系統內建 3.9，import 型別語法 `str | None` 需要 3.10+）import 三個改動模組成功；獨立腳本驗證 `TTLCache`（命中快取不重呼叫 fetch_fn、過期後重呼叫、`fetch_fn` 拋例外時不寫入快取）與檔案快取三函式（不存在回 `None`、寫入後讀得回來、TTL=0 視為過期）皆符合預期。
- **驗證（headless GUI smoke test，含真實網路）**：`QT_QPA_PLATFORM=offscreen` 跑一份臨時腳本（複製 `main()` 的啟動流程，`app.exec()` 在 `on_ready` 裡 `app.quit()` 收尾），驗證：①`run_startup_preload_in_thread` 正常跑完、`TradingNoteWindow` 建成、6 個分頁（資金流向分析／部位紀錄／個股／期貨／AI 助理／設定）都在；②`get_market_snapshot`／`get_cached_daily_futures_report` 重新呼叫時吃到剛才啟動時寫入的快取，回傳筆數一致（真實 `price_cache.json` 11696 檔、`futures_cache.json` 2201 筆、`get_futures_snapshot` 正確回傳 TX／MTX）；③`get_tpex_valuation("6488")` 真的打了一次 TPEX 官方估值 API（拿到本益比/股價淨值比/殖利率），第二次呼叫從 `TTLCache` 命中、耗時從 0.363s 降到 <0.1ms；④`fetch_valuation("2330", token, market="TWSE")` 真的打了一次 FinMind API（`get_call_count()` +1），第二次呼叫命中 `_dataset_cache`（`get_call_count()` 不再增加）。13 項檢查全數通過，跟改動前記錄的行為一致。
- **已 commit**（hash 見上方必讀簡介）。

#### 2026-08-07：「期貨」分頁顯示資料日期
- **動機**：「期貨」分頁本身完全沒有日期資訊，使用者無法直接判斷目前顯示的 TX／MTX 收盤資料是哪一天的（例如今天 TAIFEX 還沒發布新資料、實際看到的是昨天收盤價）；要看資料日期得切去「設定」分頁的「資料新鮮度」區塊才看得到，不直覺。
- **改法**：`refresh_futures_tab` 的 `on_done` callback 裡，原本重新整理成功時把 `futures_status_label`（原本只在失敗時顯示錯誤訊息的欄位）清空成 `""`，改成用既有的 `_futures_snapshot_date(snapshot)`（本來就給「設定」分頁的資料新鮮度用）算出最新資料日期，顯示成「資料日期：YYYY-MM-DD」；查無日期（空快取）時仍顯示空字串。重新整理失敗時的行為不變，`on_error` 照舊把錯誤訊息蓋上去。
- **驗證**：headless GUI smoke test（`QT_QPA_PLATFORM=offscreen`，真實網路＋真實 `data/futures_cache.json`）驗證啟動流程跑完、背景 `run_task_in_thread` 完成後 `futures_status_label.text()` 顯示「資料日期：2026-08-07」，跟真實 TAIFEX 資料的日期一致。
- **已 commit**（hash 見上方必讀簡介）。

#### 2026-08-05：資金流向分析頁 —— 泡泡大小正規化 + 個股量比異常清單
- **動機**：泡泡圖原本用絕對成交金額決定大小，導致半導體這類本身盤子大的產業永遠是最大顆泡泡，市值小但近期突然爆量的中小型族群（例如矽光子、散熱次族群）會被淹沒看不見。
- **改法①：泡泡大小 → 資金比重(%)**。`IndustryFlow` 新增 `capital_share_pct` 欄位（`compute_industry_flow`，分母是全市場今日總成交金額，含歷史不足會被過濾的產業，避免佔比隨過濾結果跳動）；`populate_flow_chart` 改用這個值決定泡泡半徑，標籤同時顯示「產業名稱 + %」。**注意**：這是絕對金額的線性換算，相對排序/大小比例跟改之前完全一樣，不會讓小族群視覺上變大；真要讓「爆量但小」的族群在泡泡圖上顯眼，需要改用相對指標（例如量比）決定大小，目前尚未這樣做。
- **改法②：新增「個股量比異常清單」**。`tradingnote_history.py` 新增 `VolumeRatioOutlier` dataclass 與 `compute_volume_ratio_outliers(db_path, snapshot, avg_days=5, min_ratio=1.5)`：逐檔股票比較「今日成交量 ÷ 近N日均量」（跟 `compute_industry_flow` 的量比不同，那是產業加總後的比值，單一檔股票爆量會被同產業其他股票稀釋掉），依比值分三級距（`VOLUME_RATIO_TIERS = ("1.5～2倍", "2～3倍", "3倍以上")`）。`tradingnote_gui.py` 在資金流向分析頁新增對應區塊：均量天數輸入、級距篩選下拉、可排序表格，雙擊個股開啟既有的 `StockDetailDialog` 查 FinMind。
- **驗證**：兩項都用真實 `data/history.db` + headless（`QT_QPA_PLATFORM=offscreen`）跑過，`capital_share_pct` 加總為 100%、異常清單抓到 176 檔（1.5～2倍 107、2～3倍 52、3倍以上 17），級距篩選、排序、雙擊資料綁定（ticker/market）都確認正確。
- **改法③：成分股清單 10 檔 → 30 檔**。`tradingnote_history.py` 的 `get_industry_top_stocks_range` 預設 `top_n` 從 10 改成 30；`tradingnote_gui.py` 呼叫處同步改成 `top_n=30`。`results[:top_n]` 本來就會在候選數不足時回傳全部，所以「該產業不足30檔就全部顯示」不需要額外邏輯。同時把 `IndustryTopStocksDialog` 視窗標題（原本寫死「前十大成分股」）改成用 `len(top_stocks)` 動態顯示實際筆數，避免產業檔數不足30時標題誤植；表格高度上限也從 320px 調到 760px，讓30檔在一般畫面上不用捲動就能看完。已用真實 DB 驗證：210檔的「電子零組件業」顯示30檔、標題「前 30 大」，只有4檔的「農業科技業」顯示全部4檔、標題「前 4 大」。
- **改法④：新增啟動 loading 進度**。原本 `TradingNoteWindow.__init__`（`tradingnote_gui.py`）同步做三件事才 `show()`：`get_market_snapshot`（可能連網抓 TWSE/TPEX）、`record_snapshot`（寫歷史DB）、以及後面 `refresh_flow_tab`/`refresh_stocks_tab` 內部觸發的產業分類更新（也可能連網）——網路狀況不好時，視窗在這段期間完全不會出現（`main()` 是 `TradingNoteWindow()` 建構完才 `window.show()`），使用者只會看到程式「沒反應」。
  - 新增 `run_startup_preload_in_thread(parent, progress_cb, done_cb, error_cb)`（`tradingnote_gui.py`，緊接在 `run_refresh_in_thread` 後面），沿用同一套背景執行緒＋`queue.Queue`＋150ms `QTimer` 輪詢機制，在背景做完「抓報價→寫歷史DB→更新產業分類」三步（共4個進度階段），完成後把 `snapshot` 帶回主執行緒。
  - 新增 `StartupProgressDialog`（`tradingnote_gui.py`，緊接在新函式後面）：純顯示用的小視窗（狀態文字＋`QProgressBar`），故意拿掉關閉鈕（`CustomizeWindowHint`）——啟動階段還沒有主視窗可以退回去，如果使用者手動關掉會導致背景做完後沒有視窗可顯示。
  - `TradingNoteWindow.__init__` 簽名改成 `__init__(self, snapshot, last_error)`，不再自己呼叫 `get_market_snapshot`/`record_snapshot`，直接吃 `main()` 傳進來的結果。
  - `main()` 改成：建立 `QApplication` → 顯示 `StartupProgressDialog` → 呼叫 `run_startup_preload_in_thread` → 背景工作完成的 callback（`on_ready`）裡才建立 `TradingNoteWindow(snapshot, last_error)`、`show()`、關掉 splash → 進入 `app.exec()`。`on_ready` 建立的 `window` 特別存進 `main()` 區域變數的 `windows` 這個 list，避免 Python 端物件被回收導致視窗一閃就消失（PySide 常見坑，巢狀 callback 裡的區域變數沒有外部參照會被 GC）。
  - **驗證**：兩條路徑都用 headless（`QT_QPA_PLATFORM=offscreen`）+ 真實 `data/price_cache.json`／`data/history.db` 跑過。正常路徑：splash 顯示 → 進度事件依序收到「正在寫入歷史資料庫...」「正在更新產業分類...」「完成」→ `on_ready` 收到 11612 檔 snapshot → 視窗建立、`show()` 成功、6 個分頁都在。錯誤路徑：monkeypatch `get_market_snapshot` 讓它拋 `PriceFetchError` → `on_ready` 收到空 snapshot＋錯誤訊息 → 視窗依然正常建立顯示（跟改之前 `except PriceFetchError` 的行為一致，只是現在錯誤是從背景執行緒的 `error_cb` 傳回來，不是 `__init__` 自己 catch）。
- **已 commit**：改法①②③ → `b417810`；改法④（啟動 loading 進度）→ `d2ffe90`。這兩個 commit 原本跟其他改動疊在同一批未 commit 異動裡，事後依 diff hunk 拆開重建，見上方必讀簡介。

#### 2026-08-03：期貨資料源從 Fugle 即時報價換成 TAIFEX 官方盤後行情
- **原因**：「期貨」分頁原本串接 Fugle 的 `data-futopt` 即時行情（`tradingnote_fugle.py`，已刪除），但使用者實測啟動軟體出現異常；查證 `developer.fugle.tw` 定價文件後發現 futopt（期貨/選擇權）完全不在免費方案內——連歷史/盤後資料都沒有，只有 intraday 且需要付費 Developer 方案（NT$1,499/月起）才能呼叫，這正是異常的原因（免費 key 呼叫回 403）。
- **改法**：新增 `tradingnote_taifex.py`，改打 TAIFEX 官方公開、免金鑰的「期貨每日交易行情」端點（`https://openapi.taifex.com.tw/v1/DailyMarketReportFut`），一次回傳全市場所有期貨契約（含價差單），從中篩出 TX／MTX 近月合約的「一般」（日盤）與「盤後」（夜盤）兩筆收盤彙總。已用真實 API 呼叫驗證過欄位與近月判斷邏輯正確。
- **GUI 影響**：拿掉 Fugle API Key 輸入框、搜尋框、自選合約清單（`fugle_watchlist`）、15 秒背景輪詢計時器；「期貨」分頁改為隨主要「重新整理」按鈕（`force_refresh`／`_apply_refresh_result`）一起更新，因為資料一天只變一次。
- **順便做的另一件事**：「重新整理」彈窗（`RefreshDialog`）原本只有文字狀態、沒有進度條，這次加了 `QProgressBar`，透過 `get_market_snapshot()` 新增的 `on_progress(done, total, label)` callback 回報 TWSE 抓取／TPEX 抓取／寫入快取／寫入歷史資料庫四個階段。
- **狀態**：此變動已 commit（`dba11b5`）並 push 到 `origin/main`（後續又有數個 commit，見上方必讀簡介的 Git 狀態，皆已同步）。

### 尚未做 / 刻意留白
- 沒有寫任何自動化測試（unit test），驗證方式都是手動跑 + headless（`QT_QPA_PLATFORM=offscreen`）smoke test。
- 只有 EOD（收盤）/ 盤後資料，非即時報價（刻意選擇：TWSE/TPEX 即時 API 需要 session/referer 處理；Fugle 免費方案不支援期貨；兩者皆放棄即時）。
- CLI 沒有「個股」「期貨」「AI 助理」模組的對應指令，這三個目前只有 GUI 在用。
- `ARCHITECTURE.md` 需要補上「期貨」「AI 助理」分頁、`tradingnote_taifex.py`、`tradingnote_ai_agent.py`、`tradingnote_http.py`、`google-genai` 依賴的說明（目前只寫到 FinMind 為止）。
- 沒有 `requirements.txt`／venv，GUI 依賴（`PySide6`、`pyqtgraph`、`google-genai`）直接裝在 Homebrew python3.12 的使用者站台目錄。
- 泡泡圖大小目前是「資金比重%」（絕對金額的線性換算），還不是能凸顯「小市值但爆量」的相對指標，見上方「變更歷史 → 2026-08-05」的注意事項。

### 如果要繼續開發，建議先讀
1. 這份 `HANDOFF.md` 的「必讀簡介」（目前狀態最新，但 `ARCHITECTURE.md` 細節部分落後）
2. `/Users/zhengyufan/Claude/tradingnote/tradingnote_gui.py`（GUI 全貌）
3. 若要動到共用 HTTP／期貨／AI 助理：`tradingnote_http.py`（新重構，先 `git diff` 確認）、`tradingnote_taifex.py`、`tradingnote_ai_agent.py`
