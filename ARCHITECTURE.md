# tradingnote - 程式架構

## 專案結構

```
tradingnote/
├── tradingnote_core.py     # 核心邏輯（部位紀錄、台股價格擷取、損益分析），兩個介面共用
├── tradingnote_history.py  # 核心邏輯（120日歷史、產業分類、產業資金流向分析），兩個介面共用
├── tradingnote_finmind.py  # 核心邏輯（FinMind API：本益比／殖利率／三大法人買賣超），僅 GUI 使用
├── tradingnote.py           # 終端機文字介面（CLI）
├── tradingnote_gui.py       # 圖形介面（PySide6 + pyqtgraph GUI）
├── 啟動TradingNote.command  # 雙擊啟動 GUI 版的腳本
└── data/
    ├── positions.json        # 使用者紀錄的部位（執行時自動建立）
    ├── price_cache.json      # 台股整市場收盤價快取（執行時自動建立）
    ├── settings.json          # 使用者可調整的設定，如「每次啟動自動檢測」「FinMind token」（執行時自動建立）
    └── history.db             # SQLite：120日歷史價格、產業分類快取（執行時自動建立）
```

設計原則：**核心邏輯與介面分離**（與 `GG/sudoku_core.py` 同一套原則）。`tradingnote_core.py` 不 import 任何 GUI 套件、不 print，只負責部位資料模型、JSON 持久化、台股價格擷取與損益計算；`tradingnote_history.py` 同樣不依賴任何介面，負責歷史資料庫與產業資金流向分析；`tradingnote_finmind.py` 同樣不依賴任何介面，負責 FinMind API 的本益比／法人買賣查詢；`tradingnote.py` 與 `tradingnote_gui.py` 各自 import 這些核心模組，各自實作自己的顯示與互動方式（目前只有 GUI 用到 `tradingnote_finmind.py`）。

**依賴**：原本刻意零外部依賴（純 stdlib）。GUI 改用 `PySide6`（Qt for Python）+ `pyqtgraph` 內嵌繪圖後，此設計被打破——`tradingnote_gui.py` 需要這兩個套件；`tradingnote_core.py`／`tradingnote_history.py`／`tradingnote_finmind.py`／`tradingnote.py`（CLI）仍然零依賴（`tradingnote_finmind.py` 純粹用 `urllib` 打 FinMind 的 REST API，沒有額外套件）。GUI 原本是 Tkinter + matplotlib，改為 PySide6 + pyqtgraph 的原因是 pyqtgraph 的 `ViewBox` 原生支援滑鼠滾輪／觸控板兩指捲動縮放、拖曳平移，且可以額外接 macOS 觸控板雙指捏合（pinch）手勢，matplotlib 的內嵌畫布沒有這種互動能力。因為 GUI 必須用 Homebrew Python 3.12（見下方第 4 節原因），安裝指令也要對著同一個直譯器下：

```bash
/opt/homebrew/bin/python3.12 -m pip install --user --break-system-packages PySide6 pyqtgraph
```

（Homebrew 的 Python 預設是 PEP 668 externally-managed，直接 `pip install` 會被擋下，需要這兩個旗標；用 `--user` 裝進使用者站台目錄，不會動到 Homebrew 自己的安裝。）

---

## 1. `tradingnote_core.py`（共用核心）

### 資料模型
- `Position`：`id`、`ticker`、`shares`、`entry_price`、`entry_date`、`note`、`name`、`market`
- `PriceInfo`：正規化後的價格資料（不論來源是 TWSE 或 TPEX，欄位一致）
- `PnLResult`：現價、現值、成本、未實現損益、損益百分比

### 紀錄（部位 CRUD + JSON 持久化）
| 函式 | 功能 |
|---|---|
| `load_positions(path)` / `save_positions(path, positions)` | 讀寫 `positions.json` |
| `add_position(...)` | 新增部位，並嘗試立即查價填入名稱／市場別 |
| `remove_position(positions, id)` | 依 id（可用前綴）刪除部位 |
| `find_position(positions, id)` | 依 id 尋找部位 |

