# tradingnote 交接摘要

## 專案位置
`/Users/zhengyufan/Claude/tradingnote`（獨立新專案，與同層的 `/Users/zhengyufan/Claude/trade-journal` 完全無關、無資料共用）

## 這是什麼
台股部位紀錄＋產業資金流向＋個股基本面查詢程式，架構參考 `/Users/zhengyufan/Claude/GG/ARCHITECTURE.md`（數獨小遊戲）的「核心邏輯與介面分離」原則：核心模組不依賴任何 UI，CLI 與 GUI 各自 import。**細節一律以 `ARCHITECTURE.md` 為準**，這份 HANDOFF 只是快速摘要，兩者有出入時看 `ARCHITECTURE.md`。

```
tradingnote/
├── tradingnote_core.py       # 核心：部位模型、JSON 持久化、TWSE/TPEX 查價、損益計算、settings.json
├── tradingnote_history.py    # 核心：120日歷史 SQLite、產業分類、產業資金流向分析
├── tradingnote_finmind.py    # 核心：FinMind API（本益比／殖利率／三大法人買賣超），僅 GUI 使用
├── tradingnote.py             # CLI（無「個股」模組對應指令）
├── tradingnote_gui.py         # GUI：PySide6 + pyqtgraph（原本是 Tkinter + matplotlib，已整個換掉）
├── 啟動TradingNote.command    # 雙擊啟動 GUI，固定用 /opt/homebrew/bin/python3.12
├── ARCHITECTURE.md             # 本專案架構文件（已同步更新，細節見此）
└── data/                       # 執行時自動建立
    ├── positions.json          # 使用者紀錄的部位
    ├── price_cache.json        # 全市場收盤價快取（30分鐘 TTL）
    ├── settings.json           # auto_check_continuity、finmind_token（GUI「設定」分頁寫入）
    └── history.db              # SQLite，約 22MB：120日歷史價格 + 產業分類快取
```

## 目前狀態：GUI 已從 Tkinter 整個換成 PySide6 + pyqtgraph
這是這次對話做的最大變動，之前的 HANDOFF 版本還停留在 Tkinter 階段，已經過時：

- **換框架的原因**：使用者要求圖表能用觸控板縮放（pinch-to-zoom／兩指捲動），matplotlib 內嵌畫布做不到這種互動，改用 pyqtgraph 的 `ViewBox`（原生支援滾輪縮放/拖曳平移）+ 額外攔截 macOS 觸控板原生手勢（`QEvent.NativeGesture`）。
- **視覺主題**：暖米色背景＋赤陶色重點色（呼應 Claude 介面配色），透過 `QApplication.setStyleSheet()` 套用；pyqtgraph 圖表底色也對應調整過。
- **GUI 四個分頁**：
  1. 資金流向分析（預設頁）：pyqtgraph 泡泡圖，可觸控板/滾輪縮放平移
  2. 部位紀錄：`QTableWidget`，新增/刪除/查價/重新整理
  3. **個股（這次新加的模組）**：`QTreeWidget` 依產業族群列出全市場約1700檔股票（現價/漲跌%，資料來自本地快取，不額外打 API），雙擊某檔才會叫 FinMind API 查最新本益比/殖利率/股價淨值比/三大法人買賣超
  4. 設定：自動檢測開關、回補歷史資料按鈕、**FinMind API Token 輸入框**（密碼遮罩＋顯示切換）
- **依賴**：GUI 需要 `PySide6` + `pyqtgraph`（已裝在 `/opt/homebrew/bin/python3.12` 的使用者站台目錄）。`matplotlib` 不再被 GUI 使用；`tradingnote_core.py`／`tradingnote_history.py`／`tradingnote_finmind.py`／CLI 仍然零依賴。
- **FinMind 整合**：`tradingnote_finmind.py`，兩個函式 `fetch_valuation()`／`fetch_institutional_investors()`，純 `urllib` 打 `https://api.finmindtrade.com/api/v4/data`，已用真實股票代號 2330 測試過兩個資料集都正常回傳。**Token 我沒有拿到、也沒有寫進 `settings.json`**——使用者說已經有 FinMind 帳號的 token，但選擇直接在 GUI 的「設定」分頁輸入，不透過對話貼給我（比較安全的做法）。目前 `data/settings.json` 裡還沒有 `finmind_token` 這個 key，代表使用者還沒開過新版 GUI 去輸入它。
- **已驗證**：語法檢查、`import` 檢查、無頭（`QT_QPA_PLATFORM=offscreen`）啟動測試個股分頁分組（35個產業族群、正確欄位、ticker 資料綁定）、FinMind 兩個端點對 2330 實測都成功。**沒有**驗證過的：實際觸控板 pinch 手勢、個股分頁雙擊彈窗在真實螢幕上的視覺效果（測試時使用者螢幕在使用中，自動截圖搶不到前景視窗，改用 headless 驗證邏輯正確性）。

## 尚未做 / 刻意留白
- 沒有寫任何自動化測試（unit test），驗證方式都是手動跑 + headless smoke test。
- 只有 EOD（收盤）資料，非即時報價（刻意選擇，即時 API 需要 session/referer 處理）。
- CLI 沒有「個股」模組的對應指令，FinMind 查詢目前只有 GUI 在用。
- FinMind token 尚未輸入到 `settings.json`，且免費額度有限（約每小時數百次請求，部分資料集需登入才查得到）。
- 沒有 requirements.txt／venv，GUI 依賴（PySide6、pyqtgraph）直接裝在 Homebrew python3.12 的使用者站台目錄。

## 這次對話的其他變動（跟 tradingnote 無關，但影響環境）
- **`~/.zshrc` 加了 `cd ~/Claude`**：新開的 Terminal 互動式 shell 現在預設會停在 `~/Claude`（原本落在 `~/Claude/GG`），純粹是 shell 層級的改動，Claude Code CLI 本身沒有「預設目錄」設定。
- **`claude-code` 已透過 `brew upgrade claude-code` 升級**：2.1.206 → 2.1.212，需要開新的 session 才會生效（這也是為什麼會需要這份交接文件）。

## 如果要繼續開發，建議先讀
1. `/Users/zhengyufan/Claude/tradingnote/ARCHITECTURE.md`（本專案架構文件，最新最完整）
2. `/Users/zhengyufan/Claude/tradingnote/tradingnote_gui.py`（GUI 全貌，這次改動最大的檔案）
3. `/Users/zhengyufan/Claude/tradingnote/tradingnote_finmind.py`（新模組，程式碼很短）
