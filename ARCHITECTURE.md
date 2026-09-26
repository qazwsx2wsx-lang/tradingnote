# tradingnote - 程式架構

**維護原則**：這份文件只記錄「為什麼這樣設計」的穩定架構決策——模組邊界、依賴方向、資料流、儲存結構、跨模組共用慣例。這份文件之前爛掉的原因是拿它同時當「檔案清單」跟「功能清單」維護，兩者都隨每個新功能變動，結果沒人跟得上更新。現在拆開：

- **檔案清單、GUI 現況、已知缺口** → 看 [`STATUS.md`](STATUS.md)（每次交接都會更新，這裡不重複列）
- **某次改動的動機／改法／驗證細節** → 看 [`CHANGELOG.md`](CHANGELOG.md)
- **這裡只放**：加一層抽象、換掉儲存方式、改變依賴方向等級的決策——加一個新函式、新分頁、新資料集通常不需要動這份文件。

---

## 設計原則：核心邏輯與介面分離

核心模組（部位模型、API 呼叫、SQLite、計算邏輯）完全不 import 任何 UI 套件、不依賴 `PySide6`；CLI（`tradingnote.py`）與 GUI（`tradingnote_gui.py`）各自 import 同一批核心模組，各自實作顯示與互動方式。這個原則從專案一開始（Tkinter 版）就存在，換成 PySide6 + pyqtgraph 之後依然成立——只有介面層換了套件，核心層完全沒被影響。

**依賴**：核心模組（`tradingnote_core.py`／`tradingnote_history.py`／`tradingnote_institutional.py`／`tradingnote_finmind.py`／`tradingnote_taifex.py`／`tradingnote_technical.py`／`tradingnote_journal.py`／`tradingnote_concepts.py`／`tradingnote_http.py`／`tradingnote_cache.py`／`tradingnote_paths.py`／`tradingnote_api_config.py`）與 CLI 全部零外部依賴（純 stdlib：`urllib`／`sqlite3`／`json`）。只有 `tradingnote_gui.py` 跟 `tradingnote_tasks.py`（GUI 專用的背景執行緒 helper）需要 `PySide6`＋`pyqtgraph`。

**執行環境**：專案在自己的 `.venv` 裡跑（Windows 上是 `.venv/Scripts/python.exe`，之前 macOS 上是 Homebrew `python3.12`）。**不要用系統內建的 `python`**——實測系統 Python 3.14 對 TLS 憑證鏈驗證變嚴格，TWSE／TPEX 的憑證缺少 Subject Key Identifier 擴充欄位會導致 `SSL: CERTIFICATE_VERIFY_FAILED`，這不是 API 本身的問題，只有用專案 `.venv`（3.12.10）才會穩定成功。

---

## 分層與依賴方向

```
外部 API（TWSE／TPEx／FinMind／TAIFEX）
        │  端點網址集中在 tradingnote_api_config.py（純常數，零依賴，任何模組都能安全 import）
        ▼
共用底層工具（無業務邏輯，全部核心模組共用）
  tradingnote_http.py    HTTP JSON／POST 表單抓取、數值解析、PriceFetchError
  tradingnote_cache.py   檔案 JSON TTL 快取／記憶體 TTL 快取／fetch_with_file_cache
                         （fetch→cache→失敗退回舊快取 這套流程的共用 helper）
  tradingnote_paths.py   AppPaths：CLI／GUI 共用的檔案路徑，唯一定義 data/ 底下每個檔名的地方
  tradingnote_tasks.py   GUI 專用：BackgroundTask／run_background_task
        │
        ▼
核心業務模組（不依賴任何 UI，彼此之間允許互相 import，但方向要單向、不能循環）
  tradingnote_core.py          部位模型／損益／TWSE-TPEX 即時查價／settings.json
  tradingnote_history.py       股票歷史 SQLite／產業分類／資金流向與估值計算／量比異常
  tradingnote_institutional.py 全市場三大法人買賣超（獨立於 history.py，只共用 http/cache）
  tradingnote_institutional_history.py 三大法人逐日歷史＋類股指標預先計算（寫 daily_prices 只透過 history 的公開函式）
  tradingnote_flow.py          資金流向的期間模型＋儀表板結果＋分析服務（整合 history 的計算函式，加一層快取）
  tradingnote_technical.py     技術指標計算（純數學，吃 OHLCV list，不碰網路／DB）
  tradingnote_finmind.py       FinMind API（需 token；import tradingnote_core 的估值函式做上櫃 fallback）
  tradingnote_taifex.py        TAIFEX 期貨行情＋大額交易人未沖銷部位（含自己的歷史表，見下方「資料儲存」）
  tradingnote_journal.py       交易週誌／持股週曆（自己的資料表，不依賴 history.py／finmind.py）
  tradingnote_concepts.py      概念股分類（讀 concepts.json，人工維護，不打任何 API）
        │
        ▼
介面層（各自 import 上面所有需要的核心模組）
  tradingnote.py       CLI
  tradingnote_gui.py   GUI（唯一 import tradingnote_tasks.py 的地方）
  scripts/*.py         排程／回補腳本（backfill、fetch_daily、recompute）
  web/                 Next.js「法人資金流去哪？」：唯讀開啟 history.db，只查預先算好的表，不含任何計算
```