### 設定（JSON 持久化）
`load_settings(path)` / `save_settings(path, settings)`：讀寫 `settings.json`，`load_settings` 會用 `DEFAULT_SETTINGS` 補齊缺漏欄位（目前只有 `auto_check_continuity`，預設 `True`）。CLI 與 GUI 共用同一份設定檔——GUI 的「設定」分頁勾選/取消「每次啟動自動檢測」會立即寫回這個檔案，CLI 下次啟動也會讀到同一個值。

### 查價（台股價格擷取）
| 函式 | 功能 |
|---|---|
| `fetch_twse_all()` | 呼叫 TWSE Open API `STOCK_DAY_ALL`，回傳全部上市股票今日收盤資料 |
| `fetch_tpex_all()` | 呼叫 TPEX Open API `tpex_mainboard_daily_close_quotes`，回傳全部上櫃股票今日收盤資料 |
| `get_market_snapshot(cache_path, force_refresh=False)` | 整市場快取（30 分鐘 TTL 或手動強制刷新），失敗時退回舊快取 |
| `lookup_price(ticker, snapshot)` | 從快取中查單一代號（純記憶體查找，不打 API） |

兩支 API 皆為公開、免金鑰、一次回傳全市場資料，用 `urllib.request` 即可（不需要額外安裝 `requests`）。上市／上櫃的判斷方式：分別在兩個字典中查找該代號屬於哪一邊。

### 分析（損益計算）
`compute_pnl(position, price)`：以現價與成本價計算未實現損益與百分比，是使用者部位層級的分析功能。

---

## 2. `tradingnote_history.py`（歷史與產業資金流向，共用核心）

不依賴任何介面，獨立於 `tradingnote_core.py` 之外（避免核心模組混雜 SQLite 邏輯）。

### 資料庫
`data/history.db`（SQLite，stdlib `sqlite3`，執行時自動建立）：
- `daily_prices(date, ticker, market, name, close, change_pct, volume, trading_value)`：每日全市場快照，PK 為 `(date, ticker)`（另有 `(ticker, date DESC)` 索引，見下方 LIFO 一節）。
- `industry_map(ticker, name, industry, market, updated_at)`：股票代號對應的產業分類。

**連線層設定**：`_connect()` 一律用 `sqlite3.connect(path, timeout=30)` 並開 `PRAGMA journal_mode=WAL`。原因：GUI 啟動時的背景資料連續性同步、使用者手動點「回補歷史資料」、以及 CLI／GUI 同時開啟，都可能同時對這個檔案讀寫——實測過兩個執行緒同時呼叫 `backfill_twse_history` 若不處理會直接卡死（SQLite 預設 5 秒 busy timeout 在高頻寫入下不夠，且沒有 WAL 時讀寫會互相鎖）。WAL 模式讓讀者不擋寫者、寫者不擋讀者，`timeout=30` 給真的需要排隊的寫入更多緩衝。

