# tradingnote 變更記錄（CHANGELOG.md）

Append-only 的歷史變更記錄，新的加在最上面（跟以前 HANDOFF.md 的慣例一樣）。**這份文件只用來查歷史脈絡／某個改動當時的驗證細節，不代表現在的狀態**——現在的狀態、已知缺口、還沒做的事看 `STATUS.md`。

寫新entry時的慣例：標題用 `## YYYY-MM-DD 標題`；內容包含動機／改法／驗證方式，`**未做**`／`**沒有**` 標出沒驗證到的部分，不要為了好看而省略。

---

## 2026-09-16 修正介面卡頓：高頻互動路徑改走背景執行緒

**動機**：user 回報 GUI「非常卡頓」，要求檢查架構文件跟實際執行之間的矛盾點。派 Explore agent 檢查後確認：`ARCHITECTURE.md:107` 明文規定「GUI 一律用 `run_background_task()`」，但好幾處會被高頻觸發的重運算（個股選取即時查技術指標、資金流向全市場約1700檔聚合）完全繞過這條路徑，直接同步跑在 Qt 主執行緒——選取股票、打字篩選、甚至每秒一次的 `_flow_revision_timer` 都可能讓主執行緒卡住。

**改法**（分四階段，逐階段驗證）：
1. `_filter_stocks_tree`（搜尋框逐鍵觸發、對約1700個 `QTreeWidgetItem` 做 `setHidden`）：加 150ms debounce（`QTimer.singleShot`），迴圈包 `setUpdatesEnabled(False)` 批次更新；`refresh_stocks_tab`（建樹）同樣包 `setUpdatesEnabled`。
2. `_on_stock_selected`：原本同步呼叫 `load_local_technical`（SQLite 查詢＋全歷史技術指標運算），改成 120ms debounce＋`run_background_task` 背景執行緒，並用 `tradingnote_cache.TTLCache`（5分鐘）依 ticker 快取。背景完成時比對 `_stock_trend_expected_ticker` 是否仍是目前選取的股票，避免使用者已切換到別檔時舊結果覆蓋畫面。
3. `refresh_flow_tab`：`self.flow_service.analyze()`（全市場約1700檔聚合）改走 `run_background_task`。因為 `FlowAnalysisService` 內部快取字典不是 thread-safe，改成「同一時間只讓一份分析在跑，跑的期間若又有新請求就只記下最新一份、等目前這份跑完再接著跑」（`_flow_refresh_in_flight`／`_flow_refresh_pending_request`），不讓多個背景執行緒同時呼叫 `analyze()`。`_refresh_flow_on_revision` 每秒輪詢維持不變，但觸發的 `refresh_flow_tab()` 已經不會卡住主執行緒。
4. `_on_futures_row_selected`：`get_large_traders_history_series`（SQLite 查詢）改成 100ms debounce＋背景執行緒，仿照 `_on_position_row_selected` 既有的背景化模式。

**過程中發現的真實 bug（不是這次才引入，是 `tradingnote_tasks.run_background_task` API 的既有陷阱）**：實作階段2時，用 `QT_QPA_PLATFORM=offscreen` + 真實 `QEventLoop.exec()` 寫獨立驗證腳本，發現我自己新增的三個呼叫點全部沒有把 `run_background_task()` 的回傳值存起來（`run_background_task(self, work, on_done, on_error)`，沒有 `xxx = `）——結果就是背景執行緒明明跑完了，`on_done` 卻永遠不會被呼叫，且完全不拋例外、不留任何錯誤訊息，純靜默失敗。回頭檢查發現：現有全部 5 處既有呼叫點（`tradingnote_gui.py` 內 `self._xxx_timer = run_background_task(...)` 或 `run_task_in_thread(...)`）無一例外都有存回傳值，是一個必須遵守但沒被明文寫進 `ARCHITECTURE.md` 的隱性慣例。已補上 `self._stock_trend_task`／`self._flow_refresh_task`／`self._futures_trend_task` 三個屬性存住回傳值，並在 `ARCHITECTURE.md`「跨模組共用慣例」補一筆說明這個陷阱。

**驗證**：
- `python -m py_compile tradingnote_gui.py` 每階段都過關。
- `python -m unittest test_tradingnote_flow test_tradingnote_technical`：13 個測試全過（這次只改 `tradingnote_gui.py`，核心模組邏輯不受影響，用來確認沒有意外波及）。
- 用 `.venv` python、`QT_QPA_PLATFORM=offscreen`，實際 `import tradingnote_gui` 並建構 `TradingNoteWindow`，寫獨立驗證腳本（真實 `QEventLoop.exec()`，不是手動 `processEvents()` 忙輪詢——後者在這個 Qt 版本下對「重複觸發」的 `QTimer` 表現不穩定，會誤判成「卡住」，改用真正的事件迴圈才量得準）：
  - `setCurrentItem`（觸發 `_on_stock_selected`）本身在 0.0000s 內回傳（原本同步查詢會卡在這一行）；trend badges 於 0.234s 後透過背景執行緒正確填入 4 個徽章 widget。
  - 連續快速切換 5 檔股票（模擬方向鍵快速瀏覽），debounce 正確只在最後一檔穩定後才觸發一次背景查詢。
  - `refresh_flow_tab()` 呼叫本身 0.0000s 回傳；背景分析於 2.562s 後完成並正確套用到畫面（`flow_list`／`stock_capital_flow_list`／`volume_outliers_list`／泡泡圖）。
  - 連續呼叫 5 次 `refresh_flow_tab()`（模擬使用者快速切換日期／分類），驗證只有最新一份請求真正被執行、`_flow_refresh_pending_request` 最後正確清空成 `None`，沒有多個背景執行緒同時呼叫 `analyze()`。
  - 「期貨」頁選列同樣驗證 debounce＋背景執行緒觸發正確。

**未做**：沒有在真正跑起來的桌面 App（有實體視窗、真人滑鼠鍵盤）裡手動操作驗證主觀「有沒有感覺變不卡」——這次驗證都是離線腳本＋計時斷言，量到的是「主執行緒呼叫本身是否近乎瞬間回傳」跟「背景結果最終有沒有正確套用」，不是真人操作的主觀流暢度；且測試用的 `snapshot={}`（沒有真實報價），`flow_service.analyze()` 在真實約1700檔非空 snapshot 下的實際背景運算時間可能比測試量到的 2.5 秒更長，但無論多長都不會再卡住主執行緒，這是這次修正的核心目標。全域 `pg.setConfigOptions(antialias=True, ...)` 沒開 `useOpenGL` 這點（會放大每次圖表重繪的成本）這次刻意沒動，風險較高（深色主題才剛調好，改動全域繪圖設定有連帶視覺風險），留待後續視情況評估。

---

## 2026-09-15（續10）融資／借券歷史趨勢圖（借券賣出餘額、借券成交量）

**動機**：user 要求「融資、借券、券賣要有歷史紀錄顯示」。先評估現況：融資餘額／融券餘額其實已經有 120 天歷史＋趨勢圖（`fetch_margin_short_sale_history`），真正缺的是借券賣出餘額（SBL，證券商辦理有價證券借貸的餘額）跟借券成交（借券市場實際成交量／費率）——這兩個原本只顯示最新一天文字，沒有歷史圖。

**改法**：
1. `tradingnote_finmind.fetch_short_sale_balance()`／`fetch_securities_lending_summary()`：兩者原本就用 `_fetch_dataset(..., lookback_days=10)` 抓多天資料，只是最後只取最新一天、把其餘天數丟掉——**不用新增任何 API 呼叫**，只要把丟掉的資料留下來即可。改法：`lookback_days` 預設從 10 拉長到 120（比照 `fetch_margin_short_sale_history`），回傳值新增 `"dates"`/`"series"` 兩個 key，原本 `"date"`/`"balance"`/`"change"`（或 `"volume"`/`"avg_fee_rate"`）維持不變、只是不再拿掉——現有讀取「最新一天」文字摘要的呼叫端完全不用改。動手前先實測 FinMind 免費額度在 120 天窗口內兩個資料集分別給了 85／84 個交易日，確認資料深度足夠畫趨勢圖，不是憑空假設。
2. `tradingnote_gui.py`：新增 `_populate_short_sale_balance_chart()`／`_populate_lending_volume_chart()`，跟既有的 `_populate_margin_chart`／`_populate_vpt_chart`／`_populate_mfi_chart` 同一種寫法（各自一個小函式，不是抽一個通用 helper——這批函式本來就是已知的重複模式，這次維持現狀一致，不在無關的功能改動裡順便重構）。兩者分開兩個獨立分頁，不是合併成一張雙軸圖：借券賣出餘額（股）是累積餘額，借券成交量（張）是當日流量，量級跟性質都不同，合併成一張圖容易誤導。
3. `_render_detail_block()`（「個股」頁 `StockDetailDialog` 的「顯示完整籌碼面資訊」跟「部位紀錄」頁的個股明細共用同一份畫面邏輯）簽名新增 `sbl_chart`／`lending_chart` 兩個參數，這兩個地方原本就各自有一份重複的「建立六個 `pg.PlotWidget`、加進 `QTabWidget`」樣板碼（已知架構債務），這次一併各加兩個新分頁「借券賣出餘額」「借券成交」——沒有讓債務變嚴重，但也沒有趁機解決它，維持現有慣例，這次只加內容不重構結構。連帶找到並更新 6 處 `_render_detail_block(...)` 呼叫、`StockDetailDialog`／`TradingNoteWindow` 兩邊各自的 3 處 `.clear()` 清空區塊，全部同步加上新的兩個圖表 widget。

**驗證**：`.venv` python `import tradingnote_gui` 成功、`py_compile` 過關。用真實 `fetch_position_detail("2330", "")`（無 token，公開額度）拿到的真實資料直接呼叫 `_render_detail_block()`（不透過背景執行緒，跳過 UI 事件迴圈的等待），截圖確認「借券賣出餘額」分頁顯示 85 個交易日的真實趨勢線、數值走勢跟同一份文字摘要顯示的最新一筆（16,618,514 股）吻合；「借券成交」分頁顯示 84 個交易日的成交量趨勢，可以看到 6 月中旬有一次明顯放量的高峰。另外用全部欄位皆為 `None` 的假資料呼叫 `_render_detail_block()`、以及直接對兩個新的 `_populate_*_chart` 函式傳 `None`，確認資料不足或查詢失敗時不會拋例外、圖表正常留空——這對這個實際依賴 FinMind 免費額度（可能超額度、可能沒 token）的功能特別重要。**未做**：沒有在跑起來的完整 App 裡實際點「顯示完整籌碼面資訊」按鈕觸發背景執行緒＋UI 這條路徑（滑鼠座標/截圖已知 DPI 落差，見更早條目）——但背景執行緒／按鈕連線邏輯這次完全沒有改動，只有新增兩個參數傳遞跟兩個新函式，且已經用真實資料驗證過 `_render_detail_block` 本身，風險低。**額外發現**：這輪工作途中發現 working tree 裡有非本次改動產生的異動（`tradingnote_flow.py`／`tradingnote_history.py`／`tradingnote_institutional.py` 被修改，新增 `ANALYSIS_CONSISTENCY.md` 等檔案）——研判是 Codex 同時間在同一份共用資料夾工作，內容/進度不明，這次的 commit 刻意不包含這些檔案，見 `STATUS.md`「目前實際尚未 commit 的異動」。

---

## 2026-09-15（續9）趨勢徽章推廣到「個股查詢」分頁的 `stock_preview` 摘要面板

**動機**：續8 把「趨勢／動能／量能」徽章加到 `StockDetailDialog`，但續8 的 CHANGELOG 條目也記錄「個股查詢」分頁右側的 `stock_preview` 摘要面板這次沒有一起加。user 這輪直接要求推廣過去。

**改法**：
1. 把續8 寫在 `StockDetailDialog.__init__` 裡的「建立徽章列＋沒資料時顯示 muted 提示」邏輯抽成共用函式 `_populate_trend_badge_row(layout, technical, empty_text=...)`，`StockDetailDialog` 自己也跟著改成呼叫這個共用函式（原本是內嵌一段重複邏輯）——避免這次推廣變成再複製貼上一份一樣的 if/else。
2. 新增 `_clear_layout(layout)`：`stock_preview` 面板會隨使用者在清單上切換選取而重複更新，不像 `StockDetailDialog` 只在開啟時建立一次，所以需要「清空重繪」而不是「只建立一次」。
3. `_build_stocks_tab`：`stock_preview` 面板新增 `self.stock_preview_trend_row`（QHBoxLayout），放在股價 StatCard 跟 meta 文字之間。`_on_stock_selected`：清單上什麼都沒選取時呼叫 `_clear_layout` 清空（不顯示「資料不足」，因為根本還沒選股票，語意不同）；選到股票時呼叫 `_populate_trend_badge_row(self.stock_preview_trend_row, load_local_technical(HISTORY_DB_PATH, ticker))`，跟股價/meta 一起同步更新。

**驗證**：`.venv` python `import tradingnote_gui` 成功、`py_compile` 過關。用獨立測試腳本原樣複製 `stock_preview` 面板的容器建構程式碼，模擬三種切換情境截圖比對：
- 未選取 → 選 2330：徽章正確顯示「趨勢：偏空／動能：中性／量能：萎縮」。
- 選 2330 → 未選取：**第一次測試時發現真的 bug**——`_clear_layout` 原本只呼叫 `widget.deleteLater()`，但 `layout.takeAt()` 只是讓 layout 不再排版這個 widget，widget 本身仍是同一個 parent 的子物件，`deleteLater()` 排的是之後才執行的真正刪除，所以舊徽章會先「脫離 layout 版面控制、但還留在畫面上原地」，跟新的 meta 文字疊在一起變成一團亂碼。修法：`setParent(None)` 立刻把 widget 從父子關係拔掉（畫面立刻消失），`deleteLater()` 才負責之後真正釋放物件，兩者都要做。修完後重新截圖確認清空狀態乾淨、沒有殘影。
- 2330 → 另一個不存在的假 ticker 9999：確認會正確清掉 2330 的徽章、顯示「歷史資料不足...」，不是疊加或殘留舊徽章。

這個 bug 也回頭檢查過 `StockDetailDialog` 那邊沒有同樣風險——它的徽章列只在 `__init__` 建立一次、不會重複重繪，沒有「清空舊 widget」這個步驟，所以沒有受影響，不需要額外修正。**未做**：沒有在跑起來的完整 App 裡用滑鼠實際點選清單項目觸發（已知 DPI 落差，見更早條目），但獨立測試已經是原樣複製容器建構程式碼＋呼叫真實的 `_on_stock_selected` 會呼叫的同一批函式（`_populate_trend_badge_row`／`_clear_layout`／`load_local_technical`），不是另外寫一套簡化邏輯測試。

---

## 2026-09-15（續8）`SignalBadge` 推廣到「個股概覽」（`StockDetailDialog`）

**動機**：續6/續7 都把「`SignalBadge` 推廣到個股概覽」列為候選但沒做，理由是當時程式裡沒有任何「趨勢判斷」邏輯可以驅動徽章內容。這輪 user 直接要求做這件事，所以先補上最小可行的 rule-based 判斷，再接上徽章。

**改法**：
1. `tradingnote_gui._stock_trend_badges(technical)`：純函式，輸入是 `tradingnote_technical.calculate_indicators()`（透過 `load_local_technical` 取得）既有的輸出，**沒有新增任何指標計算**，只是解讀已經算好的數字：
   - 趨勢：最新收盤價相對 MA20 的乖離（≥1% 偏多、≤-1% 偏空，中間留一段中性帶過濾貼線雜訊）。
   - 動能：RSI(14) 最新值（≥55 偏強、≤45 偏弱）。
   - 量能：今日成交量 ÷ 均量20（≥1.2x 放大、≤0.8x 萎縮）——量能本身沒有天生多空傾向（爆量可能噴出也可能出貨），tone 刻意不用 positive/negative，放大用 warning（提醒注意），萎縮用 neutral，避免暗示「量增=好事」這種沒有根據的判斷。
   任一項所需資料不存在（例如新股不到 20 個交易日）就跳過該項，不用預設值假裝有結論；三項都不足時回傳空 list。
2. `StockDetailDialog.__init__`：原本 `load_local_technical(...)` 的結果只丟給 `TechnicalAnalysisWidget`，這次改成先存成區域變數 `technical`，同一份資料同時餵給徽章邏輯跟技術分析元件（不重複查兩次 DB）。在 hero（股價/漲跌）下方、狀態文字上方插入一列 `SignalBadge`；`_stock_trend_badges` 回傳空 list 時顯示 muted 文字「歷史資料不足，暫無法判斷趨勢／動能／量能。」而不是留白——跟已有的技術分析圖表本身的「尚無足夠歷史價格資料」空狀態文字風格一致。

**驗證**：`.venv` python `import tradingnote_gui` 成功、`py_compile` 過關。用 `load_local_technical` 對真實 `2330`（`data/history.db` 既有資料）跑一次 `_stock_trend_badges`，人工核對算出來的門檻判斷（收盤 2385.0 vs MA20 2410.25，乖離 -1.05% → 偏空；RSI 46.7 → 中性）是對的，不是憑感覺看結果"順眼"就通過。用獨立測試腳本建構真實 `StockDetailDialog`（`fake_parent` 帶 `.snapshot`）截圖驗證兩種情境：(a) `2330` 真實資料 → 三個徽章「趨勢：偏空」「動能：中性」「量能：萎縮」都正確顯示且顏色跟同一列的漲跌%（同樣是綠色，續7 翻轉後負值＝綠）一致；(b) 一個不存在於 `history.db` 的假 ticker `9999` → 正確顯示「歷史資料不足...」的空狀態文字，沒有拋例外或顯示錯誤數字。**未做**：沒有在跑起來的完整 App 裡雙擊真實清單項目觸發這條路徑（滑鼠座標/截圖已知 DPI 落差，見更早條目）——但這條路徑只在 `__init__` 內部新增程式碼，沒有動任何事件連線邏輯，風險低；「個股查詢」分頁右側的 `stock_preview` 摘要面板這次沒有一起加徽章（規格裡沒明確要求那裡也要，且該面板空間較小，之後有需要再加）。

---

## 2026-09-15（續7）`SectionCard`＋`InsightCard`＋正紅負綠拍板，一次做完

**動機**：user 對「接下來做 SectionCard、InsightCard、還是先決定紅漲綠跌」的提問回答「都做」，一次把三件事都做完並要求 push 到遠端。

**改法**：