**依賴方向規則**：核心模組之間可以互相 import（例如 `tradingnote_taifex.py` import `tradingnote_history.py` 的交易日曆函式），但**一個模組管理的 SQLite 表，讀寫函式要放在它自己的檔案裡**，不能讓別的模組把「自己表的 schema／CRUD」定義在別人的檔案裡再回頭 import——這條規則是這次審查修過的教訓：`large_traders_history`（期貨資料）原本連 schema 帶讀寫都定義在 `tradingnote_history.py`（股票歷史模組）裡，`tradingnote_taifex.py` 只是把函式 import 回來用，依賴方向是反的；已經搬正（見 `CHANGELOG.md` 2026-09-15）。

---

## 各模組職責

### 共用底層工具
- **`tradingnote_http.py`**：`http_get_json`／`http_post_text`／`to_float`／`to_int`／`PriceFetchError`。所有對外 HTTP 呼叫唯一入口，沒有內建重試（`tradingnote_institutional.py` 目前自己包了一層重試，是已知的不一致，見「已知架構債務」）。
- **`tradingnote_cache.py`**：`load_fresh_file_cache`／`load_stale_file_cache`／`write_file_cache`（檔案 JSON TTL 快取的讀/寫原語）、`fetch_with_file_cache`（把「讀新鮮快取→過期重抓→失敗退回舊快取」整套流程收成一個 helper）、`TTLCache`（行程內記憶體快取，`get_or_fetch(key, fetch_fn)`）、`load_keyed_store`／`save_keyed_entry`（以 key 分開存放、永久保留的 JSON 檔，例如逐股票查詢結果快取）。
- **`tradingnote_paths.py`**：`AppPaths` dataclass + `get_app_paths()`，`data/` 底下每個檔案的路徑只在這裡定義一次；支援 `TRADINGNOTE_DATA_DIR` 環境變數覆寫（測試用隔離資料目錄）。
- **`tradingnote_tasks.py`**：`BackgroundTask`／`run_background_task(parent, work_fn, on_done, on_error, on_progress)`。GUI 所有背景執行緒（回補、重新整理、查詢）都走這一條路徑，背景執行緒本身不碰任何 widget，只透過 `queue.Queue` 回報，由 `QTimer` 在 Qt 執行緒輪詢取出結果——避免跨執行緒直接操作 UI。
- **`tradingnote_api_config.py`**：純常數，全部外部端點網址集中在這，官方端點改版時只需要改這一個檔案。