### 歷史回補與資料連續性
| 函式 | 功能 |
|---|---|
| `record_snapshot(db_path, snapshot)` | 把 `get_market_snapshot()` 拿到的當日整市場快照寫入 `daily_prices`（UPSERT，重複執行安全）。CLI／GUI 每次啟動或 `refresh` 時呼叫，是 TPEX 歷史逐日累積的**唯一**來源。 |
| `backfill_twse_history(db_path, target_days=120)` | 用舊版 TWSE 端點 `https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=...&type=ALLBUT0999`（未列在官方 openapi swagger 中，格式未來可能改變）補到有 `target_days` 個 TWSE 交易日為止。**已存在的日期會靜默跳過**（不呼叫 `on_progress`），只有真的補到新的一天才會呼叫 `on_progress`——這讓這個函式同時扮演兩個角色而不衝突：① 初次的大量回補（120 天，需要 1-2 分鐘，GUI 在「設定」分頁手動觸發）；② **架構上的資料連續性保證**：CLI／GUI 每次啟動都會自動呼叫一次，資料已經連續時幾乎瞬間完成、完全靜默，只有真的中斷了幾天沒開程式、出現缺口時才會被感知到（印出/顯示進度）。可安全中斷後重跑。**同一行程內用 `_backfill_lock`（`threading.Lock`）確保同時間只有一個在跑**——啟動自動同步跟使用者手動點按鈕若前後腳觸發，第二個呼叫會排隊等第一個做完，而不是兩個同時打 API／寫入互相拖累（實測修好前會直接卡死其中一個）。**TPEX 沒有對應的免費歷史端點**——已測試 `stk_quote_result.php` 的 `d` 參數會被忽略、永遠回傳當天資料，因此上櫃股票只能靠 `record_snapshot` 逐日累積，資料連續性保證僅涵蓋 TWSE。 |
| `prune_history(db_path, keep_trading_days=120)` | 只保留最近 120 個相異日期，避免資料庫無限成長。 |

### 個股資料查詢（LIFO：主要存取順序）
`get_latest_ticker_record(db_path, ticker)` / `get_ticker_history(db_path, ticker, limit=None)`：本專案存取單一股票歷史資料的**標準方式是 LIFO**——SQL 一律 `ORDER BY date DESC`，最新日期優先，並有專屬索引 `idx_daily_prices_ticker_date ON daily_prices (ticker, date DESC)` 支撐（PK 索引是 `(date, ticker)`，`ticker` 只是次要欄位，對「依代號查」這個模式完全用不上、會退化成全表掃描——已用 `EXPLAIN QUERY PLAN` 驗證過，加了這個索引後從 `SCAN` 變成 `SEARCH`）。用途：
- 即時快照（`get_market_snapshot()`）查無某檔股票時（API 當下沒回傳、或剛好卡在重新整理中間），CLI 的 `price` 指令與 GUI 的查價視窗會改向 `get_latest_ticker_record` 要「歷史資料庫裡最新一筆」，讓個股查詢的資料連續性延伸到單檔股票層級，而不只是整體市場快照。
- `compute_industry_flow` 計算每檔股票近 N 日均量時，底層查詢也是 `ORDER BY date DESC`，直接取前 N 筆即為「近 N 日」，不需要在 Python 端額外排序。

### 產業資金流向分析
`compute_industry_flow(db_path, snapshot, avg_days=5) -> list[IndustryFlow]`：把當日快照依產業分組，`avg_days` 同時決定 X／Y 兩軸的天數，讓「N 日流向」的定義一致：計算每個產業近 `avg_days` 個交易日的累積漲跌幅（`avg_change_pct`，今日收盤對比 `avg_days` 個交易日前收盤，用今日成交金額加權平均，取代單日漲跌%）、當日總成交金額（`total_trading_value`，泡泡大小的依據，用全部有效成分股計算，不受歷史資料限制）、以及當日成交量相對近 `avg_days` 日均量的比值（`volume_ratio`）。`avg_change_pct` 與 `volume_ratio` 用同一組「歷史資料足夠」的成分股計算（歷史天數 < `max(2, avg_days // 2)` 的股票會被整檔排除），資料不足時兩者皆為 `None`，畫圖端要處理這個情況而不能直接當數字用。GUI「資金流向分析」頁的日數下拉選單（5/10/20 日）直接把選到的值傳進這個參數。