1. **正綠負紅 → 正紅負綠**：續4/續6 都有提到現有 `COLOR_GAIN`(綠)/`COLOR_LOSS`(紅) 其實是西式慣例，跟台灣「漲紅跌綠」不一致。這輪直接拍板改成台灣慣例。因為 `ui/theme.py` 是唯一色彩來源、所有呼叫端都透過 `gain_loss_color()` 或常數名稱取色（沒有任何地方寫死 RGB 判斷「這是綠色」），修法極簡：只把 `COLOR_GAIN`/`COLOR_LOSS` 兩個 hex 值對調（`#22C55E`↔`#EF4444`），連帶對調 `COLOR_GAIN_TINT`/`COLOR_LOSS_TINT`。個股漲跌、三大法人買賣超、大額交易人淨部位、資金流向 StatCard／SignalBadge／InsightCard／泡泡圖四象限，全部自動變成紅漲綠跌，**沒有改任何一處呼叫端程式碼**——這正是先前把顏色收斂進 `ui/theme.py` 這筆投資的回報。順便把 `tradingnote_gui.py` 裡兩處「正綠負紅」的中文註解（三大法人前10大淨、期貨大額前10淨）文字改成「正紅負綠」，避免註解跟實際行為對不上。
2. **`ui/components/section_card.py`（`SectionCard`）**：標題（＋可選描述、`header_row` 讓呼叫端加右側附加 widget）＋`body_layout`（呼叫端自由塞內容），沿用 `StatCard` 同一套 `summaryCard` QSS，不另外發明卡片樣式。第一個用法：`_build_institutional_direction_section`（「法人方向」子頁）整段（標題＋提示＋三張法人買賣超圖）包進一張 `SectionCard`——原本這段是直接鋪在頁面背景上，沒有卡片邊界，跟規格「所有主要 Dashboard 區塊盡量包成統一的 section card」對不上。過程中把 `_CARD_BG`（原本 theme.py 內部私有、只有 QSS 自己用）升級成公開的 `COLOR_CARD_BG`，因為 `pg.PlotWidget.setBackground()` 需要實際色值而不是 QSS 屬性——把三張法人圖表的背景從 `COLOR_SURFACE`（頁面底色）改成 `COLOR_CARD_BG`（卡片底色），不然圖表背景會跟卡片背景不同深淺、看起來像圖表沒貼齊卡片。
3. **`ui/components/insight_card.py`（`InsightCard`）**：headline（依 tone 上色）＋detail（muted），純顯示用元件。tone 對照表抽成 `ui/theme.py` 的 `TONE_COLORS`（`SignalBadge` 原本自己有一份 `_TONE_COLORS`，這次一併搬過去共用，避免兩個元件各自維護一份一樣的對照表）。第一個用法：`tradingnote_gui._flow_momentum_insight(rows)`——rule-based（規格明確要求不接 LLM）從 `dashboard.industry_flow` 找當天最極端的一筆量價訊號：只挑 `turnover_ratio`（今日量比）與 `daily_change_pct`（當日漲跌%）都存在、且量比 ≥1.5、漲跌幅 |%| ≥0.5 的族群，取量比最高者，依漲跌正負生成「資金動能增強」或「放量下跌需留意」；沒有夠格的族群時回傳中性的「暫無明顯資金訊號」文案（規格「Loading/Error/Empty State 要一致，不要留白」），不是留白也不是報錯。放在「資金流向」分頁 StatCard 下方、圖例徽章上方，在 `_refresh_flow_summary` 裡跟三張 StatCard 一起更新，沒有額外多打 API 或多查 DB。

**驗證**：`.venv` python `import tradingnote_gui` 成功；`py_compile` 過一遍所有新增/修改的 `.py`；用合成的 `IndustryFlow` 資料單元測試 `_flow_momentum_insight` 三種情境（有正向候選/沒有候選/正負都有但選最極端那個），寫成檔案用 UTF-8 讀回確認中文字串正確（不是只看終端機亂碼判斷"看起來對"）。**實機啟動整個 GUI**（`main()`），視窗帶到前景截圖確認「資金流向」分頁：`COLOR_GAIN`/`COLOR_LOSS` 確實對調（今日偏流入變紅、偏流出變綠）、`InsightCard` 顯示「資金動能增強／資訊服務業...」且跟「成交最活躍」StatCard 挑到同一個族群（互相印證邏輯正確，不是巧合湊出來的假數字）、泡泡圖四象限底色也跟著對調（原本紅的象限變綠、綠的變紅）。`SectionCard` 因為「法人方向」子頁要多切一次分類按鈕，這台機器滑鼠座標跟截圖有已知 DPI 落差（見更早 CHANGELOG 條目），改用獨立測試腳本原樣複製 `_build_institutional_direction_section` 的容器建構程式碼＋真正的 `SectionCard`／三個 `pg.PlotWidget` 截圖驗證，確認標題／右側日期標籤／描述／三張圖背景都正確且圖表背景跟卡片背景無縫接軌。全部驗證後關閉測試程序、確認無殘留 process。**未做**：沒有在跑起來的完整 App 裡實際切到「法人方向」分頁用肉眼看過（原因同上，滑鼠點擊風險）——但 `_build_institutional_direction_section` 這次只改了容器（`layout.addWidget/addLayout` 換成 `card.header_row/body_layout`），沒有動 `_refresh_institutional_direction` 的資料邏輯，風險低。

---

## 2026-09-15（續6）新增 `SignalBadge`，第一個用法：泡泡圖圖例

**動機**：user 要求先做 `SignalBadge`（規格原文：統一分析狀態顯示，不要讓各頁自行建立不同 badge style，例如 強勢/偏多/中性/偏空/風險/資金流入/資金流出）。跟先前的 `StatCard` 不一樣，目前程式裡沒有現成的「重複的徽章 widget」可以直接替換——app 目前還沒有任何地方計算「趨勢判斷」這種質化狀態（那是規格後面 Dashboard／個股頁 phase 的事），所以這輪是先把 reusable 元件建好，同時找一個現有、真的能馬上受益的地方換掉，而不是憑空生出一個沒人用的元件。

**改法**：
1. `ui/components/signal_badge.py`：`SignalBadge(text, tone)`，`tone` 是 `positive`/`negative`/`info`/`warning`/`special`/`neutral` 六種，對應顏色都是 `ui/theme.py` 既有的語意色（`COLOR_GAIN`/`COLOR_LOSS`/`COLOR_INFO`/`COLOR_WARNING`/`COLOR_SPECIAL`/`COLOR_NEUTRAL`），底色用同一組低透明度 tint token（沿用續4已經做好的 `COLOR_*_TINT`，這次補了原本沒有的 `COLOR_INFO_TINT`/`COLOR_SPECIAL_TINT`），畫成小圓角藥丸徽章。文字內容本身不猜測、由呼叫端決定——這元件只負責「同一個狀態到處長一樣」，不做關鍵字判斷之類的隱性邏輯。
2. 第一個實際用法：「資金流向」分頁泡泡圖原本用一句純文字說明顏色代表什麼（「綠色＝當日量價偏流入，紅色＝偏流出，灰色＝中性」），換成三個 `SignalBadge`（偏流入/偏流出/中性）排成一列，放在原本文字說明的位置；剩下「標示成交佔比前五大族群，滑鼠移至泡泡查看數值」的部分留著，只是不再需要用文字描述顏色。
3. 過程中發現泡泡本身的顏色（`_populate_flow_bubble_hover` 附近，`daily_change`／`direction_text` 那段）是另外三個手寫 hex（`#8290A3`／`#2E9B65`／`#D95852`），跟 `COLOR_MUTED`／`COLOR_GAIN`／`COLOR_LOSS` 意思一樣但數值不同（應該是舊淺色主題時代各自調的，沒跟著 theme token 走）。這次順手改成直接用 `COLOR_MUTED`/`COLOR_GAIN`/`COLOR_LOSS`，這樣新加的圖例徽章顏色才能保證跟泡泡本身顏色完全一致，不是「看起來差不多」。

**驗證**：`.venv` python `import tradingnote_gui` 成功；獨立測試 `SignalBadge` 六種 tone 與一個不存在的 tone（確認 fallback 成 neutral、不丟例外）。**實機啟動整個 GUI**（`main()`），視窗帶到前景截圖確認「資金流向」分頁：StatCard 下方三個徽章（綠色「偏流入」、紅色「偏流出」、灰色「中性」）正確渲染成圓角藥丸樣式，跟下方泡泡圖的顏色一致；關閉程序、確認無殘留 process。**未做**：`SignalBadge` 目前只有這一處用法，還沒推廣到其他頁面——`SectionCard`/`InsightCard`／個股概覽的「技術面：偏多」之類的質化摘要，需要規格後面 phase 才會有對應的「趨勢判斷」邏輯可以驅動徽章內容，這次不硬套。

---

## 2026-09-15（續5）`StockDetailDialog` hero／交易週誌 stock_preview 換成 StatCard

**動機**：上一輪（續3）先換掉「資金流向」分頁的三張卡，另外兩處手刻「大數字＋漲跌」樣式（`StockDetailDialog` 的股價 hero、「個股」分頁交易週誌旁的 `stock_preview_price`）當時刻意留著沒動（規格「每次不要一次改太多」）。這輪 user 直接要求把這兩處也換掉。

**改法**：
1. 這兩處跟先前那三張卡不一樣——它們是嵌在既有 `stockHero` 屬性的 `QFrame` 裡面（`stockHero` 本身已經有背景／邊框），如果直接塞一個完整 `StatCard`（自己也畫 `summaryCard` 背景／邊框）進去，會變成卡片疊卡片。所以先幫 `StatCard` 加兩個參數：`bordered=False`（不套 `summaryCard`、不留卡片內距，讓外層容器自己負責背景/邊框）、`title` 允許 `None`/空字串（不顯示標題那行——這兩處原本都沒有獨立小標題）。
2. `StockDetailDialog`：原本 `hero_price_label`＋`hero_change_label` 兩個手動建立、右對齊的 QLabel，換成一個 `StatCard(bordered=False)`，`value`＝股價、`change`＝漲跌%、`status` 依漲跌正負推導，另外對 `value_label`/`change_label` 呼叫 `setAlignment(AlignRight)` 保留原本的靠右對齊。
3. 交易週誌旁「個股摘要」面板：原本 `stock_preview_price` 是單一 QLabel（股價與漲跌%合併成一行文字，整行上色），換成 `StatCard(bordered=False)`（不帶 change，用「整行上色」那條路徑，跟原本行為一致），`_on_stock_selected` 裡三處 `.setText()+.setStyleSheet()` 都改成 `.set_value(text, status=...)`。

**驗證**：`.venv` python `import tradingnote_gui` 成功。兩處都不方便用滑鼠在真實視窗裡點出來看（`StockDetailDialog` 要雙擊「個股」分頁清單、`stock_preview` 要點選清單項目觸發 `_on_stock_selected`，這台機器滑鼠座標跟截圖有已知 DPI 落差，見更早的 CHANGELOG 條目），改用獨立測試腳本直接呼叫真實 class／真實程式碼路徑截圖驗證：`StockDetailDialog(fake_parent, "2330", "台積電", "", market="TWSE")` 真的建構整個對話框（`fake_parent` 是一個帶 `.snapshot` 的 `QWidget` 子類，模擬真實 parent），`.grab()` 存圖確認 hero 區「台積電 2330 / TWSE」在左、「1,080.00 / +2.86%」右對齊在右且是綠色，跟改之前的樣式一致，沒有卡片疊卡片的視覺瑕疵；`stock_preview` 那塊因為建構邏輯掛在 `_build_stocks_tab`（依賴整個 `TradingNoteWindow` 初始化），改成在測試腳本裡原樣複製那幾行容器建構程式碼＋真正的 `StatCard` 元件呼叫 `set_value("1,080.00　+2.86%", status="positive")`，截圖確認同樣正確。**未做**：沒有在跑起來的完整 App 裡用滑鼠實際雙擊/點選觸發這兩個路徑——上述截圖驗證的是真實 class/真實元件本身的渲染結果，但沒有驗證「使用者實際點擊清單項目」這個互動路徑本身有沒有問題（例如事件連線）；不過這條路徑（`_on_stock_double_clicked`／`_on_stock_selected`）這次完全沒改動連線邏輯，只改了被呼叫函式內部怎麼更新畫面，風險低。

---

## 2026-09-15（續4）Dark Mode 拍板並套用

**動機**：user 的 GUI 規格明確要 dark-mode-first，給了 6 個錨點色（background/card/hover/border/primary text/secondary text）；上一輪（續3）先建立 `ui/theme.py` 時刻意保留原本淺色配色，只做「搬家」，把「要不要換色」跟「怎麼搬」分開決定，避免混在一起。這輪 user 明確說「先做 dark mode 決定，你直接建議一個配色方案」，於是直接拍板。

**改法**：
1. 在 user 給的 6 個錨點色基礎上，補齊缺的角色：accent 藍（深色底下用更亮的 `#3B82F6`／hover `#5B9DF9`，方向跟淺色主題相反——淺色主題 hover 是變暗），以及 `COLOR_GAIN`/`COLOR_LOSS` 提亮成 `#22C55E`/`#EF4444`（沿用既有「正綠負紅」語意，只換色階不換語意——**發現一個既有的潛在問題**：這組語意其實是「正值＝綠、負值＝紅」的西式慣例，連 `StockDetailDialog` 的個股漲跌 `change_pct` 也套用同一組，這跟這次規格寫的「台灣市場習慣上漲紅下跌綠」不一致；程式裡有中文註解「正綠負紅」明確顯示這是刻意的統一規則，不是筆誤，所以這輪只換色階、不動語意，要不要整個翻轉是另一個獨立決定，留給 user）。
2. 這不只是換 8 個 `COLOR_*` 常數——`ui/theme.py` 的 QSS 樣板裡原本還散落約 20 處直接寫死的淺色 hex（側欄選中底色、按鈕 hover/pressed/disabled、輸入框與表格背景「white」、下拉選單選取色、表格格線、tooltip 等），這次全部改成新增的 token（`COLOR_GAIN_TINT`/`COLOR_LOSS_TINT`/`COLOR_WARNING_TINT`/`COLOR_NEUTRAL_TINT`、`COLOR_SELECTED_BG`/`COLOR_SELECTED_TEXT`/`COLOR_SELECTED_BORDER`、`COLOR_DISABLED_BG`/`COLOR_DISABLED_TEXT`），不然會變成「深色外殼包白色內容」。
3. 順帶抓到並修正兩處原本 hardcode 淺色 hex、繞過 `ui/theme.py` 的地方：`tradingnote_gui.py` 的泡泡圖四象限底色（`_flow_quadrant_specs`，原本是不透明淺粉/淺綠 `#E9F6EF` 等，改成新的半透明 tint token，`QColor` 吃 `#AARRGGBB` 格式）；泡泡圖象限標題文字（`#344054`/`#667085` 深色文字，深色底下幾乎看不見，改用 `COLOR_TEXT`/`COLOR_MUTED`）；交易週誌已選日期卡片的 `setStyleSheet()`（繞過全域 QSS 直接寫死 `#E7F0FC`/`#F0F3F7`/`#8793A4`，改用新 token）。
4. 下拉選單箭頭圖示（`assets/chevron-down.svg`/`chevron-up.svg`）原本 stroke 是 `#617187`（舊淺色 muted），深色輸入框背景下太暗看不清楚，新增 `chevron-down-dark.svg`/`chevron-up-dark.svg`（stroke 改 `#9DA5B4`）給深色主題用，`ui/theme.py` 的 QSS 改指到新檔案。

**驗證**：`.venv` python `import tradingnote_gui` 成功；寫了一個獨立的 widget gallery 測試腳本（`QDialog` 包 `QTableWidget`/`QLineEdit`/`QComboBox`/`QSpinBox`/一般按鈕/accent 按鈕/disabled 按鈕/checkbox/progress bar，套用 `ui.theme.STYLESHEET`）用 `widget.grab()` 存成截圖檢查——過程中發現 `QHeaderView::section` 沒蓋到表頭最後一欄之後的空白區域，補了一條 `QHeaderView { background: ... }` 規則修掉。**實機啟動整個 GUI**（`main()`），把視窗帶到前景截圖確認「資金流向」分頁：側欄選中態、三張 `StatCard`（綠/紅/中性）、泡泡圖四象限底色與文字、下拉選單，全部在深色底下清楚可讀，沒有殘留白色/淺色區塊；關閉程序、確認無殘留 process。**未做**：其餘分頁（部位紀錄/交易週誌/個股查詢/期貨行情/設定）沒有逐一用滑鼠點過去實機截圖驗證——這台機器已知有滑鼠座標跟螢幕截圖的 DPI 不一致問題（見更早的 CHANGELOG 條目），這次選擇用獨立 widget gallery 側面驗證同一份全域 QSS，而不是冒險亂點滑鼠；泡泡圖／VPT／MFI／融資融券等 pyqtgraph 資料序列色（`#1f77b4` 之類）沒有跟著改，這些是分類資料色不是介面底色，deferred。是否要把「正綠負紅」翻轉成「正紅負綠」符合台灣慣例，還沒決定。

---

## 2026-09-15（續3）GUI Design System 啟動：`ui/theme.py` + `StatCard`

**動機**：user 提出一份完整的 GUI 重構／視覺美化規格（progressive disclosure、design system、`ui/theme.py`／`ui/components/`／`ui/pages/`／`ui/widgets/` 拆分、StatCard/SectionCard/SignalBadge/InsightCard/StockHeader 等 reusable component），目標是把 `tradingnote_gui.py`（約 4700 行）拆小、統一視覺語言，同時完全不動核心計算邏輯／API layer／SQLite schema。規格明確要求「不要一次全部重寫」，第一輪只做 design system + 基礎 reusable components。

**掃描發現**（先讀 code 才動手，見規格 Phase 1）：
- 現有 GUI 其實已經有半套 design system——`COLOR_*` 常數＋單一 `STYLESHEET` QSS 字串集中在檔案開頭，widget 靠 `setProperty("summaryCard"/"metricValue"/"muted"/"header"/"accent", True)` 套樣式，不是到處 `setStyleSheet()` 各寫各的。目前是**淺色**主題（冷灰藍白），跟規格要的 dark-mode-first 不同——這次刻意先不換色，避免把「搬家」跟「改視覺」混在一起，dark mode 留給之後單獨確認。
- `run_background_task` 已經是唯一的背景執行緒路徑，規格要求的「不要另外發明 threading」已經滿足，不用動。
- 找到 3 處手刻的「標題＋大數字」卡片（`_build_flow_chart_section` 的今日偏流入/偏流出/成交最活躍、`StockDetailDialog` 的 hero 價格、交易週誌 `stock_preview`），視覺完全相同、各自手刻 layout——對應規格要的 `StatCard`。
- `COLOR_GAIN if x >= 0 else COLOR_LOSS` 這個二元三元式在 13 處逐字重複（另外 2 處是三元＋`COLOR_MUTED`／`None` 的三段式邏輯，語意不同，這輪不動）。

