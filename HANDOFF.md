# tradingnote 交接摘要

## 必讀簡介
新 session 開頭只需要讀這一段（原則上不超過 20 行）；下面「詳細內容」是備查用，需要細節才展開讀，省 token。**每次交接前，把最新、最需要注意的事更新到這一段，並保持精簡**——細節寫進下方對應章節，不要塞在這裡。

- **這是什麼**：台股部位紀錄＋產業資金流向＋個股基本面查詢＋期貨盤後行情＋AI 問答的桌面程式（PySide6 + pyqtgraph）。位置：`/Users/zhengyufan/Claude/tradingnote`，跟同層 `trade-journal` 無關。
- **Git 狀態**：`main` branch，領先 `origin/main` 4 個已 commit（`d93cb86`／`b417810`／`d2ffe90`／`3cdf6be`，尚未 push），另外還有本次對話**尚未 commit**的異動：`tradingnote_finmind.py`／`tradingnote_gui.py`／`tradingnote_history.py` 修改，新增 `tradingnote_concepts.py`／`concepts.json`。三批改動都已驗證：①「部位紀錄」頁族群/概念股/FinMind 120日三大法人趨勢圖；②「資金流向分析」頁「流向天數」「資料區間」改成日曆式起始日期選擇（沒資料的日期反白不能選）；③「部位紀錄」頁新增融資融券餘額／外資持股比例／借券成交／停資停券籌碼面資訊（同樣改 `tradingnote_finmind.py`／`tradingnote_gui.py`，沒有新檔案），見下方「變更歷史 → 2026-08-08」三則。已用真實網路＋真實 `data/positions.json`／`data/history.db` headless 驗證過，工作目錄除了這批新異動外是乾淨的。
- **只是要接著開發新功能**：讀到這裡就夠了。要動到 GUI 分頁結構、期貨模組、概念股清單、或想知道更早的變更緣由，才需要往下展開「詳細內容」。

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
├── tradingnote_finmind.py    # 核心：FinMind API（本益比／殖利率／三大法人買賣超，含120日歷史），僅 GUI 使用
├── tradingnote_concepts.py   # 核心：概念股分類（讀 concepts.json，人工維護、非API資料），僅 GUI「部位紀錄」頁使用
├── concepts.json              # 人工維護的概念股清單，跟 trade-journal/themes.json 無關、不共用
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
1. **資金流向分析**（預設頁）：pyqtgraph 泡泡圖（大小＝資金比重%），可觸控板/滾輪縮放平移，點擊泡泡看該產業前十大成分股；下方有「資金動向清單」（產業層級）跟「個股量比異常清單」（個股層級，2026-08-05 新增）兩個表格。泡泡圖「流向區間」與清單「資料區間」都是日曆式起始日期選擇器（點按鈕開 `TradingDateDialog`，見下方「變更歷史 → 2026-08-08」），沒有歷史資料的日期反白不能選，結束日固定是今天／最新資料。
2. **部位紀錄**：`QTableWidget`（含族群／概念股欄位，本地資料即時顯示），新增/刪除/查價/重新整理；表格下方是 `QSplitter` 分隔的詳細資訊區塊，選取某筆部位時背景查 FinMind 顯示本益比/殖利率/股價淨值比，以及三大法人（外資/投信/自營商）近120個交易日累計買賣超趨勢圖（`pg.PlotWidget`，三條線）。概念股清單來自專案根目錄 `concepts.json`（人工維護，見 `tradingnote_concepts.py`）。
3. **個股**：`QTreeWidget` 依產業族群列出全市場約1700檔股票（現價/漲跌%，資料來自本地快取），雙擊某檔叫 FinMind API 查最新本益比/殖利率/股價淨值比/三大法人買賣超（`StockDetailDialog`，「個股量比異常清單」雙擊也共用同一個對話框）。
4. **期貨**：TX（臺股期貨）／MTX（小型臺指期貨）近月合約盤後行情，資料來自 TAIFEX 官方免金鑰端點，一天更新一次，非即時。
5. **AI 助理**：Gemini API（`gemini-flash-lite-latest`）自然語言問答，模型自動判斷呼叫股票查詢/本益比/法人買賣等工具，需要「設定」分頁填入 Gemini API Key。
6. **設定**：自動檢測開關、回補天數、回補歷史資料按鈕、FinMind API Token、Gemini API Key（皆密碼遮罩＋顯示切換、`editingFinished` 自動存檔）。

### 變更歷史