`get_industry_top_stocks(db_path, snapshot, industry, top_n=10) -> list[dict]`：回傳指定產業依當日成交金額排序的前 `top_n` 大成分股（`ticker`／`name`／`close`／`change_pct`／`trading_value`）。**本專案沒有股本／發行股數資料**（`PriceInfo`、`daily_prices` 都只有收盤價、成交量、成交金額），無法計算真正市值，因此用成交金額排序作為市值的代理指標；完全用既有的 `snapshot` + `get_industry_map()`，不額外打 API，跟「個股」分頁列清單同一個「只用本地資料」原則。GUI 資金流向頁點擊某個產業泡泡（`pg.ScatterPlotItem.sigClicked`）時呼叫，結果顯示在圖表下方的表格。**實作時踩過的坑**：`sigClicked.emit(self, points, ev)` 帶 3 個位置參數，連到 `lambda` 時若沒先吃滿這 3 個位置參數，`industry=f.industry` 這種預設值寫法會被第 3 個位置參數（`ev`）覆蓋掉，導致每次點擊收到的都是 `ev` 而非產業名稱——已用 headless 測試（手動 `emit` 並比對回傳值）抓出並修正，正確寫法是 `lambda _plot, _pts, _ev, industry=f.industry: ...`。

---

## 3. `tradingnote_finmind.py`（FinMind API，僅 GUI 使用）

不依賴任何介面，純粹用 `urllib` 打 FinMind v4 REST API（`https://api.finmindtrade.com/api/v4/data`），跟 `tradingnote_core.py`／`tradingnote_history.py` 共用同一套「核心邏輯與介面分離」原則。目前只有 GUI 的「個股」分頁在使用者雙擊個股時才會呼叫，不影響其他既有的 TWSE/TPEX 查價機制。

| 函式 | 功能 |
|---|---|
| `fetch_valuation(ticker, token, lookback_days=10)` | 查 `TaiwanStockPER` 資料集，回傳最新一筆本益比（PER）、股價淨值比（PBR）、殖利率；查無資料回傳 `None` |
| `fetch_institutional_investors(ticker, token, lookback_days=10)` | 查 `TaiwanStockInstitutionalInvestorsBuySell` 資料集，回傳最新交易日的三大法人（外資、外資自營商、投信、自營商自行買賣、自營商避險）買賣超；查無資料回傳 `None` |

兩者都用 `lookback_days`（預設 10 天）抓一段區間再取最後一筆，而不是只查當天——原因跟 CLI/GUI 既有的 LIFO 備援邏輯一樣：遇到假日或盤後資料延遲，當天可能還沒有資料，抓區間、取陣列最後一筆能保證拿到「最新的一筆」而不是查無資料。`token` 留空仍可呼叫（FinMind 允許匿名查詢），但額度很低、部分資料集可能查不到，token 存在 `settings.json` 的 `finmind_token` 欄位（跟 `auto_check_continuity` 同一份設定檔），只能從 GUI 的「設定」分頁輸入。查詢失敗（連線失敗、回應 `msg` 不是 `success`）一律拋出 `PriceFetchError`（沿用 `tradingnote_core.py` 的例外類型，讓 GUI 端不用額外多 import 一種例外）。

---

## 4. `tradingnote.py`（終端機版）

指令列迴圈，指令包含 `add`／`list`／`remove`／`price`／`refresh`／`flow`／`backfill`／`help`／`quit`。`flow` 印出文字版產業資金流向表格（不需要 matplotlib）；`backfill` 同步執行 `backfill_twse_history` 並印出進度（手動強制執行，用來確認/重跑）。`price` 查無即時報價時會 fallback 到 `get_latest_ticker_record`（LIFO）並標註「取自歷史資料」。`main()` 啟動時除了 `record_snapshot`，會先讀 `settings.json`（跟 GUI 共用同一份），只有 `auto_check_continuity` 為真（預設）才呼叫 `sync_history_continuity()`（本檔案內的小函式，包一層 `backfill_twse_history`）：資料已連續時完全靜默、幾乎不耗時；真的有缺口才會印出補齊進度。這個開關目前只能從 GUI 的「設定」分頁切換（CLI 沒有對應指令），但兩邊讀寫同一個檔案，切一次兩邊都生效。表格輸出沿用與 `sudoku.py` 相同的 `visible_width()`/`pad()`，處理中文字（雙寬字元）與 ANSI 顏色碼的對齊問題。CLI 目前沒有「個股」模組的對應指令，`tradingnote_finmind.py` 只有 GUI 在用。