**改法**（規格 Phase 2＋3 的安全子集）：
1. 新增 `ui/theme.py`：把 `COLOR_*`／`FONT_FAMILY`／`STYLESHEET` 從 `tradingnote_gui.py` **逐字搬過去**，`tradingnote_gui.py` 改成 `from ui.theme import ...`，內容不變（純搬家，不是重新設計）。順便加了 `COLOR_INFO`/`COLOR_WARNING`/`COLOR_SPECIAL`/`COLOR_NEUTRAL` 四個語意色 token，給之後的 `SignalBadge`/`InsightCard` 用，目前尚未在任何畫面使用。
2. 新增 `ui/format.py`：`gain_loss_color(value)` helper，取代那 11 處單純二元的 `COLOR_GAIN if x >= 0 else COLOR_LOSS`（`tradingnote_gui.py` 的 665/730/941/1018/3012/3101/3202/3429/3927/4208/4474 行，含改前行號），三段式的那兩處（原 3876/4440 附近）刻意不動。
3. 新增 `ui/components/stat_card.py`：`StatCard(title, value, unit=None, change=None, status=None)`，視覺對齊既有 `summaryCard`/`metricValue` QSS，`set_value()` 供背景重新整理完成後更新數值不用重建整張卡。有 `change` 文字時色彩套在 change 那行（大數字維持中性色）；沒有 `change` 時色彩直接套在大數字本身——對齊 `_build_flow_chart_section` 原本「整行上色」的用法。
4. 把 `_build_flow_chart_section` 的手刻卡片迴圈換成 `StatCard` 實例（`self.flow_summary_cards`，原本叫 `flow_summary_labels`），`_refresh_flow_summary` 改呼叫 `card.set_value(..., status=...)`。**沒有**動 `StockDetailDialog` hero、交易週誌 `stock_preview`——這兩處雖然視覺相同，但這輪先只換一處驗證元件本身沒問題，其餘留給下一輪（規格十八「每次不要一次修改太多」）。

**驗證**：`.venv` python `import tradingnote_gui` 成功（無 import error／無 circular import）；獨立實例化 `StatCard` 四種參數組合（有/無 change、positive/negative/預設）並檢查 `value_label`/`change_label` 的文字與 `styleSheet()` 字串符合預期；**實機啟動整個 GUI**（`main()`），截圖確認「資金流向」分頁三張卡片渲染跟改動前的手刻版本像素級一致（今日偏流入＝綠字、今日偏流出＝紅字、成交最活躍＝無色），確認沒有回歸後正常關閉程序。**未做**：其餘兩處 stockHero 手刻卡片、`SectionCard`/`SignalBadge`/`InsightCard`/`StockHeader` 等元件、sidebar 分組、dashboard／個股頁重做，全部照規格留到後續 phase，這輪刻意不碰。

---

## 2026-09-15（續2）ARCHITECTURE.md 重寫

**動機**：`ARCHITECTURE.md` 描述的是 4 分頁、僅 Mac、PySide6 剛換完 Tkinter 那個時期的版本（沒有期貨、週誌、法人方向、`tradingnote_flow/taifex/institutional/technical/journal/cache/http/paths/tasks/api_config` 任何一個模組），跟現在約 9600 行、15 個核心模組的專案完全對不上。

**改法**：整份重寫，改變寫法策略以避免重蹈覆轍——舊版本用「每個函式一張表」的方式鉅細靡遺記錄，導致每加一個函式就要跟著改，沒人跟得上而放著爛掉。新版本只記錄「為什麼這樣設計」的穩定架構決策（模組邊界、依賴方向、SQLite schema 慣例、跨模組共用模式），檔案清單／GUI 現況指去 `STATUS.md`（本來就會隨時更新），某次改動細節指去 `CHANGELOG.md`——加一個新函式、新分頁通常不需要動 `ARCHITECTURE.md`。新增「已知架構債務」一節收斂前次架構審查發現的問題清單。同步把 `STATUS.md` 裡「`ARCHITECTURE.md` 過時」的已知缺口拿掉、改成指向新文件。

**驗證**：核對新文件裡列的 15 個模組、SQLite 4 張表擁有權（`daily_prices`/`industry_map`/`valuation_history`→history.py、`large_traders_history`→taifex.py、`journal_*`→journal.py）、`tradingnote_tasks.BackgroundTask` 的實際程式碼，逐項讀原始檔確認敘述無誤後才寫進去，不是憑印象重寫。**沒有**：這只是文件更新，沒有連帶跑測試（不影響任何 `.py` 行為）。

## 2026-09-15（續）拿掉左側導覽列圖示 + 實測全部 API 端點 + 實機執行驗證

**動機**：使用者要求「gui多餘的圖示拿掉」+「測試各api是否有用」。圖示範圍問過使用者確認是左側導覽列（六個分頁圖示，語意跟文字不太搭，例如「期貨行情」配 `SP_ArrowUp`、「設定」配 `SP_DesktopIcon`）。

**改法**：`tradingnote_gui.py` 的 `_page_specs` 從 4-tuple（含 `QStyle.SP_*` 圖示常數）改成 3-tuple，`QListWidgetItem` 建構不再帶圖示，連帶拿掉兩處現在無意義的 `main_navigation.setIconSize(...)` 呼叫；週誌頁上一週/本週/下一週/儲存按鈕的圖示不在這次範圍內，維持原樣。

**API 測試**：寫一次性腳本直接呼叫專案實際在用的 14 個抓取函式（不是重寫一套 HTTP 呼叫），涵蓋 TWSE（5）／TPEX（3）／TPEx 產業鏈平台（1）／TAIFEX（4）／FinMind（1，匿名無 token）。**全部 14 個成功**，端點都還活著。過程中發現：用系統內建 `python`（Windows Store 別名，3.14.6）測會有 8 個 TWSE/TPEX 端點噴 `SSL: CERTIFICATE_VERIFY_FAILED（Missing Subject Key Identifier）`，換成專案 `.venv`（3.12.10，App 實際用的直譯器）重測後穩定全過——這是 Python 3.14 對 TLS 憑證鏈驗證變嚴格、跟 TWSE/TPEX 憑證缺 Subject Key Identifier 擴充欄位的已知不相容，**不是 API 本身壞了**，純粹是測試時要用對直譯器。

**實機執行**：直接用 `.venv` python 跑 `tradingnote_gui.py`（不是 offscreen），約 20 秒完成啟動 preload、視窗正常顯示、stdout/stderr 全程乾淨無例外。截圖確認：①左側導覽列六個項目圖示已清乾淨（`icon().isNull()` 全 True，且截圖目視乾淨）；②資金流向頁四象限泡泡圖、族群數摘要、日期選擇器格式都跟 `STATUS.md` 描述一致；③狀態列價格更新時間與總損益正常顯示。**沒有**：嘗試用 `SetCursorPos`/`mouse_event` 點擊切換到「交易週誌」分頁失敗——這台機器 `CopyFromScreen` 截圖用的原生像素座標（4096×1152）跟 `SetCursorPos` 的座標系統對不上（DPI 縮放），跟 `CHANGELOG.md` 好幾則舊記錄的環境限制是同一個問題，沒有繼續盲猜座標硬點，避免誤觸旁邊開著的其他視窗；因此分頁切換這次仍未能用滑鼠實機驗證。跑完後已 `taskkill` 乾淨關閉，沒留下背景行程。

**驗證**：`ast.parse` 語法檢查過；離屏建構 `TradingNoteWindow` 確認 6 個導覽項目文字與圖示狀態；既有 10 項單元測試全過；14 個 API 端點實測全通過；實機執行截圖驗證（見上）。

## 2026-09-15 架構重複整理（API 端點集中化、大額交易人歷史歸位、快取 fallback 共用化）

使用者要求檢查架構冗餘，做了三項改動：

1. **API 端點集中化**：新增 `tradingnote_api_config.py`，把原本散在 `tradingnote_core.py`／`tradingnote_history.py`／`tradingnote_institutional.py`／`tradingnote_taifex.py`／`tradingnote_finmind.py`／`refresh_concepts.py` 六個檔案裡的 TWSE／TPEx／FinMind／TAIFEX 端點網址集中到一個檔案，六個檔案改成 import 常數，之後官方端點改版只需要改一個檔案。
2. **`large_traders_history` 歸位**：原本這張表（期貨大額交易人未沖銷部位歷史）的 schema 與讀寫函式放在 `tradingnote_history.py`（股票歷史模組），`tradingnote_taifex.py` 卻反過來 import 回來用——依賴方向是反的。已搬到 `tradingnote_taifex.py`，新增獨立的 `_connect()`（仍指向同一個 `history.db` 實體檔案，只是各自管各自的表，兩邊互不影響）。
3. **快取 fallback 共用化**：`tradingnote_cache.py` 新增 `fetch_with_file_cache()`，把「讀新鮮快取→過期重抓→失敗退回舊快取」這套流程收斂成一個 helper（可自訂 serialize/deserialize 處理不同 payload key 或 dataclass 轉換）；套用到 `tradingnote_taifex.py` 的 3 個期貨端點與 `tradingnote_institutional.py` 的法人快照，取代原本 4 處幾乎一樣的手刻邏輯。**刻意沒有**套用到 `tradingnote_core.get_market_snapshot`——它的 `on_progress` 是「並行抓 TWSE／TPEX 兩階段各自報告進度」，跟其他 4 處單純的 fetch/cache 二態不同，硬套只會讓 helper 多出一堆只為了這一個呼叫端存在的 hook 參數。

**驗證**：AST 語法檢查全過；逐一 mock 測試 fresh-hit／force-refresh／過期失敗回退舊快取／查無快取直接拋錯 4 種路徑；`large_traders_history` 搬家後用暫存 DB 驗證兩個模組各自開同一檔案寫入/讀取不衝突；既有 10 項單元測試全過；完整 `TradingNoteWindow` offscreen 建構成功。**未做**：沒有實機開 GUI 用滑鼠走一次「期貨」頁大額交易人趨勢圖／法人方向頁，確認畫面顯示跟改動前一致（邏輯測試涵蓋了，畫面沒有）。

同時發現但**沒有動**、留給之後處理的架構問題（詳見對話紀錄，或重新問一次「協助檢查架構」）：GUI 內部 9 個 QDialog 重複視窗樣板碼、紅綠漲跌上色邏輯重複 13 次、`finmind_token`/`backfill_target_days` 設定讀取重複 4-5 次、`tradingnote_history.py` 內 5 個函式重複「載入N天歷史+算cutoff日期」的 SQL 查詢邏輯、SQLite 連線 schema 建立邏輯在 `tradingnote_history.py`／`tradingnote_journal.py` 重複、`tradingnote_institutional._http_get_json_retry` 應該併入 `tradingnote_http.http_get_json`。

## 2026-09-14 左側主導覽與 GUI 版面整理

主視窗六個上方分頁改為 Codex 風格的左側導覽列，右側使用頁面堆疊並顯示目前功能名稱與簡短說明；資金流向、部位、週誌、個股、期貨、設定的功能內容與頁內圖表分頁維持原行為。標準視窗側欄 218px，小於 1080px 寬或 700px 高時縮為 164px、隱藏側欄副標並收緊選單高度，保留主要表格空間。新增側欄、選中／hover 狀態、內容卡片與頁首的統一樣式；既有 10 項測試通過，另以離屏主視窗驗證六個選單與頁面切換。

## 2026-09-11 響應式介面比例

主視窗最低尺寸降為 680×480，初始尺寸改用螢幕可用範圍的 88%。全域文字縮小一級，表格、分頁與控制項同步收緊；主要分頁、更新、部位操作、週誌導覽／儲存及搜尋加入 22–24px Qt 系統圖示。視窗小於 1080px 寬或 700px 高時自動切換緊湊版面：縮短週誌導覽文字與日期格式、壓縮邊距和週卡高度、隱藏個股頁長提示；七張週卡可忽略文字 size hint 等比分配寬度。圖表最小高度／寬度也降低，避免小解析度被固定尺寸撐破。新增離屏測試驗證緊湊與標準版面切換，目前全套 10 項測試通過。

## 2026-09-10 交易週誌與持股週曆

新增 `tradingnote_journal.py` 與主視窗「交易週誌」分頁：週一至週日七張卡片顯示持股合計金額變化、絕對變動最大的前三檔與日誌摘要，選取日期後可看全部持股並編輯單一自由文字日誌。提供前／後週、本週、日期跳轉、儲存按鈕、Ctrl+S，以及切換日期與關閉程式時自動保存；未來日期不能編輯。

日誌與完整持股快照新增在既有 `history.db` 的獨立資料表。首次啟用記錄日期與當日快照，之後啟動、刷新及部位 CRUD 都覆寫當日快照；啟用後用最近快照延續，啟用前依目前持股與進場日回推並標示估算。每檔金額變化為股數乘以當日與前一交易日收盤價差，休市或缺價顯示 N/A。測試在暫存 DB 執行，涵蓋 schema、估算／快照／空手、週一跨週末、缺價、日誌往返與離屏 GUI。

## 2026-09-10 個股本地技術線圖

個股彈窗直接讀取 SQLite 收盤價、成交量、成交金額，合併已有個股快取 OHLCV；新增查圖不呼叫 API。技術分析共24類：MA、EMA、KD、MACD、RSI、布林通道、帶寬、BIAS、ROC、MOM、成交量與均量、成交金額、日報酬、年化波動率、量比、回落、每日成交均價、OBV、VPT、VWMA、Williams %R、CCI、ATR、MFI。全部／60／120／240筆視窗在完整計算後裁切，暖機與缺欄位保留缺值。SQLite沒有開高低價，因此KD、%R、CCI、ATR、MFI依賴已有OHLCV；沒有合成K棒。這是現有日線資料的常用指標目錄，不代表所有可能的技術分析公式。日線未還原權息。

驗證：test_tradingnote_technical.py；真實2330共401筆資料；離屏GUI 24指標×4區間×4資料情境。離屏截圖文字呈方框，尚未驗證實際Windows視窗字型；圖線及互動切換通過。py_compile遇既有pyc覆寫權限問題，改以AST解析與實際import驗證語法。

## 2026-08-27：資金流向頁改用分類按鍵切換
- **介面**：將產業泡泡圖、產業熱度排行、資金流入前 50、個股爆量四個區塊改成四個按鍵與單一內容頁，避免所有大型表格同時垂直堆疊，縮短主頁捲動距離。
- **區間口徑**：所有小分類統一使用最上方「流向區間」；移除產業排行內重複的資料區間選擇器，個股爆量的均量天數也跟隨同一區間。
- **穩定性**：資金流向兩張新增表格改用標準 `QTreeWidgetItem`，由核心計算結果先排序並停用原生自訂數值排序，避開部分 Windows Qt/PySide6 環境初始化時的原生崩潰。

## 2026-08-27：個股技術分析（KD／MACD／均線／RSI）
- **資料與架構**：新增 `tradingnote_technical.py` 純本地計算層；個股明細與部位紀錄共用一次 FinMind `TaiwanStockPrice` 150 日 OHLCV，從同一份資料計算 KD、MACD、MA5／20／60、RSI，以及既有 VPT／MFI。
- **介面**：個股完整明細與部位紀錄各新增「技術分析」分頁，可切換 KD、MACD、均線並顯示最新值；沒有足夠資料時顯示明確提示。
- **額度**：技術指標不增加 API 存取次數；上市個股完整明細目前最多 8 個不同 FinMind 資料集請求，`TaiwanStockPrice` 實測因行程內快取只請求 1 次。上櫃估值走原有 TPEX 官方端點，FinMind 請求數少 1。
- **驗證**：已通過語法 AST 檢查、80 日合成資料指標計算、技術分析元件三模式切換、完整主視窗無網路建構，以及完整個股明細的 8 次資料集／1 次價格查詢快取測試。
- **架構**：新增無 UI 相依的 `tradingnote_flow.py`，以 `FlowPeriod` 統一期間、`FlowDashboardData` 統一結果、`FlowAnalysisService` 統一分析與最近結果快取；GUI 刷新時只建立一次儀表板資料，四個子分類共用同一批結果。估值泡泡圖的短期窗口由選定區間限制，長期窗口使用選定流向區間。
- **爆量清單**：新增「族群」欄位，使用既有產業分類；表格欄位排序與雙擊股票查詢位置同步調整。
- **驗證**：`.venv` headless 建立完整 `TradingNoteWindow` 成功；產業清單可渲染 35 筆，個股排行可渲染 50 筆；語法檢查通過。

## 2026-08-26：產業人氣／動能／量能量化排行與個股資金流入前50名
- **動機**：單看產業成交金額會偏向半導體等大型產業，無法分辨「最有人氣」「最有動能」與「突然放量」是否是同一批族群；需要把資金流向拆成可解釋、可排序的指標。
- **產業分數**：`IndustryFlow` 新增三個 0～100 的同批產業橫斷面百分位與一個綜合分數：人氣分數＝今日資金比重百分位、動能分數＝選定區間加權漲跌百分位、量能分數＝今日量比百分位；綜合熱度＝三者等權平均。先轉百分位再平均，避免成交金額／百分比／倍數不同單位互相主導。分數是當次查詢的相對排名，不代表跨日期固定的絕對分數。
- **GUI**：資金動向清單新增熱度排名、今日資金比重、人氣／動能／量能／綜合熱度欄位，預設按綜合熱度由高到低；欄位可正確依原始數值排序。原本的產業點擊行為保留。
- **個股排行**：新增 `compute_stock_capital_flow()` 與「個股資金流入前50名」表格，依選定區間累積成交金額排序，顯示區間漲跌、量比與全市場比重，雙擊沿用 `StockDetailDialog`。這是成交金額代理，不是法人淨買超，UI 已明確註記。
- **驗證**：語法檢查與 `git diff --check` 通過；真實 `data/history.db`／`price_cache.json` 驗證 35 個產業、33 個產業有完整三分數，所有分數落在 0～100，綜合分數等於三者平均；個股排行仍能產生 50 筆且排序正確。當前 bundled Python 缺少 PySide6，因此未能完成 GUI headless smoke test。