#### 2026-08-08：「部位紀錄」頁新增融資融券／外資持股／借券／停資停券籌碼面資訊（尚未 commit）
- **動機**：使用者要求「部位模組新增，以 FinMind 查詢籌碼面變化，能取用的都顯示」——在既有的三大法人 120 日趨勢圖之外，把 FinMind 其他籌碼面資料集也加進「部位紀錄」頁，範圍是「實際能查到的都顯示」，不是照抄 FinMind 文件清單。
- **範圍認定（先實測、再決定要做哪些）**：用真實 token 對 2330 逐一測試 FinMind 籌碼面候選資料集，結果：
  - ✓ 可用：`TaiwanStockMarginPurchaseShortSale`（融資融券）、`TaiwanStockShareholding`（外資持股比例）、`TaiwanStockSecuritiesLending`（借券成交）、`TaiwanStockMarginShortSaleSuspension`（停資停券，測試區間剛好 0 筆，屬正常、非資料集失效）。
  - ✗ 不可用：`TaiwanStockHoldingSharesPer`（股權分散表/大戶持股）——回 HTTP 400「Your level is register. Please update your user level.」，需要 FinMind 付費 Sponsor 方案，免費 token 拿不到，**排除，不實作**。
  - 故意跳過：`TaiwanDailyShortSaleBalances`——雖然可用，但欄位（`SBLShortSales*`／`MarginShortSales*`）跟 `TaiwanStockMarginPurchaseShortSale` 的 `ShortSale*` 系列大量重複，多打一次 API 換不到多少新資訊，不值得多一份 FinMind 額度＋多一塊 UI。
- **改法**：
  - `tradingnote_finmind.py` 新增四個函式（緊接在 `fetch_valuation` 之後）：`fetch_margin_short_sale_history(ticker, token, lookback_days=120)`（融資融券，一次 API 呼叫同時回傳逐日餘額序列給趨勢圖、跟最新一天摘要給文字，不像其他函式拆兩個 lookback_days 各打一次）、`fetch_foreign_shareholding(ticker, token, lookback_days=10)`（外資持股比例＋跟前一筆比較的變化）、`fetch_securities_lending_summary(ticker, token, lookback_days=10)`（借券是逐筆成交資料，同一天可能多筆不同費率，彙總成當天總量＋成交量加權平均費率）、`fetch_margin_short_sale_suspension(ticker, token, lookback_days=90)`（停資停券公告清單，lookback 刻意拉長到 90 天，因為這個資料集本來就很少有資料，區間太短容易漏掉）。融資融券的餘額欄位（`MarginPurchaseTodayBalance`／`ShortSaleTodayBalance`）是「當日餘額」，跟三大法人買賣超那種「當日淨變化」語意不同，畫圖要畫原始值，不能再累加一次。
  - `tradingnote_gui.py`：import 新增的四個函式；`_build_position_detail_section` 改成用 `QScrollArea`（`widgetResizable=True`）包住文字摘要＋兩張圖表（新舊各一），做法跟「資金流向分析」頁 `_build_flow_tab` 既有的 `QScrollArea` 用法一致，因為內容變高，原本 `QSplitter` 分給偵測區塊的高度（stretch 2/5）裝不下，用捲軸瀏覽而不是硬擠壓變形；新增 `self.position_margin_chart`（`pg.PlotWidget`，跟既有 `self.position_flow_chart` 同樣風格）。`_load_position_detail` 的背景 `fetch()` closure 從呼叫 2 個函式擴充成 6 個（原本的 `fetch_valuation`／`fetch_institutional_investors_history`，加上新增的 4 個），回傳 6-tuple；`_on_position_detail_done` 依序 unpack、每個資料集各自處理「查無資料」文案，停資停券只在有公告時才多印一行 `⚠` 警示（平常不佔版面）；新增 `_populate_position_margin_chart`，架構仿照既有 `_populate_position_flow_chart`（x 軸每 `len//8` 天標一次日期），但直接畫原始餘額值、不累加。`_on_position_row_selected`／`_on_position_detail_error` 同步補上清空 `position_margin_chart` 的呼叫。