### 核心業務模組
- **`tradingnote_core.py`**：`Position`／`PriceInfo` 資料模型、`positions.json`／`settings.json` 讀寫、`get_market_snapshot()`（TWSE+TPEX 即時報價，走 `fetch_with_file_cache` 風格的 fallback，但因為有多階段 `on_progress` 需求所以沒有直接呼叫共用 helper，自己手刻一份）、`compute_pnl()`。
- **`tradingnote_history.py`**（目前最大的核心模組，約1400行）：`data/history.db` 的股票歷史相關表格（`daily_prices`／`industry_map`／`valuation_history`）schema 與讀寫、TWSE 歷史回補、產業分類抓取與快取、資金流向與估值流向的百分位/加權中位數計算、個股量比異常偵測。**注意**：期貨的 `large_traders_history` 表**不在**這裡，在 `tradingnote_taifex.py`。
- **`tradingnote_institutional.py`**：全市場三大法人買賣超（TWSE T86 + TPEx `tpex_3insti_daily_trading`），依族群聚合，供「法人方向」頁用；跟 `tradingnote_history.py` 是平行關係，不互相依賴。
- **`tradingnote_institutional_history.py`**：可帶日期的 T86／BFI82U／櫃買新版 `/www/zh-tw/` 端點逐日回補（3 秒間隔、非交易日記錄在 `institutional_calendar`），把近 5／20 日、加速流入、連續買賣等跨日指標預先算進 `sector_metrics`／`stock_metrics`。**計算只在 Python 做**，`web/` 只查表——避免同一套指標在 Python 與 TypeScript 各寫一份。`aggregate_group_flow_for_dates()` 給 `tradingnote_flow.py` 用，流向區間每天都有歷史時「主力同步買超」改用區間加總。
- **`tradingnote_flow.py`**：把 `tradingnote_history.py` 的多個計算函式（產業流向、估值流向、個股排行）包成一個 `FlowAnalysisService`，GUI 刷新時只建一次儀表板資料，同批查詢的多個子頁面共用同一份結果，避免同一次刷新重複計算。
- **`tradingnote_technical.py`**：24 類技術指標的純函式計算（MA/EMA/KD/MACD/RSI/布林通道/OBV/VPT/MFI…），輸入是共用的 OHLCV bars，不觸網、不觸 DB，方便獨立測試（`test_tradingnote_technical.py`）。
- **`tradingnote_finmind.py`**（約 700 行，第二大核心模組）：FinMind v4 API 的所有資料集查詢（本益比、三大法人、融資融券、外資持股、借券、技術指標用價格序列），統一走 `TTLCache` 做行程內快取，額度統計（`get_call_count`）供 GUI 判斷是否該提早停止批次回補。
- **`tradingnote_taifex.py`**：TAIFEX 期貨每日行情、大額交易人未沖銷部位（即時＋歷史，`large_traders_history` 表的 schema 跟讀寫都在這裡）、股票期貨標的對照。
- **`tradingnote_journal.py`**：交易週誌與持股週曆，自己的 4 張表（`journal_meta`／`journal_entries`／`portfolio_snapshots`／`portfolio_snapshot_positions`），獨立的 `_connect()`，跟其他模組的表互不干涉。
- **`tradingnote_concepts.py`**：讀 `concepts.json`（人工維護的概念股清單），不打任何 API。

### 介面層
- **`tradingnote.py`**（CLI）：文字介面，目前沒有「個股」「期貨」模組的對應指令（那兩個核心模組只有 GUI 在用）。
- **`tradingnote_gui.py`**（約 4700 行，全專案最大檔案）：PySide6 主視窗，左側導覽列（2026-09-14 從上方分頁改版）+ 右側 `QStackedWidget`，六個分頁：資金流向分析（含法人方向子頁）、部位紀錄、交易週誌、個股查詢、期貨行情、設定。目前是最需要持續拆分/去重的檔案，見「已知架構債務」。

---

## 資料儲存

### JSON 檔案（`data/`，git 忽略，路徑定義見 `tradingnote_paths.py`）
| 檔案 | 內容 | 讀寫模組 |
|---|---|---|
| `positions.json` | 使用者部位紀錄 | `tradingnote_core.py` |
| `settings.json` | `auto_check_continuity`／`finmind_token`／`backfill_target_days` 等本機設定 | `tradingnote_core.py` |
| `price_cache.json` | 全市場收盤價快取（30分鐘 TTL） | `tradingnote_core.py` |
| `futures_cache.json`／`futures_large_traders_cache.json`／`futures_ssf_cache.json` | 期貨相關快取 | `tradingnote_taifex.py` |
| `institutional_cache.json` | 三大法人快照快取 | `tradingnote_institutional.py` |
| `position_detail_cache.json` | 逐股票 FinMind 查詢結果永久快取（`load_keyed_store` 風格） | `tradingnote_finmind.py` |