## 2026-08-21：「產業成分股」彈窗表格新增 EPS 欄位
- **動機**：使用者要求「泡泡圖以及各股列表 本益比後面顯示 eps」——「資金流向分析」頁的產業泡泡圖（點擊泡泡）跟下方「資金動向清單」（點擊清單列）共用同一個彈窗 `IndustryTopStocksDialog`，本益比欄後面接著看 EPS。
- **改法**：全專案的估值資料來源（TWSE/TPEX bulk 端點、FinMind `TaiwanStockPER`）都沒有現成 EPS 欄位，改用 `EPS = 現價(close) ÷ 本益比(per)` 純本地換算（`per` 為 `None`／`0` 時顯示 `N/A`，避免除以零），不新增任何 API 呼叫。只改 `tradingnote_gui.py` 的 `IndustryTopStocksDialog.__init__`：表格欄位數 8→9、表頭本益比後加「EPS」、填值與置中對齊邏輯同步加上新欄。
- **已驗證**：`ast.parse`／語法檢查通過。**未實機開 GUI 目視驗證**。

## 2026-08-19：「歷史股價」趨勢圖新增滑鼠點擊查看當日股價
- **動機**：使用者要求「歷史股價圖用滑鼠點擊能夠顯示該日股價」——原本 `_populate_price_chart` 只能把滑鼠移到線上目測，沒有明確標出點擊那天的確切數值。
- **改法**：`tradingnote_gui.py` 新增 `_setup_price_chart_click(chart)`，接 `chart.scene().sigMouseClicked`，用 `ViewBox.mapSceneToView()` 把場景座標轉成資料座標後四捨五入取最接近的交易日索引，畫一條垂直虛線（`pg.InfiniteLine`）＋文字（`pg.TextItem`）標「日期｜收盤價」；點擊前會先 `removeItem` 掉上一次點擊留下的標記，避免疊加。`_populate_price_chart` 每次重繪（含 `chart.clear()`）時把當下的 `dates`／`closes` 存到 `chart._price_dates`／`chart._price_closes` 供 handler 讀取，並重置 `chart._click_marker_items = []`，讓 handler 不會 remove 到已經被 `clear()` 清空、不存在的舊物件。`_setup_price_chart_click()` 只在 PlotWidget 建立時呼叫一次——`StockDetailDialog._build_full_detail_widgets` 的 `full_detail_price_chart`、`PositionsTab._build_position_detail_section`（部位紀錄頁）的 `position_price_chart`——不會因為 `_populate_price_chart` 多次重繪而重複連線、疊加 handler。只動「歷史股價」這一張圖，其他趨勢圖（三大法人／法人分別／融資融券／VPT／MFI）不受影響。
- **已驗證**：`py_compile` 過；headless（offscreen platform）用假資料建立 `PlotWidget`，透過 `chart.scene().sigMouseClicked.emit()` 模擬點擊事件（自製假 event 物件提供 `scenePos()`），確認點擊場景座標正確換算回資料索引、文字標籤顯示的日期與收盤價跟資料吻合、連續點擊兩次只保留最新一組標記（不累加）、`_populate_price_chart` 重繪後標記正確歸零不殘留。**未實機用滑鼠目視點擊驗證**（沿用先前螢幕自動化不穩定的結論）。

## 2026-08-18：修正泡泡圖「近N日」（尤其近2日）股價漲幅算錯的 bug
- **動機**：使用者反映「泡泡圖選擇近2日沒有股價漲幅」。
- **根因**：`compute_industry_flow`／`compute_valuation_flow`／`get_industry_top_stocks_range`／`compute_volume_ratio_outliers` 這幾個「近N日流向」函式，原本都用 `date.today()`（系統時鐘的今天）當「歷史資料」跟「即時 snapshot」的分界；但 snapshot 代表的是「TWSE/TPEX 目前實際發布的最新收盤」，不一定等於今天日曆日期（一早、假日、或個股資料延遲發布時會落後）。當 snapshot 落後、且 avg_days 很小（近2日最明顯）時，算出的「N 天前」基準日會剛好撞到 snapshot 本身那天，變成同一天收盤價互相比較，結果幾乎是 0%。更深一層：即使用「snapshot 眾數日期」當分界，個別權值股（實測台積電 2330）自己的 snapshot 日期可能比多數股票還舊，一樣會撞期、算出剛好 0.0%。
- **修法**：新增 `_snapshot_today(snapshot)`（用眾數日期取代 `date.today()`）＋在四個函式的逐檔迴圈裡改用**該檔股票自己的 snapshot 日期**過濾歷史列（不是只看全域眾數），徹底避免撞期。
- **已驗證**：`py_compile` 過；用真實即時 TWSE/TPEX snapshot 重現問題（修正前台積電 2330 近2日漲跌% = 0.0，跟即時 snapshot 本身落後於多數股票同一天精確對應）、修正後 = -1.64%（合理非零值）；`compute_industry_flow` 近2日全市場35產業漲跌%範圍從修正前 -2.67%~+2.19%（明顯偏窄、失真）變成 -5.93%~+9.07%（跟近5日/近20日級距合理遞增一致）；四個函式＋泡泡圖端到端 headless（`populate_flow_chart`，momentum／valuation 兩種模式，avg_days=2）皆正常渲染無例外。**未實機開 GUI 目視驗證**。

## 2026-08-17：個股詳細資訊新增「借券賣出餘額」與「融資成本（估算）」
- **動機**：使用者原始要求還包含「融券餘額」（已存在，既有「融資融券」文字行本來就有）跟「融資維持率」；後者查遍 FinMind／TWSE OpenAPI（143 端點）都沒有逐股公開的「融資成數」資料，無法算出可信維持率，跟使用者確認後**不做**（見 `tradingnote_finmind.py` 模組開頭 docstring 的完整說明）。
- **改法**：實際新增兩項：①`fetch_short_sale_balance()`（新，`TaiwanDailyShortSaleBalances` 的 `SBLShortSalesCurrentDayBalance`）——這個資料集之前完全沒查，是因為誤判成跟既有融券餘額重複；重新比對後發現只有 `MarginShortSales*` 半組重複（單位不同的同一件事，繼續不查），`SBLShortSales*`（有價證券借貸，另一個信用管道）是真正沒查過的新資料。②`fetch_margin_cost_estimate()`（新）——融資成本非公開資料，用「移動加權平均法」從 `TaiwanStockMarginPurchaseShortSale` 歷史融資餘額日增減 × 當日收盤價反推估算值，UI 上明確標「估算」；`margin_lookback_days=120`／`price_lookback_days=150` 故意跟 `fetch_margin_short_sale_history`／`fetch_vpt_mfi_history` 預設值一致，讓 `fetch_position_detail` 命中同一份 `_fetch_dataset` 快取、不多打 API。兩者都加進 `POSITION_DETAIL_FIELDS`／`fetch_position_detail`，`_render_detail_block` 用 `data.get(...)` 讀（舊快取沒有這兩個 key 時顯示「查無資料」、不 crash）。
- **已驗證**：`py_compile` 過；headless 對真實 2330 呼叫兩個新函式（借券賣出餘額 16,079,514 股；融資成本估算 2207.60 元，跟同期實際收盤價 2400 附近同量級，方向合理）；`_render_detail_block` 實際渲染文字含兩行新資訊；模擬舊快取（缺新 key）不 crash、正確顯示「查無資料」。**未實機開 GUI 目視驗證**（沿用先前螢幕自動化不穩定的結論，這次未再嘗試）。

## 2026-08-17：所有視窗／對話框自動符合螢幕大小
- **改法**：`tradingnote_gui.py` 新增 `_screen_fit_size()`／`_center_on_screen()` 兩個共用 helper（用 `widget.screen().availableGeometry()`），取代原本寫死的像素尺寸：主視窗 `TradingNoteWindow` 從固定 `resize(1040,720)` 改成螢幕可用工作區 85% 等比縮放＋置中；`StockDetailDialog` 完整籌碼面資訊視窗從固定 `700x620` 改成偏好尺寸與螢幕 90% 兩者取小；`IndustryTopStocksDialog`／`FuturesLargeTradersDialog` 表格高度上限從寫死 `760` 改成螢幕可用高度 80%。其餘小型進度/表單對話框內容本來就小，未受影響。
- **已驗證**：`py_compile` 過；headless（offscreen platform）呼叫 helper、實際建立 `IndustryTopStocksDialog`／`FuturesLargeTradersDialog` 均無例外；**實機screenshot 驗證主視窗**確實開啟在螢幕可用工作區 85%、置中、非最大化（過程中發現用 `Start-Process` 啟動會被 Windows 自動最大化、用一般前景啟動才正確，跟程式本身邏輯無關，是啟動方式的環境雜訊）；兩個對話框（`StockDetailDialog` 完整籌碼面資訊／`IndustryTopStocksDialog`）**未能在實機完成滑鼠互動目視驗證**——這台機器螢幕有 125% DPI 縮放，桌面上同時開很多其他視窗，PowerShell 模擬滑鼠座標一度誤觸/截到別的視窗（Steam 分頁），怕誤觸使用者其他應用而中止，改以 headless 檢查＋建議使用者自行點開確認。

## 2026-08-17：所有對話框（QDialog）標題列加放大鈕，支援雙擊標題列填滿螢幕
- **改法**：主視窗 `TradingNoteWindow`（`QMainWindow`）本來就有這個能力（Windows 原生行為），但 Qt 對話框預設不給標題列放大鈕。在 9 個 `QDialog` 子類別（`PositionFormDialog`／`PriceLookupDialog`／`StockDetailDialog`／`IndustryTopStocksDialog`／`FuturesLargeTradersDialog`／`BackfillDialog`／`TpexBackfillDialog`／`RefreshDialog`／`TradingDateDialog`）的 `__init__` 開頭各加一行 `self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowMaximizeButtonHint)`。`StartupProgressDialog`（啟動載入畫面）刻意不動——它本來就故意拿掉包含關閉鈕在內的所有按鈕，是有意設計。
- **已驗證**：`py_compile` 過；headless 建立 6 個代表性對話框，逐一確認 `windowFlags() & WindowMaximizeButtonHint` 為 True；**未實機目視點擊驗證**（原因同上，這台機器目前不適合做滑鼠自動化）。

## 2026-08-17：VPT／MFI 圖加上「最新一天」數值標註
- **改法**：`tradingnote_gui.py` 新增 `_mark_latest_value()` helper，在 `_populate_vpt_chart`／`_populate_mfi_chart` 各加一條貫穿全圖寬的水平虛線（高度＝最後一筆資料值）＋左側文字「數值｜日期」，方便一眼看到今天的值，不用把滑鼠移到線最右端對。VPT 用千分位整數格式，MFI 用一位小數；顏色沿用各自線本身的顏色（VPT `#17becf`、MFI `#bcbd22`）。只動這兩張圖，其他趨勢圖不變。
- **已驗證**：`py_compile` 過，**未實機開 GUI 目視驗證**。

## 2026-08-14：期貨頁新增「大額交易人未沖銷部位」（TAIFEX OpenInterestOfLargeTradersFutures）
- **動機**：使用者要求把 TAIFEX `OpenInterestOfLargeTradersFutures` 端點加進「期貨」模組（大額交易人未沖銷部位＝期貨版籌碼面，類比股票的三大法人）。
- **端點特性（先實測）**：`https://openapi.taifex.com.tw/v1/OpenInterestOfLargeTradersFutures`，免金鑰、一次回傳最近一交易日全市場（實測 1386 筆），欄位 `Contract`／`ContractName`／`SettlementMonth`／`TypeOfTraders`（0＝所有交易人、1＝特定法人）／`Top5Buy`／`Top5Sell`／`Top10Buy`／`Top10Sell`（前5／前10大交易人買賣方未沖銷部位，口數）／`OIOfMarket`（全市場未沖銷）。`SettlementMonth` 特殊值：`999912`＝所有契約合計（692 筆、多數商品都有）；另有極少數 `666666`（實測僅 TX 兩筆、`OIOfMarket=1` 的佔位資料，語意不明，原樣顯示不臆測）。**契約碼跟日盤行情端點不完全一致**：大額端點 346 個契約、日盤 374 個，且大額端點**沒有 MTX**（小型臺指併入 `TX`，其 ContractName 就叫「臺股期貨(TX+MTX/4)」），許多日盤商品在大額端點查無對應。
- **改法**：
  - `tradingnote_taifex.py`：新增常數 `TAIFEX_LARGE_TRADERS_FUTURES_URL`／`LARGE_TRADERS_TYPE_LABELS`（0→所有交易人、1→特定法人）／`LARGE_TRADERS_ALL_CONTRACTS_MONTH="999912"`；新增 `fetch_large_traders_futures_report()`、`_normalize_large_traders_row()`、`get_large_traders_for_product(product, rows)`（依 `SettlementMonth` 分組、每組收齊兩種 trader_type、真實月份由近到遠、999912 排最後，機制／快取模式全比照既有 `get_front_month_sessions`／`get_cached_daily_futures_report`）、`get_cached_large_traders_futures_report(cache_path, force_refresh)`（獨立快取檔、同一套 `FUTURES_CACHE_TTL_SECONDS` 與失敗退回舊快取邏輯）。
  - `tradingnote_gui.py`：新增 `FUTURES_LARGE_TRADERS_CACHE_PATH`（`data/futures_large_traders_cache.json`，`data/` 已 gitignore）；新增 module 層級 `_futures_date_to_iso()`／`_format_settlement_month()`（999912→「所有契約」、6碼→「YYYY/MM」、其他原樣）；新增 `FuturesLargeTradersDialog`（表格式彈窗，機制比照 `IndustryTopStocksDialog`，欄位：到期月份／交易人類別／前5大買賣／前10大買賣／前10大淨〔買-賣，正綠負紅〕／前10大買佔比〔Top10Buy÷OIOfMarket〕／全市場未沖銷）。`refresh_futures_tab` 的背景 `fetch()` 多抓一次大額端點（**用 try/except PriceFetchError 包住、失敗退空清單，不讓主行情表跟著壞**），回傳從 2-tuple 擴成 3-tuple，存進 `self._futures_large_traders_rows`。期貨表格接上 `cellDoubleClicked` → `_on_futures_row_double_clicked`：就地 `get_large_traders_for_product` 篩出該商品資料開彈窗（不另打 API）；查無資料時區分「整份還沒載入（提示重新整理）」vs.「已載入但 TAIFEX 沒單獨提供此商品（如 MTX，重新整理也不會有）」兩種訊息。期貨頁 hint 補一句「雙擊某列可查大額交易人」。
- **驗證**（專案 `.venv` Python 3.12.10）：`py_compile` 兩檔過。真實網路：`fetch_large_traders_futures_report` 1386 筆；`get_large_traders_for_product('TX')` 3 組（202608／666666／999912）各含兩 trader_type，`FuturesLargeTradersDialog` headless 建構後表格 6 列 9 欄、數值與格式正確（例：所有交易人 202608 前10大買 65,915／淨 +696／佔比 72.1%／全市場 91,453）；`get_cached_...` 首打寫檔、二打命中快取（皆 1386）；`_on_futures_row_double_clicked` 三路徑（TX→開彈窗、MTX→「未單獨提供」訊息、rows 空→「尚未載入」訊息）皆正確；格式函式 202608→2026/08、999912→所有契約、666666→原樣、20260814→2026-08-14 皆正確。**沒有**：用滑鼠實際雙擊（終端機無 Accessibility，走程式碼路徑＋stub 綁定 method 驗證）；沒有建構完整 `TradingNoteWindow` 跑一次 `refresh_futures_tab`（會觸發網路 preload；fetch closure 的回傳 tuple 形狀與 on_done 解包已對齊、大額抓取包在 try/except 內不影響主表）。

## 2026-08-14（續3）：期貨大額交易人未沖銷部位「歷史」自動回補（綁定回補天數）＋趨勢圖
- **動機**：使用者要「自動補、綁定回補天數、並做趨勢圖」。先前只有 openapi 最新一天（`OpenInterestOfLargeTradersFutures` 無日期參數），沒有歷史可畫趨勢。
- **資料源（實測）**：openapi 端點確認**無任何參數、只回最新一天**，無法回補。改用 TAIFEX 網站歷史 CSV 下載端點 `https://www.taifex.com.tw/cht/3/largeTraderFutDown`（POST `queryStartDate`／`queryEndDate`、Big5 編碼、免金鑰、`.venv` 3.12 預設 SSL 可連）。**單次查詢跨度有上限**：90 天可、120 天被擋回 HTML 錯誤頁 → 回補時分段（保守用 60 曆日／塊）。CSV 的「所有契約合計」到期月別是 **`999999`**（跟 openapi 即時端點的 `999912` 不同，別搞混）。
- **改法**：
  - `tradingnote_http.py`：新增 `http_post_text(url, data, encoding, timeout)`（POST 表單→解碼文字，例外包裝同 `http_get_json`）。
  - `tradingnote_history.py`：`_connect` 新增表 `large_traders_history(date, contract, settlement_month, trader_type, contract_name, top5/10_buy/sell, market_oi, PK(date,contract,settlement_month,trader_type))`＋索引；新增 `upsert_large_traders_history`／`get_large_traders_history_date_range`／`get_large_traders_history_series`。`contract` 是大額端點代碼（TX／CD／BRF；股票期貨去尾 F），日期存 ISO。
  - `tradingnote_taifex.py`（**新 import `tradingnote_history`**，無循環）：新增 `fetch_large_traders_history_range(start,end)`（打 CSV、解析成可 upsert 的 tuple）、`_date_chunks`、`backfill_large_traders_history(db_path, target_days, delay_seconds, on_progress)`——冪等/可續跑：以已存最新日「之後」往今天補（forward，資料已最新時 `forward_start>today` 整段跳過不發請求）、只有缺的舊資料超過期望起點 >7 曆日才往前延伸（backward，避免 desired_start 落在非交易日每次無謂重抓），每段再切 ≤60 天塊下載。
  - `tradingnote_gui.py`：啟動時（`auto_check_continuity` 開關內、緊接 `_sync_history_continuity()` 之後）呼叫新的 `_sync_large_traders_history()`——`run_task_in_thread` 背景跑 `backfill_large_traders_history(HISTORY_DB_PATH, backfill_target_days)`，狀態列顯示「期貨大額交易人歷史回補中...」（新增 `self.large_traders_note`，接進 `_update_status_bar`），補到資料後刷新目前選取商品趨勢圖。**趨勢圖**：`_build_futures_lt_panel` 的明細面板改成 `QSplitter(Horizontal)`：左明細表、右新增 `futures_lt_trend_chart`；新增 module 層級 `_populate_large_traders_trend(chart, series)`（畫「所有契約合計・所有交易人」前10大買/賣方未沖銷部位兩條線，比照 `_populate_margin_chart` 直接畫原始口數；用所有契約合計而非近月，避免月換倉斷點）。`_on_futures_row_selected` 選取商品時一併查 `get_large_traders_history_series(HISTORY_DB_PATH, lt_code, 999999, '0')` 畫趨勢。「設定」頁把「回補天數」「每次啟動自動檢測」文案補上「期貨大額交易人歷史」也吃這兩個設定。