- **驗證**：`python3 -m py_compile` 全部通過；用 `/opt/homebrew/bin/python3.12` import 兩個改動模組成功。四個新函式對 2330 真實呼叫：融資融券最新（2026-08-07）融資餘額 29,657 張（-292）、融券餘額 33 張（-5）——數值量級確認是「張」不是「股」（若是股，一檔權值股的融資餘額不可能只有兩三萬）；外資持股比例 69.14%；借券成交合計 1,441 張、均費率 0.41%；停資停券真的抓到一筆 2026-06-05～2026-06-10「除息」公告，證實有資料時的呈現路徑正常。headless GUI smoke test（`QT_QPA_PLATFORM=offscreen`，真實 `data/positions.json` 的 `2330`／`2301`、真實網路）：兩檔部位選取後六個段落（基本面／三大法人／融資融券／外資持股／借券／停資停券）都正確出現在文字區塊；`position_flow_chart` 維持 3 條線（三大法人，沒有因為改動而回歸壞掉）、`position_margin_chart` 正確畫出 2 條線（融資／融券餘額）；2330 正確顯示停資停券警示行、2301（測試期間無公告）正確不顯示該行，證實「只在有資料時才顯示」邏輯正確。測試前後比對 `data/positions.json` 內容未被覆寫。
- **尚未 commit**（改動檔案：`tradingnote_finmind.py`、`tradingnote_gui.py`；跟本次對話另外兩則 2026-08-08 change log 是同一批工作階段，沒有新增檔案）。
- **刻意留白／已知限制**：①`TaiwanStockHoldingSharesPer`（大戶持股分布）需要 FinMind 付費方案，目前帳號拿不到，之後若升級方案可以再補；②`TaiwanDailyShortSaleBalances` 因為跟融資融券高度重複，刻意沒做，如果之後發現真的需要 SBL 額度資訊，要另外評估；③這次只加在「部位紀錄」頁，「個股」頁的 `StockDetailDialog`（雙擊個股彈窗）目前還是只有本益比／三大法人，沒有同步加籌碼面，是自然的下一步延伸但這次沒做（使用者這次明確只要求部位模組）；④每次選取部位從原本 2 次 FinMind 呼叫變成 6 次，`FINMIND_HOURLY_LIMIT=600`／小時額度綽綽有餘（個人工具、部位數量通常個位數），沒有另外加節流邏輯。

#### 2026-08-08：「資金流向分析」頁「流向天數」「資料區間」改成日曆式起始日期選擇（尚未 commit）
- **動機**：使用者要求把「流向天數」（泡泡圖）跟「資料區間」（資金動向清單）原本的 QSpinBox（直接輸入天數）改成日曆形式選擇日期區間，且沒有資料的日期要反白不能選。跟使用者確認過範圍：兩個控制都改；結束日固定是今天／最新資料（`compute_industry_flow` 的 `avg_days` 本來就是「今天以前 N 個交易日」的語意，不含今天），只需要日曆選「起始日」，不用真的支援任意起訖區間（那需要改寫 `compute_industry_flow` 本身，範圍較大，這次沒做）。
- **改法**：
  - `tradingnote_history.py` 新增三個函式：`get_available_dates(db_path)`（回傳 `daily_prices` 裡所有出現過的交易日，ISO字串由舊到新）、`trading_days_between(db_path, start_date, end_date=None)`（算 start_date 含～end_date 不含之間有幾個交易日，預設 end_date 是今天）、`default_start_date_for_days(db_path, avg_days)`（avg_days 換算成對應的起始日期，給日期選擇器的預設值用；互為反函式：`trading_days_between(db, default_start_date_for_days(db, N)) == N`，資料足夠時成立）。
  - `tradingnote_gui.py` 新增兩個類別：`TradingCalendarWidget`（繼承 `QCalendarWidget`，建構時傳入 `valid_dates_iso`，用 `setMinimumDate`／`setMaximumDate` 框住整段資料範圍頭尾，範圍內個別沒資料的日期〔例如週末〕用 `setDateTextFormat` 塗灰階；`QCalendarWidget` 沒有原生的「單一日期停用」API，改監聽 `clicked(QDate)` 訊號，點到沒資料的日期就把選取狀態復原回上一個有效日期、不 emit 訊號，點到有資料的日期才 emit 自訂的 `dateChosen(str)` 訊號）、`TradingDateDialog`（包住 `TradingCalendarWidget` 的小視窗，點到有效日期立刻 `accept()` 關閉，不需要另外按確定；`valid_dates_iso` 是空清單時顯示「尚無足夠歷史資料」文字，不會顯示空白日曆）。
  - 泡泡圖「流向天數」QSpinBox+「套用」按鈕，換成一顆按鈕（`flow_date_button`，文字顯示「YYYY-MM-DD 起（近 N 個交易日）」），點擊開 `TradingDateDialog`；選到日期後用 `trading_days_between` 換算成 `self.flow_avg_days`（`compute_industry_flow` 實際吃的參數不變，只是換一種方式決定數值），呼叫 `refresh_flow_tab()`。「資金動向清單」的「資料區間」同樣改法（`list_date_button`／`list_start_date`／`list_avg_days`）。兩者預設值沿用原本的 5 天／20 天，開頁時用 `default_start_date_for_days` 換算成預設起始日期顯示在按鈕上。
  - 拿掉原本「選擇天數超過資料庫實際天數」的警告對話框（`_on_flow_days_apply`／`_on_list_days_apply` 裡原本那段）：因為日曆只讓使用者選「真的有資料」的日期，這個情境已經不可能發生，不需要再另外檢查提醒。
  - 「個股量比異常清單」的「均量天數」（`outlier_days_spin`）**沒有改**——那是計算量比用的滾動平均天數，跟「選一個日期當作區間起點」是不同性質的參數，這次範圍已跟使用者確認過只改前兩個。