---

## 5. `tradingnote_gui.py`（圖形介面版，PySide6 + pyqtgraph）

**模組切換（分頁）**：`QTabWidget` 四個分頁，用分頁切換取代原本「部位表格＋彈出視窗」的做法：
- **「資金流向分析」**（第一個加入的分頁，預設頁）：`FlowChartWidget`（`pg.PlotWidget` 子類別）內嵌泡泡圖，工具列有「重新整理」與「流向天數」下拉選單（`QComboBox`，5／10／20 日，`itemData` 存整數天數）。X 軸＝近 N 日累積漲跌%、Y 軸＝今日量／近 N 日均量（虛線標出 0% 與量比=1 的基準線，N 由下拉選單決定，兩軸定義見 `compute_industry_flow`）、泡泡大小＝成交金額、標籤＝產業名稱；資料不足的產業會被跳過並在標題註記數量，不會讓整張圖 crash。**縮放／平移**：pyqtgraph `ViewBox` 內建滑鼠滾輪縮放、拖曳平移；`FlowChartWidget.event()` 額外攔截 macOS 觸控板雙指捏合手勢（`QEvent.NativeGesture` + `Qt.NativeGestureType.ZoomNativeGesture`），以游標位置為中心呼叫 `ViewBox.scaleBy()`，讓觸控板兩指捲動（走滾輪事件）與雙指捏合（走原生手勢）都能平滑縮放。**點擊泡泡查成分股**：每個產業的 `pg.ScatterPlotItem` 都接了 `sigClicked`，點擊後彈出 `IndustryTopStocksDialog`（小型 `QDialog`，非同步、不佔用主畫面空間），內容呼叫 `get_industry_top_stocks()` 顯示該產業成交金額前十大成分股（代號／名稱／現價／漲跌%／成交金額，漲跌%依正負著色），表格高度依列數自動調整（上限 320px）。純本地資料，開啟即顯示、不打 API。
- **「部位紀錄」**：`QTableWidget` 表格列出所有部位（代號／名稱／股數／成本價／現價／損益／損益%／備註），損益依正負著色（綠漲紅跌，用 `QTableWidgetItem.setForeground()`），工具列：新增部位、刪除部位、查價、重新整理。
- **「個股」**：`QTreeWidget` 依產業族群列出全市場股票（頂層節點＝產業，展開後子節點＝個股：名稱／代號／現價／漲跌%），資料來源是 `get_industry_directory()`（`tradingnote_history.py`，7日快取）交叉 `self.snapshot` 目前的整市場收盤價快照——只用既有本地資料，不會為了列清單額外打 FinMind。雙擊某檔個股才會叫 `StockDetailDialog` 用 FinMind API（`fetch_valuation`／`fetch_institutional_investors`）查最新本益比／殖利率／股價淨值比與三大法人買賣超，查詢在背景執行緒跑（`run_task_in_thread`）避免網路延遲卡住視窗；child item 用 `setData(0, Qt.UserRole, ticker)` 存代號，`_on_stock_double_clicked` 用這個判斷點到的是個股列還是產業分組列（分組列沒有這個 data，直接忽略）。
- **「設定」**：資料庫維護與 API 設定集中在這裡，跟看盤/紀錄的日常操作分開。內容：①「每次啟動自動檢測」勾選框（`QCheckBox`，綁 `auto_check_continuity`），切換時立即寫回 `settings.json`；②「回補歷史資料」按鈕（原本在資金流向分析頁的工具列，移過來這裡，行為不變）；③ FinMind API Token 輸入框（`QLineEdit`，預設用密碼遮罩顯示，旁邊有「顯示」勾選框可切換明文），`editingFinished` 時寫回 `settings.json` 的 `finmind_token` 欄位，「個股」分頁雙擊查詢時讀這個值。