- **驗證**（`.venv` 3.12.10）：四檔 `py_compile` 過。真實網路＋暫存 DB：`backfill_large_traders_history(target_days=20)` 首次 1 塊 20,690 列、日期 07-27～08-14；**再跑同 target chunks 的 forward 段回 0 列**（冪等，只發一個空請求）、target 加大到 45 正確往前延伸到 07-01；`get_large_traders_history_series('TX',999999,'0')` 15 筆數值正確。Headless（綁定 method）：選 TX→趨勢圖 2 條線、標題「…近 15 日」、範圍正常、明細表 6 列；選 CDF（→CD）趨勢 2 線、明細 4 列；空歷史 `_populate_large_traders_trend([])` 顯示「尚無歷史…」不 crash。**沒有**：實機啟動跑一次完整自動回補（背景執行緒＋真實 `data/history.db`）；沒有實機滑鼠操作。**注意**：首次啟動 target=120 會抓約 2 塊（~11 萬列）約數秒，與 TWSE 股價同步併發寫 `history.db`（不同表、WAL、timeout=30，無虞）。

## 2026-08-14（續2）：期貨頁改成預設列全部、法人未沖銷部位直接顯示（摘要欄＋常駐面板）、文字搜尋
- **動機**：使用者三點要求——①預設直接呈現全部商品、用搜尋才篩掉不符的；②法人（大額交易人）未沖銷部位「直接顯示」不要只藏在雙擊；③可用文字（中文名）搜尋期貨商品。確認過②要「兩者都要」：主表格加近月摘要欄＋表格下方常駐明細面板。
- **改法（`tradingnote_gui.py` 為主，`tradingnote_taifex.py` 加兩個共用函式）**：
  - **①預設列全部**：`_filter_futures_table` 關鍵字為空時改成全部 `setRowHidden(False)`（原本只留 `DEFAULT_FUTURES_PRODUCTS`）。實測全部約 404 列（374 商品 × 有資料的時段）。
  - **②法人未沖銷直接顯示（兩者都要）**：
    - **主表格摘要欄**：`FUTURES_COLUMNS` 末尾加「大額前10買／大額前10賣／大額前10淨／大額買佔比」四欄；`_paint_futures_row` 用新的 `_large_traders_summary_entry(groups, contract_month)`（優先近月那組、其次所有契約合計、再不然第一組，取「所有交易人」）算摘要，前10淨正綠負紅。
    - **表格下方常駐面板**：`_build_futures_lt_panel`（label＋table，包在 `QSplitter(Vertical)` 裡，表格上、面板下，比照部位頁排版）；`futures_table.itemSelectionChanged` → `_on_futures_row_selected` 點選某列即用 `_populate_large_traders_table` 填該商品完整明細（各到期月份 × 所有交易人／特定法人）。雙擊彈窗 `FuturesLargeTradersDialog` 仍保留（較大檢視），改成跟面板共用 module 層級 `LARGE_TRADERS_TABLE_COLUMNS`／`_populate_large_traders_table`（把原本寫在 dialog 裡的填表邏輯抽出來）。
  - **③文字搜尋**：`tradingnote_taifex.py` 新增 `build_large_traders_name_map(rows)`（`{大額契約代碼: ContractName}`，如 BRF→布蘭特原油期貨、TX→臺股期貨(TX+MTX/4)）與 `group_large_traders_all(rows)`（一次分好 `{大額代碼: groups}`，避免逐商品重掃全表）。GUI `refresh_futures_tab.on_done` 建 `self._futures_lt_by_code`（大額分組索引）與 `self._futures_name_map`（日盤商品代碼→中文名，key 用 `large_traders_code_for_product` 換算）。「標的」欄與搜尋比對字串都吃這份名稱：股票期貨用 ssf 的「簡稱 代號」（台積電 2330）、其餘用中文契約名（布蘭特原油期貨）——所以指數／商品期貨現在也有名字可看、可搜。`_futures_display_name`／`_futures_groups_for_product` 兩個共用小 method 收斂重複邏輯。
- **驗證**（`.venv` 3.12.10）：`py_compile` 兩檔過。綁定 method 的 headless 整合測試（真實 daily／SSF／大額）：關鍵字空時 404 列 0 隱藏（預設全顯示）；TX 列摘要欄 65,915／65,219／+696／72.1%、CDF 列標的「台積電 2330」摘要 3,294／7,656／-4,362／30.1%、BRF 列標的「布蘭特原油期貨」摘要 57／55／+2／98.3%；搜尋「台積電」→CDF＋QFF、「布蘭特」→BRF（中文名搜尋有效）；點選 CDF 面板顯示「CDF 台積電 2330　大額交易人未沖銷部位」4 列（近月＋所有契約 × 兩類）、點選 MTX 面板顯示「未單獨提供」0 列。**沒有**：實機滑鼠點選／雙擊；沒建整個 `TradingNoteWindow`（走 stub 綁定 method＋真實資料，同前）。

## 2026-08-14（續）：期貨頁個股期貨顯示標的名稱、可用股票代號／名稱搜尋、大額支援
- **動機**：使用者反映「個股期貨也要能顯示」。日盤行情端點（`DailyMarketReportFut`）**沒有名稱欄位**，股票期貨只有不具名的契約代碼（如 CDF），使用者看不懂、也只能用代碼搜尋。
- **端點（依使用者給的 openapi 目錄找到）**：`https://openapi.taifex.com.tw/v1/SSFLists`（股票期貨交易標的，320 檔），欄位 `Contract`（CDF）／`StockCode`（2330）／`StockName`（台積電）／`UnderlyingStock`（全名）／`Type`。實測 319/320 個 SSF 代碼有在日盤行情裡（另 1 檔 KAF 當日無行情）。
- **大額端點代碼對照（實測發現）**：股票期貨在「大額交易人未沖銷部位」端點的代碼是**去掉結尾 F**（日盤 CDF → 大額 CD，全 320 檔吻合）；指數／商品期貨（TX、BRF…）沿用原碼。所以先前的大額雙擊對股票期貨查不到，是代碼差一個 F，不是沒資料。
- **改法**：
  - `tradingnote_taifex.py`：新增 `TAIFEX_SSF_LIST_URL`、`fetch_ssf_list()`、`get_cached_ssf_list()`（快取模式同其他期貨函式）、`build_ssf_map(rows)`→`{Contract: {stock_code, stock_name, underlying}}`、`large_traders_code_for_product(product, ssf_map)`（股票期貨去尾 F、其餘原碼）。
  - `tradingnote_gui.py`：新增 `FUTURES_SSF_CACHE_PATH`；`FUTURES_COLUMNS` 在「商品」後插入「標的」欄（欄寬陣列、`_paint_futures_row` 的 values 同步；著色欄從 (3,4) 位移到 (4,5)）；`_paint_futures_row` 用 `self._futures_ssf_map` 填標的「股票名 代號」（非股票期貨顯示 `-`）；`_filter_futures_table` 搜尋比對字串加入股票代號＋名稱；`refresh_futures_tab` 的 `fetch()` 多抓 SSFLists（同樣 try/except 包住、失敗退空 map 不影響主表），回傳 tuple 擴成 4 元素、`on_done` 存 `self._futures_ssf_map`；`_on_futures_row_double_clicked` 改用 `large_traders_code_for_product` 換算大額端點代碼再查。hint 補充可用股票代號／名稱搜尋。
- **驗證**（`.venv` 3.12.10）：`py_compile` 兩檔過。真實資料：`build_ssf_map` 320 檔、CDF→台積電 2330；`large_traders_code_for_product` CDF→CD、TX→TX、BRF→BRF；CDF 經 CD 查到大額 2 組（202608／999912）。綁定 method 的 headless 整合測試（真實 daily／SSF／大額）：CDF 那列「標的」欄顯示「台積電 2330」、行情數值正確；搜尋 `2330`／`台積電`／`CDF`／`TX` 各自篩出預期列（`2330`／`台積電` 命中 CDF＋QFF 兩檔台積電相關期貨）；雙擊 CDF 開出標題「CDF 台積電期貨　大額交易人未沖銷部位（2026-08-14）」的彈窗。**沒有**：實機滑鼠操作；沒有跑完整 `TradingNoteWindow`（同上，走 stub 綁定 method＋真實資料驗證）。

## 2026-08-14：個股／部位詳細資訊新增「法人分別（五細項）累計買賣超」圖；分點暫緩
- **動機**：使用者要在「個股」頁 `StockDetailDialog` 彈窗與「部位紀錄」頁詳細區塊各加兩個功能：①分點資訊（券商分點進出，可選日期、顯示前15名）；②法人分別資金流動（現有只有合併三大類的120日趨勢）。
- **①分點——確認免費資料源拿不到，這次沒做**：
  - FinMind `TaiwanStockTradingDailyReport`（券商分點）用免費 token 直接回 HTTP 400「Your level is free. Please update your user level.」，需付費 Sponsor 方案（跟既有 `tradingnote_finmind.py` docstring 記載的 `TaiwanStockHoldingSharesPer` 同一種 tier 限制）。
  - 依使用者指示改查 TWSE OpenAPI（`https://openapi.twse.com.tw/v1/swagger.json`，共 143 個端點全掃過關鍵字「分點／券商／進出／成交明細／買賣」）：只有 `/brokerService/brokerList`（券商總公司基本資料）、`/opendata/OpenData_BRK02`（券商分公司基本資料）、`/opendata/t187ap20`（各券商每月月計表）、`OpenData_BRK01`（營業員男女統計）等**公司層級**端點，**沒有**任何「個股 × 券商分點買賣超」的資料端點。TWSE 個股分點買賣超（e-service 那類）沒有公開的免費 JSON API。
  - 結論：免費管道確認做不到，等使用者提供付費 FinMind token（並一併補上「設定」頁的 token 輸入欄，見既有缺口）或找到其他來源再實作。
- **②法人分別——已做**：
  - `tradingnote_finmind.py`：新增常數 `INSTITUTIONAL_DETAIL_BUCKETS`（五細項：外資=Foreign_Investor／外資自營商=Foreign_Dealer_Self／投信=Investment_Trust／自營商(自行)=Dealer_self／自營商(避險)=Dealer_Hedging；舊制合併碼 `Dealer` 不納入，遇到只有舊碼的日期該天兩個自營商細項為 0，屬可接受邊角）。新增 `fetch_institutional_investors_detailed_history(ticker, token, lookback_days=120)`：資料來源／區間跟既有 `fetch_institutional_investors_history`（合併三大類）完全相同、走 `_fetch_dataset` 同一筆快取（lookback 120），只是不合併、五細項各自一條逐日淨買賣超序列；回傳 shape 跟三大類版本一致（`{"dates", "series"}`），累積由 GUI 端計算。接進 `POSITION_DETAIL_FIELDS`（插在 `institutional_history` 之後）與 `fetch_position_detail`（多這張圖**不會多一次 API**，同一 ticker 的三大類趨勢與五細項命中同一筆快取）。
  - `tradingnote_gui.py`：新增 module 層級 `_populate_institutional_detail_chart(chart, detail_history)`，畫法完全比照既有 `_populate_flow_chart`（逐日淨額 running sum 成累計曲線、加零軸虛線參考線、用算好的資料範圍 `setXRange`/`setYRange` 避開隱藏分頁 `enableAutoRange()` 陷阱），差別只是五條線五種顏色、標題「法人分別累計買賣超」。`_render_detail_block` 簽章在 `flow_chart` 後插入 `institutional_detail_chart` 參數、body 在 `_populate_flow_chart` 後呼叫新函式（用 `data.get("institutional_detail_history")`，舊快取缺 key 時空白不 crash，比照 `price_history` 慣例）。`StockDetailDialog._build_full_detail_widgets` 與 `TradingNoteWindow._build_position_detail_section` 各新增一個 `pg.PlotWidget`（`full_detail_institutional_detail_chart`／`position_institutional_detail_chart`）＋在「三大法人」分頁後 `addTab` 一個「法人分別」；四處 `_render_detail_block` 呼叫（StockDetailDialog 三處：cached／done／error；部位頁 `_render_position_detail` 一處）與三處清空圖表（`_on_position_row_selected`／`_load_position_detail` else／`_on_position_detail_error`）同步補上新 widget。**注意**：`_render_detail_block` 呼叫與清空圖表區塊在不同 method 裡縮排不同（12 空格 vs 16／8 空格），批次取代時要逐一確認每一處都補到，本次改動就踩過一次「replace_all 只換到同縮排的、漏掉不同縮排的那處」。
- **驗證**（專案 `.venv` Python 3.12.10，非系統 3.14）：`py_compile` 兩個改動檔過。Headless（`QT_QPA_PLATFORM=offscreen`）真實網路對 2330：`fetch_institutional_investors_detailed_history` 回傳 5 條 series（外資最新三日約 +3.5M/+2.6M/+4.0M 股，數量級合理）；`fetch_position_detail` 含 `institutional_detail_history` 且非 None。`_render_detail_block` 用真實 `fetch_position_detail` 結果實際渲染六張圖無例外：法人分別圖 5 條曲線、x/y range 非退化、標題正確；回歸確認三大類圖仍 3 條；刪掉 `institutional_detail_history` key 模擬舊快取，`_populate_institutional_detail_chart` 收到 None 不 crash。`StockDetailDialog` 建構後 `full_detail_tabs` 6 頁順序正確（歷史股價／三大法人／法人分別／融資融券／VPT／MFI）。**沒有**：用真實付費 token 測分點（沒有 token）；沒有用滑鼠實際點分頁切換（終端機無 Accessibility，比照歷次改動走程式碼路徑驗證）；沒有建構完整 `TradingNoteWindow` 選部位跑一次（會觸發網路 preload／背景執行緒，部位頁與個股頁共用同一份 `_render_detail_block`，已由 render 測試涵蓋）。**目前沒有 FinMind token**（`data/settings.json` 不存在），全走匿名免費額度。

## 2026-08-13：產業成分股彈窗新增近日量／均量／本益比
- **動機**：使用者要求「泡泡圖點擊顯示的群組類股要顯示近日成交量以及均量、本益比，以及有在快取內的資料」——即點擊「資金流向分析」頁產業泡泡（或資金動向清單）彈出的 `IndustryTopStocksDialog`，明確要求只用本地快取（`daily_prices`／`valuation_history`），不額外打 API，跟這個彈窗原本「純本地資料」的原則一致。
- **改法**：
  - `tradingnote_history.py`：`get_industry_top_stocks_range(db_path, snapshot, industry, days, top_n=30, volume_avg_days=5)` 新增 `volume_avg_days` 參數（預設值跟既有「個股量比異常清單」`compute_volume_ratio_outliers` 的 `avg_days` 預設一致）。`daily_prices` 查詢加入 `volume` 欄位；新增第二個查詢抓該產業所有 ticker 在 `valuation_history` 的 `per`（`date DESC` 排序，比照 `compute_valuation_flow` 的「第一次出現即最新」寫法）。回傳的 dict 新增三個 key：`today_volume`（直接取自 snapshot）、`avg_volume`（近 `volume_avg_days` 天 `daily_prices.volume` 簡單平均，沒歷史則 `None`）、`per`（該股最新一筆本益比，沒資料則 `None`）。均量／本益比的天數語意跟 `days`（排序用的累積成交金額區間）無關，固定用 `volume_avg_days`。
  - `tradingnote_gui.py`：`IndustryTopStocksDialog` 表格從 5 欄擴到 8 欄，新增「近日量(張)」「均量(張)」「本益比」（股→張換算 `/1000`，寫法比照既有「個股量比異常清單」的 `today_volume(張)` 欄；本益比缺資料顯示 `N/A`，跟既有 FinMind 本益比顯示慣例一致，均量缺資料顯示 `-`）。
- **已驗證**：`python3 -m py_compile` 兩個改動檔案皆過。Headless 用真實 `data/history.db`／`data/price_cache.json` 呼叫 `get_industry_top_stocks_range`（電子零組件業，210 檔中前10大成分股），10 筆全部拿到非 None 的 `per`／`avg_volume`；抽查 2327（國巨）：`per=47.40` 對照 `valuation_history` 該股最新一筆（`date=2026-08-13, per=47.4`）吻合；`avg_volume=54329134.2` 手算近5日 `daily_prices.volume`（56066841/81650622/36354305/44277453/53296450）平均值吻合。GUI 對話框 headless（`QT_QPA_PLATFORM=offscreen`）實際 instantiate，確認 8 欄標題與數值格式（千分位張數、本益比小數兩位）正確顯示、無例外。

## 2026-08-12：泡泡圖「估值」模式改成「最新 PER/PBR」，拿掉整套歷史回補機制
- **動機**：使用者反映「泡泡圖看起來沒有覆蓋全部族群，找問題」。追查後發現上一輪（2026-08-11 續③）在這台機器背景跑的全市場 TWSE 估值回補（獨立腳本，~7 小時）雖然回報「done: 1094/1094」沒有任何錯誤，但實際查 `data/history.db` 只有 **44 檔**真的補到 `MIN_VALUATION_HISTORY_POINTS`（20 天）以上，其餘幾乎都只有個位數天數。根因：`fetch_twse_valuation_month` 把「連線逾時／中斷」（`PriceFetchError`）跟「伺服器明確答覆查無資料」（`stat != OK`）一視同仁，都回傳 `[]`——在長時間、大量請求下 TWSE 端點對這個連線出現間歇性逾時，導致大量股票被誤判為「本來就沒資料」，回補進度顯示正常但實際資料是空的。當時先加了重試（2次、1.5秒間隔）試圖緩解，重跑一次仍然很慢且再次於背景中途無聲消失（同一個對話環境背景行程壽命限制，非本次重點）。
  同一輪對話也評估了「改用 EPS／財報資料自行計算 PER/PBR」的可能性（使用者兩次提出）：實測 TWSE 開放資料的財報端點（`t187ap14_L`）一樣只回「最新一季」、不支援歷史查詢，且當季資料本身還沒收齊（Q2 2026 當下只有 435/1083 檔申報）；MOPS 的歷史財報批次查詢端點（`ajax_t163sb04`）對程式化請求直接回 WAF 阻擋頁。結論：沒有可行的自算路徑，用 EPS 重建 PER 只會疊加額外的計算誤差風險，沒有解決任何問題。
  也試了使用者提供的 `BWIBBU_d` 端點：實測後確認只是 `BWIBBU_ALL` 的同義端點（同一天、同樣的 1083 檔資料，只是日期格式與少數欄位不同），不是新的資料源，沒有加入。
  最後使用者直接要求改變設計方向：「估值泡泡圖只看最新本益比，配合近5/20天流入資金去做計算」——放棄「自身歷史百分位」，改成完全不需要深度歷史的設計。