- **驗證**：`python3 -m py_compile` 全部通過；`trading_days_between`／`default_start_date_for_days` 互為反函式用真實 `data/history.db`（206個交易日，2025-10-01～2026-08-07）驗證：5天/20天/500天（超過實際天數）三種情況換算後再換回去都對得上（500天的情況正確 fallback 到資料庫最早的 2025-10-01，共206天）。`TradingCalendarWidget` 用 headless（`QT_QPA_PLATFORM=offscreen`）直接測試：①點有資料的日期正確 emit `dateChosen`；②點範圍外（`minimumDate` 之前）的日期不會 emit、選取狀態不變；③範圍內但沒資料的日期（實測抓到 `2025-10-04`，週末）確認 `dateTextFormat` 前景色是灰階（`#767676`＝`COLOR_MUTED`），點擊後不會 emit、選取狀態復原，有資料的日期文字格式維持預設黑色；④`TradingDateDialog` 傳空清單時不會顯示空白日曆，改顯示提示文字，不會壞掉。整合測試：模擬使用者選日期後，`flow_date_button` 文字正確更新、`refresh_flow_tab()` 正常跑完（`flow_list` 產生 35 筆資料，無例外）。
- **尚未 commit**（改動檔案：`tradingnote_gui.py`、`tradingnote_history.py`；不影響上一則 2026-08-08 change log 提到的部位紀錄頁改動，兩批是同一個工作階段但邏輯上獨立）。
- **刻意留白／已知限制**：①只能選「起始日」，結束日固定今天，不支援真正任意的歷史區間查詢（想看「過去某段已結束的區間資金流向」目前做不到，需要改寫 `compute_industry_flow` 改吃歷史收盤價而非即時 `snapshot`，這次沒做）；②`TradingCalendarWidget` 用「點到沒資料的日期就復原選取」模擬「不能選」，不是真正停用該日期的 cell（Qt 沒有這個原生 API），使用者仍會看到滑鼠可以點下去，只是點了沒反應（灰階格子），視覺上跟按鈕/文字被停用的「真正不能選」還是有微妙差異；③兩個日期選擇器（泡泡圖／清單）各自獨立呼叫 `get_available_dates`，資料量大時（目前約200筆）效能無虞，若之後歷史天數大幅拉長（例如未來想存好幾年）可能要考慮加快取。