**視覺主題**：暖米色背景（`#F5F4ED`）＋赤陶色重點色（`#CC785C`），透過 `QApplication.setStyleSheet()` 套用單一份 QSS 字串（`STYLESHEET`），涵蓋分頁、按鈕、表格、輸入框、狀態列；`pg.setConfigOptions(background=..., foreground=...)` 讓 pyqtgraph 圖表底色與軸線顏色跟著一致。主要操作按鈕（新增部位、確認、查詢、重新整理、回補歷史資料）用 `accent_button()` 建立，透過 Qt 動態屬性 `accent=true` 套用赤陶色填色樣式，次要操作維持中性外框樣式。

狀態列（`QStatusBar`，分頁下方，四頁共用）顯示價格更新時間、總損益，以及背景資料連續性同步的進度（有需要才會顯示）。

- 「回補歷史資料」開一個進度視窗（`BackfillDialog`，`QDialog`），在背景執行緒跑 `backfill_twse_history`（若有明顯缺口，約需數十次請求＋節流延遲；已連續則秒級完成），完成後自動重繪資金流向頁的圖。
- **啟動時的資料連續性自動同步**：`TradingNoteWindow.__init__` 讀取 `settings.json` 的 `auto_check_continuity`（預設開），開啟時才會呼叫 `_sync_history_continuity()`，用跟 `BackfillDialog` 相同的 `run_backfill_in_thread()` 共用機制在背景執行緒跑，但不彈出對話框——只在狀態列顯示簡短進度文字，完成後若真的補了新資料才會重繪資金流向圖。
- **並發保護**：啟動自動同步跟使用者手動點「回補歷史資料」有可能前後腳觸發（例如剛開程式就馬上去設定頁點按鈕），兩者都走同一個 `backfill_twse_history`，該函式內部用 `_backfill_lock` 序列化，所以即使真的同時觸發也不會互相卡住，只是第二個會排隊等第一個做完（已用兩個並發執行緒實際測過：加鎖前會直接卡死其中一個，加鎖後兩個都能正常結束）。
- 查價視窗（`PriceLookupDialog`）跟 CLI 一樣，即時快照查無資料時會 fallback 到 `get_latest_ticker_record`（LIFO，`ORDER BY date DESC`），並標註資料來自歷史資料庫。
- **「重新整理」**（資金流向分析／部位紀錄頁共用同一個 `force_refresh`）：點擊後彈出 `RefreshDialog`（`QDialog`），在背景執行緒依序執行「連線 TWSE／TPEX 取得即時報價」（`get_market_snapshot(force_refresh=True)`）與「寫入歷史資料庫」（`record_snapshot`）兩個階段，狀態文字即時更新，避免網路延遲時整個視窗看起來像當掉。完成或失敗都會呼叫 `on_complete(snapshot, error)`：成功時 `TradingNoteWindow._apply_refresh_result` 套用新快照並清空 `last_error`，重繪三個分頁；失敗時保留舊快照、只更新 `last_error`（跟原本同步版本行為一致），使用者需按「關閉」手動關掉視窗（跟 `BackfillDialog` 同一套「完成後仍留著讓使用者看結果」的 UX）。
- `run_backfill_in_thread(parent, progress_cb, done_cb, error_cb)`：抽出的共用背景執行緒＋`queue.Queue`＋`QTimer` 輪詢機制（相當於 Tkinter 版的 `root.after` 輪詢，改用 Qt 的計時器），`BackfillDialog`（手動、彈窗）與啟動時的自動同步（靜默、狀態列）都建立在這個函式上，避免重複的 threading 邏輯。
- `run_task_in_thread(parent, work_fn, on_done, on_error)`：跟 `run_backfill_in_thread` 同一套背景執行緒＋queue＋QTimer 輪詢機制，但拿掉 progress 中間狀態，只有「完成／失敗」兩種結果，給 `StockDetailDialog` 這種一次性查詢用，避免重複實作一次 threading 邏輯。
- `run_refresh_in_thread(parent, progress_cb, done_cb, error_cb)`：跟前兩者同一套背景執行緒＋queue＋QTimer 輪詢機制，但 `progress_cb` 傳的是階段文字（連線中／寫入中）而非數字進度或純完成/失敗，專給 `RefreshDialog` 用。