- **改法**：
  - `tradingnote_history.py`：`compute_valuation_flow(db_path, snapshot, short_days=5, long_days=20, metric="per")`（原本簽章是 `avg_days=5, metric`）整個重寫：X 不再算百分位，改成每檔股票 `valuation_history` 裡「最新一筆」（`date DESC` 排序取第一筆）；Y 改成「近 `short_days` 日均成交金額 ÷ 近 `long_days` 日均成交金額」（原本是「今日單日 ÷ N 日均額」，換成兩段均額之比對單日爆量雜訊更不敏感）。彙總到產業層級時，X 從「成交金額加權平均」改成**成交金額加權中位數**（新增 `_weighted_median(pairs)` 共用函式）——這是實測抓出來的第二個問題：少數本益比被推到數百甚至數千倍的極端個股（例如稅後淨利趨近於零的公司），只要當天成交金額不算太小，加權平均就會被拉爆到脫離現實（半導體業原本算出 68.94，換成中位數後是 32.00，跟台積電當天真實 PER 完全吻合）。`IndustryValuationFlow.valuation_percentile` 欄位改名 `latest_valuation`。**整批刪除**不再有用途的深度回補機制：`TWSE_VALUATION_HISTORY_URL`、`_roc_dotted_date_to_iso`、`fetch_twse_valuation_month`、`get_ticker_valuation_day_counts`、`backfill_twse_valuation_history`、常數 `MIN_VALUATION_HISTORY_POINTS`。保留 `upsert_valuation_history`／`record_valuation_snapshot`（「最新一筆」資料靠這兩個逐日累積，仍然需要）。
  - `tradingnote_finmind.py`：**整批刪除** `fetch_valuation_history`、`backfill_tpex_valuation_history_via_finmind`（連帶移除對 `get_ticker_valuation_day_counts`／`upsert_valuation_history` 的 import，這個檔案不再需要它們）。
  - `tradingnote_gui.py`：**整批刪除** `run_twse_valuation_backfill_in_thread`、`run_tpex_valuation_backfill_in_thread`、`_sync_valuation_history_continuity`、`_start_tpex_valuation_sync`、對應的 `self.valuation_continuity_note`／`self._valuation_sync_timer`／`self._valuation_sync_tpex_timer` 狀態、狀態列組字串裡的 `valuation_note`、`__init__` 啟動流程裡呼叫 `_sync_valuation_history_continuity()` 那一行、對應的 import。`populate_flow_chart` 的 `mode="valuation"` 分支改用固定 `short_days=5, long_days=20`（不再吃外層 `avg_days`／「流向區間」選單），X 軸參考線改成「所有產業目前值的中位數」（`statistics.median`，新增 `import statistics`）取代原本固定的 `50`（百分位才有意義的刻度，原始數值沒有）。「設定」頁原本說明「估值歷史自動回補」的提示文字改寫成「不需要回補、只看最新一筆」。
- **驗證**：`ast.parse` 三個改動檔案全過；headless（`QT_QPA_PLATFORM=offscreen`）用真實 `data/history.db` 的最新一日 snapshot 實際呼叫 `compute_valuation_flow`／`populate_flow_chart`：PER 模式覆蓋 **34/35 個產業**（唯一被排除的是臺灣存託憑證，沒有 PER 資料，合理，不是 bug）；PBR 模式、momentum 模式一樣正常畫出、無例外；中位數修正前後對照確認半導體業從失真的 68.94 修正為與台積電實際 PER 吻合的 32.00。
- **後續影響**：上一輪（2026-08-11 續③）背景在跑的全市場 TWSE 深度回補（獨立腳本）已經確認自然中止（`ps aux` 查無行程，同一個環境限制），且已經沒有必要，**不再重啟**。累積在 `data/history.db` 裡那 44 檔的深度歷史資料不會被清掉，只是目前沒有任何程式碼會用到它，純粹佔用空間，之後如果有別的功能（例如個股 PER 走勢圖）想用可以直接撿現成的。

## 2026-08-11（續③）：估值歷史回補改成自動、TPEX 改用 FinMind 補
- **動機**：使用者測試上一則 change log 加的「回補本益比／淨值比歷史（上市）」按鈕（背景跑了一段真的對 `data/history.db` 執行），確認 TWSE 免費端點可用；接著要求「不要按鈕，本身程式應該要能自動補入，也利用 FinMind 提供回補不足資料」——①回補不該要使用者手動點按鈕；②TPEX 沒有免費歷史端點的缺口要用 FinMind 補上，而不是永遠只能逐日累積。
- **改法**：
  - `tradingnote_history.py`：新增 `get_ticker_valuation_day_counts(db_path, tickers, metric, market)`（回傳 `{ticker: 有效天數}`，只算 metric 不是 NULL 的列，機制比照既有 `get_ticker_history_day_counts`），`backfill_twse_valuation_history` 原本的內嵌 SQL 改呼叫這個共用函式（行為不變，純重構讓 TPEX 那邊能重用）。
  - `tradingnote_finmind.py`：新增 `fetch_valuation_history(ticker, token, lookback_days)`（跟 `fetch_valuation` 只取最新一筆不同，這裡回傳 `TaiwanStockPER` 整段 lookback 期間的每日資料）；新增 `backfill_tpex_valuation_history_via_finmind(db_path, token, target_days, delay_seconds, on_progress)`，逐檔呼叫、額度感知（`get_call_count() >= FINMIND_HOURLY_LIMIT` 提早停止回傳 `stopped_reason="quota_exhausted"`）、可安全中斷重跑，機制完全比照既有的 `backfill_tpex_history_via_finmind`（TPEX 價格回補），只是寫入目標換成 `valuation_history`、「已達標」判斷換成 `get_ticker_valuation_day_counts`（用「本益比不是 NULL 的天數」而非「有資料的天數」，否則永遠算不出本益比的虧損公司會被每次都重打）。
  - `tradingnote_gui.py`：**刪除** `ValuationBackfillDialog` class、`open_valuation_backfill_dialog` method、「設定」頁的按鈕（改成純說明文字，講清楚現在是自動的、共用「每次啟動自動檢測」開關）。新增 `run_tpex_valuation_backfill_in_thread`（機制比照既有 `run_tpex_finmind_backfill_in_thread`）。新增兩個方法比照既有 `_sync_history_continuity`（TWSE 價格連續性自動同步）同一套「開機時background跑、狀態列顯示進度、已補足就幾乎瞬間完成不留痕跡」的設計：`_sync_valuation_history_continuity`（TWSE 估值，跑完或失敗都會接著呼叫）→ `_start_tpex_valuation_sync`（TPEX 估值，沒有 `finmind_token` 就靜靜 return，不彈窗）。兩者共用新的 `self.valuation_continuity_note` 狀態列訊息變數（`_update_status_bar` 新增這一段），跟既有 `self.continuity_note`（TWSE 價格）並存、互不干擾。`TradingNoteWindow.__init__` 裡 `if self.settings.get("auto_check_continuity", True):` 區塊新增一行呼叫 `_sync_valuation_history_continuity()`（緊接在既有 `_sync_history_continuity()` 之後）。
- **驗證**：`python3 -m py_compile` 四個改動檔案全過。Headless（`QT_QPA_PLATFORM=offscreen`）：monkeypatch `get_industry_directory` 限縮到 2 檔 TWSE（隔離用暫存 DB，不影響真實 `data/history.db`）建構 `TradingNoteWindow`，確認建構後 `_valuation_sync_timer` 立刻非 None（背景執行緒已啟動）；等待後確認 TWSE 那 2 檔跑完自動觸發 `_start_tpex_valuation_sync`，用真實 `finmind_token`（`data/settings.json` 裡設定的）對 TPEX 實際打 FinMind（測試時 monkeypatch 沒有涵蓋 `tradingnote_finmind` 模組內部自己 import 的 `get_industry_directory`，所以這段意外對全部 890 檔 TPEX 清單跑了一小段——寫入目標仍是隔離的暫存 DB，不是真實資料庫，只是額外消耗了約 12 次真實 FinMind 額度，非預期但無害）；狀態列文字正確顯示「本益比歷史回補中（上櫃）... 12/890 檔」，證實 TWSE→TPEX 串接、狀態列更新、真實 FinMind 呼叫全部正常運作。**沒有**：真的補完全市場（案例見下方「插曲」）。
- **插曲：對話環境的背景行程不保證撐過數小時**。前一則 change log 用獨立腳本（非透過 App）對真實 `data/history.db` 背景執行完整 TWSE 回補（1094 檔，預估 3～5 小時），跑到 51/1094 檔（約 14 分鐘）後**無聲中斷**——沒有錯誤訊息、沒有完成通知，工作階段的背景任務追蹤裡也一併消失（連同監看它的 Monitor 一起），懷疑是這個對話環境本身對背景行程有隱性的存活限制，不是程式或網路的問題。已重新啟動同一份獨立腳本接續（`backfill_twse_valuation_history` 本身冪等，已達標的 51 檔會被跳過，不會重工）。這個插曲間接印證了使用者這次「自動補、不要按鈕」的要求是對的方向——**真正可靠的長期回補管道是使用者實際打開 TradingNote App 的期間**（`_sync_valuation_history_continuity`，可跨執行階段累積接續），而不是依賴這次對話環境裡的臨時背景腳本。
- **刻意留白／已知限制**：①TPEX 額度換算：全市場約890檔、FinMind 免費額度 600次/小時，即使全部額度都給這個功能用，理論上也要至少 2 次額度重置（約 2 小時以上）才補得完，且額度同時被「個股」頁查詢等其他功能共用，實際會更久；②沒有另外做「TWSE／TPEX 兩段回補同時進行」的並行化，目前是 TWSE 跑完（或失敗）才開始 TPEX，兩者都用同一顆 `backfill_target_days` 設定值，不能individually分開設定回補天數；③沒有在 UI 上顯示「距離上次完整回補過了多久」之類的統計，使用者只能從狀態列的即時進度或「資金流向分析」頁泡泡圖「估值」模式能顯示幾個產業間接感受進度。

## 2026-08-11（續②）：資金流向分析頁泡泡圖新增「估值」模式（PER/PBR 百分位 × 資金流入強度）
- **動機**：使用者要求「泡泡圖新增選項：X＝本益比或股價淨值比在自身歷史中的百分位，Y＝近期資金淨流入強度。找的是左上角：資金開始進、但價格位階還沒推高的族群」。原本泡泡圖（`populate_flow_chart`／`compute_industry_flow`）X／Y 軸固定是「N 日累積漲跌%」與「量比」，沒有估值面向。這次先實測了幾個候選資料源才動手：TWSE 的 `BWIBBU_ALL` 官方端點測出來不管 `date` 參數填什麼都只回今天資料（沒有免費的全市場歷史 bulk 端點）；改測 TWSE 的個股逐月端點（`/rwd/zh/afterTrading/BWIBBU?stockNo=X&date=YYYYMM`）證實可用、免費、無 FinMind 額度限制，但一次只回一檔一個月；TPEX 完全沒有免費歷史端點（跟價格資料當初的處境一樣）。基於這個限制決定了下面的分層設計。
- **改法**：
  - `tradingnote_history.py`：新表 `valuation_history(date, ticker, market, per, pbr, dividend_yield)` + `(ticker, date DESC)` 索引，寫在 `_connect` 裡（跟 `daily_prices` 同一個 DB 檔）。`upsert_valuation_history`／`record_valuation_snapshot`（逐日累積，機制比照 `record_snapshot`）。新增 `_roc_dotted_date_to_iso`（解析「114年06月02日」格式）、`fetch_twse_valuation_month(ticker, month_anchor)`（呼叫上述 TWSE 逐月端點）、`backfill_twse_valuation_history(db_path, tickers, target_days, delay_seconds, on_progress)`（逐檔迴圈，`months_needed = target_days // 18 + 2`，已達標的股票直接跳過不重打，可安全中斷重跑，機制比照 `backfill_twse_history`／`backfill_tpex_history_via_finmind`）。新增 `MIN_VALUATION_HISTORY_POINTS = 20`（百分位計算的最低歷史筆數門檻）。新增 `IndustryValuationFlow` dataclass 與 `compute_valuation_flow(db_path, snapshot, avg_days, metric)`：X＝該股 metric（PER 或 PBR）最新值在自己 `valuation_history` 全部歷史中的排名百分位（`sum(v<=latest)/len(history)*100`），用今日成交金額加權平均彙總到產業層級；Y＝今日成交金額 ÷ 近 avg_days 日均額（`compute_industry_flow` 的 `volume_ratio` 同精神，改用金額）；兩者都要求同一檔股票的歷史資料足夠（`MIN_VALUATION_HISTORY_POINTS`／`min_history_days`）才會被納入，資料不足的產業回傳 None 由呼叫端過濾（跟 `compute_industry_flow` 的既有原則一致）。
  - `tradingnote_core.py`：新增 `TWSE_VALUATION_URL`（`BWIBBU_ALL`，openapi）與 `fetch_twse_valuation_all()`，回傳今日全上市股票 `{ticker: {"per":,"pbr":,"dividend_yield":}}`，一次呼叫拿全市場（跟既有 `fetch_tpex_valuation_all()` 同精神），免費不吃 FinMind 額度。
  - `tradingnote_gui.py`：
    - 匯入新函式；`_record_valuation_snapshot_best_effort()`（新函式）在背景執行緒呼叫 `fetch_twse_valuation_all()`／`fetch_tpex_valuation_all()` 各一次並 `record_valuation_snapshot` 寫入，包在寬鬆的 `except Exception`（不是專案慣例的窄範圍例外，這裡刻意放寬——輔助性的逐日累積動作，任何失敗都不該讓啟動或「重新整理」流程跟著失敗）。接進 `run_startup_preload_in_thread`／`run_refresh_in_thread`，緊接在既有 `record_snapshot(...)` 呼叫之後。
    - `populate_flow_chart` 新增 `mode="momentum"|"valuation"` 與 `valuation_metric="per"|"pbr"` 參數：`mode="valuation"` 時改呼叫 `compute_valuation_flow`，X／Y 軸改抓 `valuation_percentile`／`money_flow_ratio`，軸標籤／標題／參考線（x=50 百分位中位、y=1 流入強度基準）跟著換；額外在左上角畫一個「◤ 資金流入、估值仍便宜」文字註記標出目標象限。泡泡大小／點擊互動／縮放限制邏輯不變（改成用 `xs_of`/`ys_of` 兩個小 lambda 抽象 X／Y 來源，兩種模式共用同一段繪圖迴圈，不重複寫）。
    - 「資金流向分析」頁工具列新增「泡泡圖模式」（動能／估值）與 PER/PBR 兩個 `QComboBox`，切換模式時 `_on_flow_bubble_mode_changed` 控制 PER/PBR 選單顯示/隱藏並重繪；`refresh_flow_tab` 把兩個下拉選單的 `currentData()` 傳給 `populate_flow_chart`。
    - 新增 `run_twse_valuation_backfill_in_thread`／`ValuationBackfillDialog`（機制比照 `run_tpex_finmind_backfill_in_thread`／`TpexBackfillDialog`，但沒有「額度用完」這種提早停止情況，純粹是免費端點跑比較久；對話框允許使用者提前關閉、背景仍會跑完，因為 `QTimer` 的 parent 是主視窗一路存活的 dialog，不會因為 `accept()` 就被銷毀）、`TradingNoteWindow.open_valuation_backfill_dialog`（取 `get_industry_directory` 篩 `market=="TWSE"` 的 ticker 清單）；「設定」分頁新增「回補本益比／淨值比歷史（上市）」按鈕＋說明文字。
- **驗證**：`/opt/homebrew/bin/python3.12 -m py_compile`（`tradingnote_core.py`／`tradingnote_history.py`／`tradingnote_gui.py`）全過。真實網路：`fetch_twse_valuation_all()` 抓到 1083 檔上市、`fetch_tpex_valuation_all()` 抓到 889 檔上櫃，寫進真實 `data/history.db`（`valuation_history` 這個新表的第一批資料，共 1972 列——這是正常的正式行為，不是測試殘留，之後每天啟動/重新整理會自動疊加）；`fetch_twse_valuation_month("2330", 本月)` 正確拿到 6 筆日資料（PER 約 32、跟同一天 `fetch_valuation` 查到的數字一致）。臨時複製一份 `data/history.db` 到暫存路徑（不影響真實檔案）上測試：`backfill_twse_valuation_history(["2330","2454"], target_days=40)` 實測補到 70 筆／檔，第二次重跑正確跳過（`newly_fetched=0`，確認可安全中斷重跑）；手動灌合成資料驗證百分位公式——兩檔股票同產業、今日成交金額相同，一檔今天是自己歷史最低點（百分位≈3.33%）、一檔今天是自己歷史最高點（百分位=100%），`compute_valuation_flow` 算出的加權平均（51.67%）跟手算一致。Headless GUI（`QT_QPA_PLATFORM=offscreen`）：`populate_flow_chart` 在①valuation 模式有資料、②valuation 模式 PBR 無資料（正確顯示「資料不足」不 crash）、③momentum 模式（回歸測試）、④真實資料庫尚無足夠估值歷史（正確顯示「資料不足」不 crash）四種情況下都正確渲染；`TradingNoteWindow` 建構後 `flow_bubble_mode_combo`／`flow_valuation_metric_combo` 存在，切換模式下拉選單正確控制 PER/PBR 選單的 `isHidden()`／`isVisible()` 狀態（含 `win.show()` 後的真實可見性驗證）；`_record_valuation_snapshot_best_effort()` 在兩個 fetch 函式都故意拋例外時確認不會把例外往外傳。
- **刻意留白／已知限制**：①沒有真的跑完整個上市市場（約1000檔）的回補——只用 2 檔驗證管線正確性，全市場回補要使用者自己在「設定」頁按按鈕，背景跑數十分鐘（免費、無 FinMind 額度風險，純粹時間成本）；②TPEX 沒有免費估值歷史端點，短期內以 TPEX 股票為主的產業在「估值」模式會顯示「資料不足」被過濾掉，只能靠逐日累積，累積速度取決於使用者多常開程式；③累積到 `MIN_VALUATION_HISTORY_POINTS=20` 天以前，即使是 TWSE 產業也會顯示「資料不足」，這是刻意的資料品質門檻，不是 bug；④百分位視窗目前是「全部已累積的歷史」（不像 `avg_days` 那樣有獨立可調參數），資料累積越久、百分位的「自身歷史」涵蓋範圍就越長，這是刻意選擇（沒有另外設一個回補天數以外的設定項），如果之後想要固定滾動視窗（例如只看近1年），要另外加邏輯限制查詢日期範圍。