### SQLite（`data/history.db`，單一檔案，多個模組各自管各自的表）
| 表 | 內容 | 擁有模組 |
|---|---|---|
| `daily_prices`／`industry_map`／`valuation_history` | 股票歷史價格／產業分類／估值歷史 | `tradingnote_history.py` |
| `large_traders_history` | 期貨大額交易人未沖銷部位歷史 | `tradingnote_taifex.py` |
| `daily_institutional`／`market_summary`／`institutional_calendar`／`sector_map`／`sector_metrics`／`stock_metrics` | 三大法人歷史、交易所公布金額、類股對照與預先計算指標 | `tradingnote_institutional_history.py` |
| `journal_meta`／`journal_entries`／`portfolio_snapshots`／`portfolio_snapshot_positions` | 交易週誌與持股快照 | `tradingnote_journal.py` |

**共用同一個實體檔案，但 schema／連線各自獨立**：每個擁有模組都有自己的 `_connect(db_path)`，各自用 `CREATE TABLE IF NOT EXISTS` 建自己的表，一律 `PRAGMA journal_mode=WAL` + `timeout=30`（多執行緒/多行程同時讀寫同一個檔案時，WAL 讓讀者不擋寫者、`timeout` 給排隊的寫入緩衝）。三個擁有模組彼此不 import 對方的 `_connect()`，只有需要跨模組查資料時才 import 對方暴露出來的讀寫函式（例如 `tradingnote_taifex.py` 會 import `tradingnote_history.py` 的交易日期輔助函式，但不會碰它的表）。

---

## 跨模組共用慣例

- **背景執行緒**：GUI 一律用 `tradingnote_tasks.run_background_task()`，不要自己刻 `threading.Thread` + 手動輪詢。**一定要把回傳值存到一個會存活到任務完成的地方**（慣例是 `self._xxx_timer = run_background_task(...)`，這個名字沿用的是舊版實作，現在存的其實是 `BackgroundTask` 物件本身，不是 timer）——`BackgroundTask` 內部用來輪詢佇列的 `QTimer` 是它自己的屬性，如果呼叫端不留住回傳值，這個物件在背景執行緒做完之前就可能被 Python 回收，`on_done`／`on_error` 永遠不會被呼叫，而且不會拋例外、不會有任何錯誤訊息，是純粹的靜默失敗（2026-09-16 效能修正時實測發現：在事件迴圈裡直接寫 `run_background_task(self, work, on_done, on_error)` 而不接回傳值，`work_fn` 明明跑完了，`on_done` 卻永遠不會觸發）。現有每一處呼叫都遵守這個慣例，新增呼叫點時比照辦理。
- **檔案快取 fallback**：新的「打 API、寫入快取、失敗退回舊快取」需求，先看 `tradingnote_cache.fetch_with_file_cache()` 能不能直接用；只有像 `get_market_snapshot()` 那種需要多階段 progress callback 的特例才手刻。
- **LIFO 存取**：查詢單一股票的最新歷史資料，一律 `ORDER BY date DESC` + 專屬的 `(ticker, date DESC)` 索引，不要對 `(date, ticker)` 主鍵索引做「依 ticker 查」的查詢（會退化成全表掃描）。
- **一個模組管一張表**：見上方「資料儲存」——新增 SQLite 表時，schema 跟讀寫函式要放在邏輯上擁有這份資料的模組裡，不要因為「剛好在改哪個檔案」就近放。

---

## 已知架構債務

（2026-09-15 架構審查發現，尚未處理——找得到就直接修，不用等這份文件更新）

- `tradingnote_gui.py` 內 9 個 `QDialog` 子類別重複視窗樣板碼（放大鈕 flag + 螢幕適配尺寸），沒有共用 base class。
- 紅綠漲跌上色邏輯（`value >= 0 ? 綠 : 紅`）在 `tradingnote_gui.py` 裡重複了十幾次，沒有共用 helper。
- `tradingnote_history.py` 內至少 5 個函式（`compute_group_flow`／`compute_group_valuation_flow`／`get_group_top_stocks_range` 等）各自重寫「載入 N 天歷史 + 算 cutoff 日期」的 SQL 查詢邏輯。
- `tradingnote_institutional._http_get_json_retry()` 自己包一層重試，`tradingnote_http.http_get_json()` 本身沒有重試選項——不一致，其他打大型 JSON 端點的模組（history／finmind／taifex）完全沒重試。
- `tradingnote_technical._number()` 跟 `tradingnote_http.to_float()` 邏輯幾乎一樣（多一個 `math.isfinite` 檢查），可以合併。

詳細討論見對話紀錄，或跟 Claude／Codex 重新問一次「協助檢查架構」。