#### 2026-08-08：「部位紀錄」頁新增族群／概念股／FinMind 三大法人120日趨勢圖（尚未 commit）
- **動機**：使用者要求部位紀錄裡每檔股票都能看到詳細資訊（含 FinMind），以及所屬產業族群、所屬概念股、三大法人120日資金流向，原本「部位紀錄」頁只有基本的股數/成本/損益欄位，要查 FinMind 得切去「個股」頁另外雙擊查詢，且沒有任何概念股分類、也只能看最新一天的三大法人買賣超（無法看趨勢）。
- **概念股資料來源的決定**：FinMind 沒有概念股資料集；同層 `trade-journal/themes.json` 剛好有類似的主題/概念股清單，但 `HANDOFF.md` 明確記載 tradingnote 跟 trade-journal「完全無關、無資料共用」。跟使用者確認後，選擇在 tradingnote 內建立完全獨立的 `concepts.json`（不讀取、不參照 trade-journal 任何檔案），內容是本次對話手動整理的一份把握度較高的龍頭股起點清單（11個族群、約20檔），使用者之後可自行編輯擴充。
- **改法**：
  - 新增 `tradingnote_concepts.py`：`load_concepts()` 讀 `concepts.json`（壞掉/不存在時回傳空清單，不拋例外，因為概念股是輔助資訊）；`build_ticker_concept_map()` 轉成 `{ticker: [concept_name, ...]}`。GUI 啟動時讀一次存在 `self._ticker_concept_map`（跟 settings.json 一樣，改 concepts.json 要重開程式才生效）。
  - `tradingnote_finmind.py` 新增 `fetch_institutional_investors_history(ticker, token, lookback_days=120)`：跟既有的 `fetch_institutional_investors`（只回傳最新一天、五個細分類）不同，這個回傳完整區間、且合併成「三大法人」標準三分類（`INSTITUTIONAL_BUCKETS`：外資=Foreign_Investor+Foreign_Dealer_Self、投信=Investment_Trust、自營商=Dealer_self+Dealer_Hedging+Dealer舊制），格式 `{"dates": [...], "series": {"外資": [...], ...}}`，走同一套 `_dataset_cache`（key 含 lookback_days，跟既有10天查詢是分開的快取項目，不互相污染）。
  - `tradingnote_gui.py`：
    - `COLUMNS` 新增「族群」「概念股」兩欄（緊接在「名稱」後面），`refresh_table` 用 `get_industry_map(HISTORY_DB_PATH)`（本地 SQLite、7日TTL快取，非新增網路呼叫）＋ `self._ticker_concept_map` 即時填入，抓不到時 fallback 顯示「-」、不彈錯誤視窗（避免影響損益欄位正常顯示）。
    - `_build_positions_tab` 改用 `QSplitter(Vertical)`：上半部原本的 `QTableWidget`，下半部新增「個股詳細資訊」區塊（`_build_position_detail_section`）：族群/概念股文字（即時）＋ FinMind 本益比/殖利率/股價淨值比與三大法人最新一天買賣超（背景查詢文字）＋ `pg.PlotWidget` 三條累計淨買賣超趨勢線（外資/投信/自營商，`x` 軸用交易日索引＋每隔 `len//8` 天標一次日期避免擠爆）。選取表格某列（`itemSelectionChanged`）觸發 `_load_position_detail`，跟 `StockDetailDialog` 一樣用 `run_task_in_thread` 背景查，但**不彈窗**，直接更新頁面本身（使用者這次明確要求「顯示在部位紀錄頁本身」，不要另外點擊/彈窗）。用 `_is_current_detail_target` 防止使用者查詢途中切換部位時，舊查詢的結果覆蓋新選取的畫面。
- **驗證**：`python3 -m py_compile` 全部通過；用 `/opt/homebrew/bin/python3.12` import 新舊模組成功；真實網路呼叫 `fetch_institutional_investors_history("2330", token, 120)` 拿到83個交易日、三分類數字合理（跟 FinMind 官網資料比對一致）；headless GUI smoke test（`QT_QPA_PLATFORM=offscreen`，真實 `data/positions.json` 裡的 `2330`／`2301`、真實網路）驗證：族群欄位正確顯示「半導體業」、概念股欄位正確顯示「晶圓代工與IC設計」；選取列後背景查詢完成，詳細資訊文字正確顯示 FinMind 本益比/殖利率與三大法人最新買賣超；圖表正確畫出3條曲線（外資/投信/自營商）。測試腳本執行前後比對 `data/positions.json` 內容未被意外覆寫（原本就有2筆真實部位，測試用的假部位判斷式因此沒有觸發）。
- **尚未 commit**（改動檔案：`tradingnote_finmind.py`、`tradingnote_gui.py`；新增檔案：`tradingnote_concepts.py`、`concepts.json`）。
- **刻意留白／已知限制**：①概念股清單目前只有本次對話手動整理的11個族群約20檔龍頭股，覆蓋率低，需要使用者之後自行擴充 `concepts.json`；②120日是 FinMind 查詢的日曆天數區間（`lookback_days=120`），實際回傳的交易日數會少於120（例如台積電測試時是83天），跟專案裡 `DEFAULT_BACKFILL_TARGET_DAYS=120` 的既有慣例一致，沒有特別去換算成「120個交易日」；③表格重新整理（`refresh_table`）不會保留使用者原本選取的列，切換到部位紀錄頁重新整理後若原本有選取，下方詳細資訊區塊會維持上次查詢結果不會自動清空，也不會自動重查。

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