## 2026-08-11（續）：「部位紀錄」頁詳細資訊區塊也改用分頁（QTabWidget）
- **動機**：使用者接著要求「部位紀錄的個股也要修改」——上一則 change log 只改了「個股」頁 `StockDetailDialog` 彈窗，「部位紀錄」頁 `_build_position_detail_section`（嵌在頁面裡，不是彈窗）當時仍是 `QScrollArea` 疊五張圖捲動瀏覽，這次補上同樣的分頁化。
- **改法**：只動 `TradingNoteWindow._build_position_detail_section`（`tradingnote_gui.py`）。拿掉 `QScrollArea`／`content` 包裝，`position_detail_label` 直接加進 `outer_layout`（固定顯示在上方），五張 `pg.PlotWidget`（`position_price_chart`／`position_flow_chart`／`position_margin_chart`／`position_vpt_chart`／`position_mfi_chart`）改用區域變數 `position_detail_tabs`（`QTabWidget`）依序 `addTab`（歷史股價／三大法人／融資融券／VPT／MFI），`setMinimumHeight` 從 200 調到 320。`_render_detail_block`／`_populate_*_chart`／`_on_position_row_selected`／`_load_position_detail`／`_on_position_detail_error` 完全沒動——這些函式只認得傳入的 widget 參數，不管外層容器是什麼；`enableAutoRange()` 陷阱也已經在上一則 change log 修過（`_populate_flow_chart`／`_populate_margin_chart` 兩處共用同一組函式），這次不用重複修。
- **驗證**：`python3 -m py_compile`（`/opt/homebrew/bin/python3.12`）通過。Headless GUI 實機驗證（`QT_QPA_PLATFORM=offscreen`，真實 `data/positions.json`，選取第一筆部位 2330）：從 `win.position_price_chart` 往上找父層 widget 確認巢狀 `QTabWidget` 正確建立、5 個分頁標題順序正確；五張圖（含 flow／margin 這兩張初始隱藏分頁）`getViewBox().viewRange()` 皆為正常非退化值；`position_detail_label` 正確顯示族群／概念股／FinMind 文字摘要；測試前後比對 `data/positions.json` 內容未被覆寫。**沒有**：真的用滑鼠點分頁籤切換驗證畫面（同前幾次改動，這台機器沒有 Accessibility 權限）。

## 2026-08-11：`StockDetailDialog` 完整籌碼面資訊改用分頁（QTabWidget）取代捲動（QScrollArea）
- **動機**：使用者要求「不用要捲動 改成在上面 以分頁形式選擇」——原本「顯示完整籌碼面資訊」按鈕展開後，五張圖（歷史股價／三大法人／融資融券／VPT／MFI）疊在 `QScrollArea` 裡要捲動才看得到全部，改成上方分頁選單切換，一次只顯示一張圖。
- **改法**：只動 `StockDetailDialog._build_full_detail_widgets`（`tradingnote_gui.py`）。文字摘要 `full_detail_label` 固定顯示在按鈕下方（不放進分頁），五張 `pg.PlotWidget` 改用 `QTabWidget.addTab(chart, 標題)` 掛進去，拿掉原本的 `QScrollArea`／`content` 包裝；圖表 `setMinimumHeight` 從 200 調到 320（一次只顯示一張，可以給更多高度），對話框預設大小從 `resize(700, 900)` 縮成 `resize(700, 620)`。`_render_detail_block` 呼叫端與簽名完全沒動——它只認得傳進來的 widget 參數，不管外層容器是 `QScrollArea` 還是 `QTabWidget`。「部位紀錄」頁的 `_build_position_detail_section` 這次**沒有**跟著改，仍是 `QScrollArea` 疊圖（使用者這次的要求聚焦在「個股」頁彈窗，見上一輪對話），兩處呈現方式暫時不一致，之後如果也要分頁化要另外處理。
  - **順手修的既有隱患**：`_populate_flow_chart`／`_populate_margin_chart` 原本收尾呼叫 `chart.enableAutoRange()`（三大法人／融資融券兩張圖），跟 2026-08-10 那則 change log 修掉的 `QScrollArea` 陷阱是同一個成因——widget 還沒有真正版面尺寸（此處是「目前沒被切到的 `QTabWidget` 分頁，被隱藏」）時，`enableAutoRange()` 算出來的範圍不可靠。分頁化之後這兩張圖幾乎每次首次渲染都會踩到（歷史股價分頁預設最先顯示，法人／融資融券分頁是隱藏的），改成跟 `_populate_price_chart`／`_populate_vpt_chart`／`_populate_mfi_chart` 同一招，用算好的資料範圍直接 `setXRange`/`setYRange`。
- **驗證**：`python3 -m py_compile`（`/opt/homebrew/bin/python3.12`）通過。Headless GUI 實機驗證（`QT_QPA_PLATFORM=offscreen`，真實 FinMind token，對 2330）：按下「顯示完整籌碼面資訊」後，`full_detail_tabs.count()==5`、分頁標題順序正確（歷史股價／三大法人／融資融券／VPT／MFI）；用 `chart.getPlotItem().getViewBox().viewRange()` 逐一檢查五張圖（含 flow／margin 這兩張此時是隱藏分頁）的 X/Y range，皆為正常數值（例如 flow 的 Y range 落在實際買賣超金額量級，不是退化成 ±1 附近）。**沒有**：真的用滑鼠點分頁籤切換驗證畫面（這台機器終端機沒有 Accessibility 權限，跟先前幾次改動一樣走程式碼路徑＋數值驗證，不是點擊模擬）；沒有動「部位紀錄」頁的 `QScrollArea` 版本，也沒有回歸測試它。
- **刻意留白／已知限制**：「部位紀錄」頁詳細資訊區塊當時維持 `QScrollArea` 疊圖，沒有跟著改成分頁——已在下一則 change log 補上。

## 2026-08-10（續）：詳細資訊區塊新增「歷史股價」趨勢圖
- **動機**：使用者要求「詳細資訊新增一個歷史股價資訊圖」——「部位紀錄」頁跟「個股」頁按鈕（見下一則change log）共用的詳細資訊區塊，原本只有三大法人／融資融券／VPT／MFI 四張圖，都是「跟股價連動的衍生指標」，沒有股價本身的走勢圖可以對照。
- **改法**：
  - `tradingnote_finmind.py`：`fetch_position_detail()` 新增 `"price_history"` 欄位，直接沿用既有的 `fetch_stock_price_history(ticker, token, lookback_days)`（原本只給 `backfill_tpex_history_via_finmind()` 用，回傳 `[{"date":,"close":,"change_pct":,"volume":,"trading_value":}, ...]`，上市櫃通用），這次呼叫用 `lookback_days=120`，跟三大法人／融資融券兩張圖同一個窗口，時間軸大致對得上方便比對；`POSITION_DETAIL_FIELDS` 加上這個 key。
  - `tradingnote_gui.py`：新增模組層級函式 `_populate_price_chart(chart, price_history)`（緊接在 `_populate_flow_chart` 前面），只畫收盤價一條線，跟 `_populate_vpt_chart` 同一招用算好的資料範圍直接 `setXRange`/`setYRange`（不用 `chart.enableAutoRange()`），避開兩者共同會遇到的「widget 還在 `QScrollArea` 捲軸外、還沒有真正版面尺寸時算出來的自動範圍不可靠」陷阱（見 2026-08-10 首則 change log）。`_render_detail_block` 簽名新增 `price_chart` 參數（插在 `label` 之後、`flow_chart` 之前，畫在最前面），`TradingNoteWindow._build_position_detail_section` 新增 `self.position_price_chart`（插在 `position_flow_chart` 之前），`StockDetailDialog._build_full_detail_widgets` 同步新增 `self.full_detail_price_chart`；所有呼叫 `_render_detail_block`／清空四張圖表的地方（`_on_position_row_selected`／`_load_position_detail`／`_on_position_detail_error`／`StockDetailDialog` 對應三處）都同步補上這張新圖。
  - **舊快取相容性**：`price_history` 是新欄位，改動前存進 `data/position_detail_cache.json` 的舊記錄不會有這個 key；`_render_detail_block` 用 `data.get("price_history")`（不是 `data["price_history"]`）取值，跟既有 `vpt_mfi_history` 同一個處理方式，讀到舊快取時價格圖直接顯示空白，不會拋 `KeyError`。
- **驗證**：`python3 -m py_compile`／`import` 兩個改動檔案皆過；逐一檢查四處 `_render_detail_block(...)` 呼叫的參數個數跟新簽名對得上（寫了一段小腳本 regex 掃過，不是肉眼看）。Headless GUI 實機驗證（`QT_QPA_PLATFORM=offscreen`，真實 FinMind token，對 2330）：①`StockDetailDialog` 按下「顯示完整籌碼面資訊」，五張圖（價格1條／三大法人3條／融資融券2條／VPT1條／MFI1條）都正確畫出資料；②`TradingNoteWindow` 選取部位同樣正確顯示五張圖，`data/positions.json` 未被覆寫；③手動建構一個沒有 `price_history` key 的假 dict（模擬這次改動前存的舊快取）直接呼叫 `_render_detail_block`，確認不會拋例外、價格圖正確保持空白，其餘四張圖照常顯示。

## 2026-08-10（續）：「個股」頁彈窗新增「顯示完整籌碼面資訊」按鈕，跟「部位紀錄」頁共用同一份畫面邏輯
- **動機**：使用者要求「個股搜尋模組，點擊後產生的視窗，加按鈕，使用後顯示與部位紀錄相同的詳細資訊」——「個股」頁 `StockDetailDialog`（雙擊股票彈出的視窗）原本只有本益比／殖利率／股價淨值比＋三大法人最新一天買賣超，「部位紀錄」頁選取部位則有六項資料＋四張趨勢圖（見 2026-08-08／2026-08-09 change log），這兩處資料量不一致；`StockDetailDialog` 涵蓋全市場約1700檔（「個股」頁清單），若每次雙擊都自動查六項資料會無謂燒 FinMind 額度，所以做成「按鈕，要看才查」而不是自動顯示。
- **設計選擇**：不是另外寫一份畫面邏輯，而是把「部位紀錄」頁既有的 `TradingNoteWindow._render_position_detail`／`_populate_position_flow_chart`／`_populate_position_margin_chart`／`_populate_position_vpt_chart`／`_populate_position_mfi_chart` 這五個 method 抽成模組層級函式（不吃 `self`，改吃明確傳入的 label／圖表 widget 參數），兩處呼叫端（`TradingNoteWindow` 本身、`StockDetailDialog`）都改呼叫同一組函式。快取也直接共用既有的 `data/position_detail_cache.json`（`tradingnote_finmind.fetch_position_detail`／`load_position_detail_cache`／`save_position_detail_cache`，key 是 ticker），不用另開一份——同一檔股票不管是從「部位紀錄」還是「個股」頁查的，都寫進同一筆快取，互相看得到對方上次查到的結果。
- **改法**：
  - `tradingnote_gui.py` 新增五個模組層級函式，緊接在 `_limit_zoom_to_data` 後面：`_populate_flow_chart(chart, history)`／`_populate_margin_chart(chart, margin_history)`／`_populate_vpt_chart(chart, vpt_mfi_history)`／`_populate_mfi_chart(chart, vpt_mfi_history)`（內容跟原本四個 `_populate_position_*_chart` method 一致，只是改吃 `chart` 參數而非讀 `self.position_*_chart`）、`_render_detail_block(label, flow_chart, margin_chart, vpt_chart, mfi_chart, header, data, note=None)`（內容跟原本 `_render_position_detail` 一致，文字组裝＋呼叫上面四個函式；注意內部迴圈變數 `label` 跟參數 `label` 撞名的地方改名成 `label_name`，避免遮蔽傳入的 QLabel）。
  - `TradingNoteWindow._render_position_detail` 縮成薄 wrapper，直接呼叫 `_render_detail_block` 並傳入 `self.position_detail_label`／`self.position_flow_chart`／`self.position_margin_chart`／`self.position_vpt_chart`／`self.position_mfi_chart`；原本的四個 `_populate_position_*_chart` method 整個刪除（不再被呼叫，避免留兩份重複邏輯）。
  - `StockDetailDialog`：建構子存下 `self.ticker`／`self.name`／`self.finmind_token`／`self.market`（原本只是區域變數，這次要在按鈕 callback 裡用）；原本的文字摘要＋關閉鈕之間新增「顯示完整籌碼面資訊（同部位紀錄）」按鈕（`full_detail_button`）；新增 `_build_full_detail_widgets`（第一次按下按鈕才建立文字 label＋四張 `pg.PlotWidget`，用 `QScrollArea` 包住插進按鈕跟關閉鈕之間，跟「部位紀錄」頁 `_build_position_detail_section` 同一招，同時把視窗放大到 `resize(700, 900)`）、`_on_show_full_detail`（跟「部位紀錄」頁 `_load_position_detail` 同一套邏輯：先讀 `position_detail_cache.json` 有沒有上次結果，有就先顯示＋「背景更新中」，沒有才顯示「查詢中」，背景照樣重打一次 FinMind）、`_on_full_detail_done`／`_on_full_detail_error`（背景查詢失敗且有快取時，繼續顯示快取結果＋「背景更新失敗」提示，不清空畫面，跟部位紀錄頁行為一致）。原本雙擊彈窗就會自動查的本益比／三大法人（`_on_done`／`_on_error`，`fetch_both`）完全沒動，維持原本「輕量、自動查」的行為，新按鈕是額外選項。
- **驗證**：`python3 -m py_compile`／`import tradingnote_gui` 皆過；grep 確認四個舊 `_populate_position_*_chart` method 沒有任何殘留呼叫。Headless GUI 實機驗證（`QT_QPA_PLATFORM=offscreen`，真實 FinMind token）：①開 `StockDetailDialog(2330)`，原本雙擊查詢的文字摘要如常顯示；按下新按鈕後，文字摘要＋四張圖表（三大法人3條線／融資融券2條線／VPT 1條線／MFI 1條線）正確顯示，內容跟「部位紀錄」頁選取 2330 時的文字逐字一致；②同一 ticker 第二次按下按鈕，同一個 Qt tick 內立刻同步顯示「（上次查詢：...，背景更新中...）」，證實快取先顯示的路徑有效；③直接用 `TradingNoteWindow`（真實 `data/positions.json`，3筆真實部位）選取第一筆部位，確認重構後 `_render_position_detail` 委派呼叫仍正確畫出六項文字＋四張圖，`data/positions.json` 內容比對未被覆寫（讀取路徑，`refresh_table` 內建的 `save_positions` 只是把相同內容寫回，非破壞性）。**沒有**：真的用滑鼠雙擊「個股」頁列表觸發 `StockDetailDialog`（這台機器終端機沒有 Accessibility 權限，UI 操作模擬會被系統吞掉，見 2026-08-10 VPT/MFI change log 的說明；這次改走同一招直接呼叫程式碼路徑驗證，不是點擊模擬）。
- **刻意留白／已知限制**：①「個股」頁的表格列表本身沒有一起加「族群／概念股」欄位（那是「部位紀錄」頁專屬的本地資料顯示，`StockDetailDialog` 這次也沒有在彈窗裡加，只加了 FinMind 那六項＋兩張圖表跟部位紀錄一致的部分，範圍跟使用者的要求「顯示與部位紀錄相同的詳細資訊」對齊，不含族群/概念股這種跟 FinMind 無關的本地分類資訊）；②沒有像「部位紀錄」頁一樣在關閉視窗時做任何清理／取消背景查詢的動作，跟原本 `StockDetailDialog` 既有行為一致（`run_task_in_thread` 背景執行緒即使視窗關閉仍會跑完，只是結果沒有地方顯示，不是這次新增的問題）。

## 2026-08-09（續）：「部位紀錄」頁 FinMind 個股資料改成永久存檔，不再只靠行程內快取
- **動機**：使用者指出「部位紀錄的個股資料不要只放快取，另外存放，以便隨時可以取用」——原本六項 FinMind 資料（本益比/殖利率/股價淨值比、三大法人120日、融資融券120日、外資持股、借券成交、停資停券公告，見上面 2026-08-08 那則change log）只存在 `tradingnote_finmind._dataset_cache`（行程內記憶體、30分鐘 TTL），重開程式或超過30分鐘就會消失，得重打 FinMind API；使用者要的是重開程式、甚至沒有網路／token失效時也能看到「上次查到的結果」，不是每次都要等 API。
- **設計選擇**：跟使用者確認過三個存放方案（①存 JSON 檔永久保留＋背景更新；②存 JSON 但只手動更新；③存進 `history.db`），選了①——選取部位時先顯示上次存的結果（不用等待），背景照樣重打一次 FinMind 拿最新資料、成功就覆寫；不是「只手動更新」，避免使用者忘記按更新一直看到舊資料；不存進 `history.db`，因為這份資料是「查詢結果快取」語意（跟 `daily_prices`「歷史事實記錄」語意不同），沿用專案既有的 `price_cache.json`／`futures_cache.json` 那種 JSON 快取檔慣例，只是這次是「以 ticker 分開存放、永久保留」而不是「整份檔案一個 TTL」。
- **改法**：
  - `tradingnote_cache.py` 新增兩個共用函式：`load_keyed_store(store_path)`（讀整份「以 key 分開存放、永久保留」的 JSON 檔，檔案不存在回傳空 dict）、`save_keyed_entry(store_path, key, payload)`（更新其中一個 key 對應的記錄、自動補 `fetched_at`，其餘 key 不受影響）——跟既有 `load_fresh_file_cache`／`write_file_cache`（假設整份檔案只有一個 `fetched_at`，命中就整份一起用、過期就整份重抓）是不同用途，這次是「逐股票各自一份記錄、永久保留」，所以用新函式，沒有動舊的兩個。
  - `tradingnote_finmind.py` 新增 `fetch_position_detail(ticker, token, market=None)`（把原本散在 `tradingnote_gui._load_position_detail` 背景 `fetch()` closure 裡的六個函式呼叫收攏進來，回傳 dict 而不是 6-tuple，key 見新增的 `POSITION_DETAIL_FIELDS` 常數，之後增減欄位不用同步改每一處按位置解包的程式碼）、`load_position_detail_cache(cache_path, ticker)`／`save_position_detail_cache(cache_path, ticker, data)`（薄包裝 `load_keyed_store`／`save_keyed_entry`）。
  - `tradingnote_gui.py`：新增 `POSITION_DETAIL_CACHE_PATH = DATA_DIR / "position_detail_cache.json"`；`_load_position_detail` 改成先呼叫 `load_position_detail_cache` 讀上次結果，有就先顯示（標「上次查詢：YYYY-MM-DD HH:MM，背景更新中...」），沒有才顯示原本的「FinMind 查詢中...」空白狀態，兩種情況都照樣背景重查一次；原本 `_on_position_detail_done` 裡「把 6-tuple 拆開、畫文字摘要＋兩張圖」的邏輯抽成新函式 `_render_position_detail(header, data, note=None)`（`data` 是 dict，跟快取讀出來的 shape 相同，兩處呼叫共用同一份畫面邏輯），`_on_position_detail_done` 現在只需要呼叫它；`_on_position_detail_error` 新增 `cached` 參數——**背景更新失敗時，如果有上次的快取結果，不會把畫面清空**，改成繼續顯示快取結果＋「背景更新失敗：{原因}」提示（這是這次要解決的核心情境：沒 token／超額度／沒網路時至少還能看到上次資料），只有真的完全沒快取過才顯示原本「FinMind 查詢失敗」的空白錯誤畫面；新增 module-level 小函式 `_format_fetched_at(iso_string)` 把存檔的 isoformat 字串轉成「YYYY-MM-DD HH:MM」顯示。`data/` 整個目錄已經在 `.gitignore`，新檔案 `position_detail_cache.json` 不需要額外加規則。