---

## 6. `啟動TradingNote.command`

```bash
#!/bin/bash
cd "$(dirname "$0")"
/opt/homebrew/bin/python3.12 tradingnote_gui.py
```

改用 PySide6 後，原本「系統內建 `/usr/bin/python3` 的 Tk 8.5 會讓 GUI 黑屏」這個限制已不適用（PySide6 不依賴 Tcl/Tk）。仍然固定用 Homebrew 的 `/opt/homebrew/bin/python3.12`，純粹是因為 `PySide6`／`pyqtgraph` 是安裝在這個直譯器的使用者站台目錄下（見上方「依賴」一節的安裝指令）；若要改用系統 `/usr/bin/python3`，需要對著它重新 `pip install --user PySide6 pyqtgraph`。

---

## 資料流總覽

```
tradingnote_core.get_market_snapshot()  ──擷取／快取──►  data/price_cache.json（台股全市場收盤價）
tradingnote_core.load_positions()/save_positions()  ◄────►  data/positions.json（部位紀錄）
tradingnote_core.load_settings()/save_settings()    ◄────►  data/settings.json（auto_check_continuity／finmind_token，CLI／GUI 共用）
        │
        ├─→ record_snapshot(snapshot)  ──逐日累積──►  data/history.db :: daily_prices（TPEX 唯一資料來源，WAL 模式）
        │        ▲
        │        ├── backfill_twse_history()  ──手動一次回補120天（backfill 指令／設定頁按鈕）──►  daily_prices（僅 TWSE）
        │        └── backfill_twse_history()  ──設定為開時，每次啟動自動補缺口（靜默，僅 TWSE）──►  daily_prices（資料連續性保證）
        │            （兩者共用 `_backfill_lock`，同一行程內序列化，不會同時寫入互相卡住）
        │
        ├─→ get_industry_map() / get_industry_directory()  ──7日TTL快取──►  data/history.db :: industry_map（產業分類，後者多帶 name／market，供「個股」分頁分組用）
        │
        ├─→ get_latest_ticker_record(ticker)  ──LIFO／(ticker, date DESC) 索引──  即時快照查無某股票時的備援來源
        │
        ├─→ tradingnote_finmind.fetch_valuation() / fetch_institutional_investors()  ──雙擊個股才呼叫──►  FinMind API（本益比／殖利率／三大法人買賣超，不落地存檔，即查即顯示）
        │
        ├─→ tradingnote.py         → 終端機表格 + 指令列互動（flow／backfill 指令，price 有 LIFO fallback）
        │
        └─→ tradingnote_gui.py     → QTabWidget 分頁切換
                 ├─ 資金流向分析（預設頁，pyqtgraph 內嵌泡泡圖，滾輪/觸控板可縮放平移、5/10/20日切換、點擊泡泡看前十大成分股 + 重新整理）
                 ├─ 部位紀錄（QTableWidget 表格 + 新增／刪除／查價［LIFO fallback］／重新整理）
                 ├─ 個股（QTreeWidget 依產業族群列出全市場股票 + 雙擊叫 FinMind 查詳細資料）
                 └─ 設定（每次啟動自動檢測開關、回補歷史資料按鈕、FinMind token 輸入框）

compute_pnl(position, lookup_price(position.ticker, snapshot)) → 部位損益分析
compute_industry_flow(db_path, snapshot, avg_days) → 產業資金流向（泡泡圖／flow 指令的資料來源，avg_days 同時決定 X/Y 兩軸天數）
get_industry_top_stocks(db_path, snapshot, industry) → 該產業成交金額前十大成分股（點擊泡泡時的資料來源，市值資料的代理指標）
```