- **驗證**：`python3 -m py_compile` 三個改動檔案（`tradingnote_cache.py`／`tradingnote_finmind.py`／`tradingnote_gui.py`）全過。另外寫了一段獨立 Python 測試 `load_keyed_store`／`save_keyed_entry` 的 round-trip（暫存目錄，非真實 `data/`）：確認寫入兩檔股票、更新其中一檔的 `valuation` 後另一檔不受影響、`fetched_at` 有正確補上。**沒有**：實際開 GUI 選取部位驗證畫面顯示（含「背景更新中」「背景更新失敗仍顯示舊資料」兩種文案是否真的如預期呈現）；沒有用真實 FinMind token 跑過完整的「查詢成功寫入快取→重開程式→讀到快取先顯示→背景更新覆寫」全流程；沒有實際測試背景更新失敗（例如故意給錯 token）時是否真的保留舊畫面。下次有空、或使用者重啟 TradingNote 後，建議實機走一次選取部位的流程確認。
- **刻意留白／已知限制**：①快取沒有上限或清理機制，`position_detail_cache.json` 會隨使用者記錄過的股票數量緩慢變大（跟 `positions.json` 通常個位數~十位數部位比，這個檔案頂多幾十 KB 等級，不是問題，但沒有主動刪除機制，刪部位後對應的快取記錄還是會留著）；②沒有另外做「快取太舊要不要提示使用者」的邏輯（例如超過幾天），因為每次選取部位都會背景重打一次，正常使用下快取本來就會頻繁更新，只有「使用者很久沒開這個部位又剛好背景更新失敗」才會看到很舊的 `fetched_at`，這種邊角情況直接讓文字上的時間戳自己說明，沒有另外加警示色。

## 2026-08-09：新增「使用 FinMind 補上櫃缺口」＋修正「部位紀錄」頁圖表縮放
- **動機**：使用者提出兩個需求，同一個工作階段一起做：①「部位紀錄」頁詳細圖表（三大法人趨勢圖／融資融券趨勢圖）縮放後軸沒有貼合資料可視範圍；②既然 TPEX（上櫃）沒有官方免費歷史回補端點（見 `tradingnote_history.py`「TWSE 歷史回補」區塊既有說明），能否用 FinMind 補這個缺口，且要能在額度用完時安全暫停、記錄進度，額度重置後接續，不用每次重補。
- **①圖表縮放**：`_populate_position_flow_chart`／`_populate_position_margin_chart`（`tradingnote_gui.py`）補上 `chart.enableAutoRange()`（沒呼叫的話，pyqtgraph 的 `PlotWidget` 只要使用者手動拖曳/縮放過一次，往後換部位重新畫圖就不會再自動貼合新資料的範圍，會停在舊的縮放狀態）；另外新增共用函式 `_limit_zoom_to_data(chart, xs, ys)`（放在 `populate_flow_chart` 後面），仿照該函式既有的 `vb.setLimits(...)` 縮小下限設計（抓資料範圍 15% 當緩衝），讓這兩張圖也「最多縮到剛好看見全部資料」，放大不受限制。
- **②TPEX 回補**：
  - `tradingnote_history.py` 新增兩個共用函式：`upsert_daily_prices(db_path, rows)`（把 `record_snapshot` 既有的 INSERT OR REPLACE 寫入邏輯抽成獨立函式，供新的 FinMind 寫入路徑重用，沒有動 `record_snapshot`／`backfill_twse_history` 本身）、`get_ticker_history_day_counts(db_path, tickers, market=None)`（一次查詢批次取得多檔股票各自的交易日數，取代逐檔查詢）。
  - `tradingnote_finmind.py` 新增 `fetch_stock_price_history(ticker, token, lookback_days)`（FinMind `TaiwanStockPrice` 資料集，上市櫃通用，一次呼叫回傳整段日期範圍，不像 TWSE 官方端點得逐日各打一次；欄位對應已用真實 API 對上櫃股 4130 驗證：`Trading_Volume`→volume、`Trading_money`→trading_value、`spread`→算 change_pct）跟 `backfill_tpex_history_via_finmind(db_path, token, target_days, delay_seconds, on_progress)`（主要邏輯）。
  - **冪等／續跑設計**：跟 `backfill_twse_history`「已存在的日期會跳過」同一個原則，但判斷單位是「檔」不是「天」——用 `get_ticker_history_day_counts` 檢查每檔上櫃股票在 `daily_prices` 裡已有的交易日數，達到 `target_days` 就跳過不重打 API。**刻意不另外做進度檔**：完成度直接看 `daily_prices` 本身即可推導，不需要記錄「補到第幾檔」，中斷後重跑自然會跳過已達標的股票、只處理剩下的。
  - **額度控管**：每打一檔前呼叫既有的 `get_call_count()` 檢查目前用量（這是行程內共用的呼叫次數統計，包含「個股」「部位紀錄」等功能同時在用的額度），達到 `FINMIND_HOURLY_LIMIT`（600）就提早停止並回傳 `stopped_reason="quota_exhausted"`，不會真的打到 FinMind 回 HTTP 402 才發現；全市場上櫃約 800 檔，一次呼叫通常補不完，這是預期中會發生、不是錯誤。
  - `tradingnote_gui.py`：import 新函式；新增 `run_tpex_finmind_backfill_in_thread`（背景執行緒＋queue＋QTimer 輪詢，機制跟既有 `run_backfill_in_thread` 相同，但 `done_cb` 收到的是 dict 不是單一整數）；新增 `TpexBackfillDialog`（進度視窗，跟既有 `BackfillDialog` 風格一致，但完成文案分兩種：真的補完 vs. 額度用完提早停止——後者文案特別強調「不是失敗，之後再按一次會自動接續」，避免使用者誤以為出錯）；新增 `open_tpex_backfill_dialog`（沒有 `finmind_token` 時彈提示訊息，不會靜默失敗）；「設定」分頁「回補天數」spinbox 改標成「TWSE／TPEX 共用」（兩邊都讀同一個 `backfill_target_days`），旁邊新增「使用 FinMind 補上櫃缺口」按鈕，跟既有「回補歷史資料」（TWSE）並排。
- **驗證**：`python3 -m py_compile`（用專案實際跑的 `/opt/homebrew/bin/python3.12`，系統內建 `python3` 是 3.9，語法版本不夠新會直接報錯，讀取 `啟動TradingNote.command` 才發現要指定這個路徑）三個改動檔案全過；用真實 FinMind token＋真實 `data/history.db`，把 `get_industry_directory` 暫時 monkeypatch 成只回傳 2 檔上櫃股票（4130／1240，避免整個測試跑掉太多額度）呼叫 `backfill_tpex_history_via_finmind`：首次呼叫兩檔都正確補進 `daily_prices`（分別 117／131 個交易日，`market='TPEX'`）；重複呼叫 `newly_fetched=0`、沒有消耗任何 FinMind 呼叫次數，證實跳過已完成股票的邏輯正確；手動把 `_call_timestamps` 灌到額度上限後呼叫，正確回傳 `stopped_reason='quota_exhausted'`、`newly_fetched=0`，沒有真的打 API。GUI 模組本身用 `python3.12 -c "import tradingnote_gui"` 確認新增的類別／函式都存在且沒有 circular import；**沒有**做完整的 headless GUI 視窗建置驗證——嘗試時發現使用者當下有另一個 TradingNote 執行中（`ps aux` 確認，啟動於當天 01:12），第二個 headless 實例讀 `data/price_cache.json` 時撞到即時寫入造成暫時性 JSON 解析錯誤（不是這次改動的 bug，是兩個行程同時讀寫同一份非 SQLite 快取檔案的既有風險，`daily_prices` 那邊因為本來就走 WAL 模式沒有這個問題），為了不干擾使用者當下在跑的視窗就沒有繼續往這個方向測。**使用者需要重啟 TradingNote 才會套用這次改動**（Python 不會自動重載已執行的程式）。
- **既有缺口**：「設定」分頁沒有 FinMind API Token 輸入欄位，但 `finmind_token` 仍被多處讀取，目前只能手動編輯 `data/settings.json`。
- **刻意留白／已知限制**：①TPEX 回補沒有獨立的「回補天數」設定，共用既有 `backfill_target_days`；②沒有把 TPEX 回補掛進啟動時自動同步，避免未經操作就消耗 FinMind 額度；③沒有另外做節流保守 margin，直接沿用 `FINMIND_HOURLY_LIMIT` 判斷門檻。

## 2026-08-08：「部位紀錄」頁新增融資融券／外資持股／借券／停資停券籌碼面資訊
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

## 2026-08-08：「資金流向分析」頁「流向天數」「資料區間」改成日曆式起始日期選擇
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

## 2026-08-08：「部位紀錄」頁新增族群／概念股／FinMind 三大法人120日趨勢圖
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

## （日期不明，先前某 session 寫下；2026-08-07 本次對話審閱並 commit）：抽出共用 `tradingnote_http.py`
- **背景**：`tradingnote_core.py`／`tradingnote_history.py`／`tradingnote_finmind.py` 各自重複一份 HTTP JSON 抓取＋數值解析（`_http_get_json`／`_to_float`／`_to_int`），`tradingnote_taifex.py` 則乾脆自己重寫一份、空值判斷邏輯還跟原版分岔。統一抽成 `tradingnote_http.py`（`http_get_json`／`to_float`／`to_int`／`PriceFetchError`），四個模組一律從這裡 import。
- **驗證**（本次對話補做，先前 session 沒有留下驗證紀錄）：`python3 -m py_compile` 全部通過；用 `/opt/homebrew/bin/python3.12` import `tradingnote_core`／`tradingnote_taifex`／`tradingnote_finmind`／`tradingnote_history`／`tradingnote.py` 成功；headless GUI smoke test（見下方兩則 2026-08-07 change log）在這個版本之上疊加其他改動後仍正常運作，未發現行為差異。
- **已 commit**：`d93cb86`。

## 2026-08-07：優化快取結構——抽出共用 `tradingnote_cache.py`
- **動機**：專案裡有四份各自重寫的 TTL 快取邏輯：`tradingnote_core.get_market_snapshot`（`price_cache.json`）與 `tradingnote_taifex.get_cached_daily_futures_report`（`futures_cache.json`）兩份幾乎一模一樣的「讀檔案→檢查 `fetched_at` 是否在 TTL 內→過期或不存在就重抓→寫回檔案，API 失敗時退回舊快取」；`tradingnote_core._tpex_valuation_cache`（單一全域 `(fetched_at, data)` tuple）與 `tradingnote_finmind._dataset_cache`（以 `(dataset, ticker, lookback_days)` 為 key 的 dict）兩份幾乎一模一樣的「命中未過期快取直接回傳、否則重新呼叫並存入」行程內記憶體快取。比照先前 `tradingnote_http.py` 的做法（見「2026-08-05 之前」的未 commit 異動 #3）抽成共用模組。
- **改法**：新增 `tradingnote_cache.py`，提供：
  - `load_fresh_file_cache(cache_path, ttl_seconds)` / `load_stale_file_cache(cache_path)` / `write_file_cache(cache_path, payload)`：檔案 JSON TTL 快取的讀／不管新鮮度讀／寫，payload 是除了 `fetched_at` 以外要存的內容（`{"prices": {...}}` 或 `{"rows": [...]}`），呼叫端自己決定 key 名稱。
  - `TTLCache` class：行程內記憶體 TTL 快取，`get_or_fetch(key, fetch_fn)`，key 用 `None` 代表只有單一份快取（`_tpex_valuation_cache` 的用法）、用 tuple 代表依查詢條件分開快取（`_dataset_cache` 的用法）；`fetch_fn()` 拋例外不會被快取，語意跟改之前完全一致。
  - `tradingnote_core.py`：`get_market_snapshot` 改用 `load_fresh_file_cache`/`load_stale_file_cache`/`write_file_cache`；`_tpex_valuation_cache` 改成 `TTLCache` 實例，`get_tpex_valuation` 改成一行 `get_or_fetch`。
  - `tradingnote_taifex.py`：`get_cached_daily_futures_report` 同樣改用檔案快取三個函式，拿掉自己的 `json`/`Path`/`datetime` import（不再需要）。
  - `tradingnote_finmind.py`：`_dataset_cache` 改成 `TTLCache` 實例，`_fetch_dataset` 內部抓取邏輯包成 closure `_do_fetch`，交給 `get_or_fetch` 處理快取判斷（`_call_timestamps.append` 仍只在 closure 真的被呼叫、也就是快取未命中時才執行，用量統計語意不變）。
- **驗證（單元層級）**：`python3 -m py_compile` 全部通過；用真正的 `/opt/homebrew/bin/python3.12`（專案指定直譯器，不是系統內建 3.9，import 型別語法 `str | None` 需要 3.10+）import 三個改動模組成功；獨立腳本驗證 `TTLCache`（命中快取不重呼叫 fetch_fn、過期後重呼叫、`fetch_fn` 拋例外時不寫入快取）與檔案快取三函式（不存在回 `None`、寫入後讀得回來、TTL=0 視為過期）皆符合預期。
- **當時驗證（headless GUI smoke test，含真實網路）**：啟動流程、報價與期貨快取、TPEX 估值、FinMind 快取均通過；分頁數與內容以目前程式為準。
- **已 commit**（hash 見上方必讀簡介）。

## 2026-08-07：「期貨」分頁顯示資料日期
- **動機**：「期貨」分頁本身完全沒有日期資訊，使用者無法直接判斷目前顯示的 TX／MTX 收盤資料是哪一天的（例如今天 TAIFEX 還沒發布新資料、實際看到的是昨天收盤價）；要看資料日期得切去「設定」分頁的「資料新鮮度」區塊才看得到，不直覺。
- **改法**：`refresh_futures_tab` 的 `on_done` callback 裡，原本重新整理成功時把 `futures_status_label`（原本只在失敗時顯示錯誤訊息的欄位）清空成 `""`，改成用既有的 `_futures_snapshot_date(snapshot)`（本來就給「設定」分頁的資料新鮮度用）算出最新資料日期，顯示成「資料日期：YYYY-MM-DD」；查無日期（空快取）時仍顯示空字串。重新整理失敗時的行為不變，`on_error` 照舊把錯誤訊息蓋上去。
- **驗證**：headless GUI smoke test（`QT_QPA_PLATFORM=offscreen`，真實網路＋真實 `data/futures_cache.json`）驗證啟動流程跑完、背景 `run_task_in_thread` 完成後 `futures_status_label.text()` 顯示「資料日期：2026-08-07」，跟真實 TAIFEX 資料的日期一致。
- **已 commit**（hash 見上方必讀簡介）。

## 2026-08-05：資金流向分析頁 —— 泡泡大小正規化 + 個股量比異常清單
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

## 2026-08-03：期貨資料源從 Fugle 即時報價換成 TAIFEX 官方盤後行情
- **原因**：「期貨」分頁原本串接 Fugle 的 `data-futopt` 即時行情（`tradingnote_fugle.py`，已刪除），但使用者實測啟動軟體出現異常；查證 `developer.fugle.tw` 定價文件後發現 futopt（期貨/選擇權）完全不在免費方案內——連歷史/盤後資料都沒有，只有 intraday 且需要付費 Developer 方案（NT$1,499/月起）才能呼叫，這正是異常的原因（免費 key 呼叫回 403）。
- **改法**：新增 `tradingnote_taifex.py`，改打 TAIFEX 官方公開、免金鑰的「期貨每日交易行情」端點（`https://openapi.taifex.com.tw/v1/DailyMarketReportFut`），一次回傳全市場所有期貨契約（含價差單），從中篩出 TX／MTX 近月合約的「一般」（日盤）與「盤後」（夜盤）兩筆收盤彙總。已用真實 API 呼叫驗證過欄位與近月判斷邏輯正確。
- **GUI 影響**：拿掉 Fugle API Key 輸入框、搜尋框、自選合約清單（`fugle_watchlist`）、15 秒背景輪詢計時器；「期貨」分頁改為隨主要「重新整理」按鈕（`force_refresh`／`_apply_refresh_result`）一起更新，因為資料一天只變一次。
- **順便做的另一件事**：「重新整理」彈窗（`RefreshDialog`）原本只有文字狀態、沒有進度條，這次加了 `QProgressBar`，透過 `get_market_snapshot()` 新增的 `on_progress(done, total, label)` callback 回報 TWSE 抓取／TPEX 抓取／寫入快取／寫入歷史資料庫四個階段。
- **狀態**：此變動已 commit（`dba11b5`）並 push 到 `origin/main`（後續又有數個 commit，見上方必讀簡介的 Git 狀態，皆已同步）。
