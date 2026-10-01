# tradingnote 現況（STATUS.md）

## 必讀簡介（2026-09-28）
- 工作目錄乾淨。HEAD 已經包含 9/26「法人資金流去哪？」（`96b655a`）＋本次 tzdata／測試隔離修正。
- **Claude（2026-09-28，Windows tzdata 依賴缺漏＋測試跨檔污染）**：
  `.venv` 補裝 `tzdata`（`requirements.txt` 新增 `tzdata; sys_platform == "win32"`）——
  Windows 沒有內建 IANA 時區資料庫，`tradingnote_institutional_history.py` 模組層級的
  `ZoneInfo("Asia/Taipei")` 原本直接 `ModuleNotFoundError`，擋掉一大批測試檔的 import。
  另外修了 `test_stock_charts.py` 對共用 `QApplication` 的 `setStyleSheet`/`setFont` 沒有
  `tearDownClass()` 還原，導致跑在它之後的 `test_tradingnote_journal_gui` 被牽連失敗（卡片
  高度 92≠132）的測試隔離 bug。**注意**：`tradingnote_institutional_history.py` 的 6 張自有表
  （`daily_institutional`／`market_summary`／`institutional_calendar`／`sector_map`／
  `sector_metrics`／`stock_metrics`）目前這台機器的 `data/history.db` 裡一張都還不存在——
  要跑過 `scripts/backfill.py` 或頁面內「更新法人資料」才會建立並有資料，不然「法人資金流去哪？」
  分頁打開會是空的。詳見 `CHANGELOG.md` 2026-09-28。
- **Claude（2026-09-26，「法人資金流去哪？」GUI 頁＋三大法人歷史）**：
  新增 `tradingnote_institutional_history.py`（6 張自有表＋讀取函式）、
  `ui/pages/institutional_flow_page.py`（左側導覽第 2 頁）、`scripts/`
  （backfill／fetch_daily／recompute／launchd 範本，說明見 `scripts/README.md`）、
  `sector_overrides.json`、`test_tradingnote_institutional_history.py`；
  改 `tradingnote_history.py`（新增 `fetch_tpex_historical_day`，上櫃歷史收盤
  不必再靠 FinMind）、`tradingnote_flow.py`（「主力同步買超」區間有歷史時用
  區間加總）、`tradingnote_api_config.py`。原本的 Next.js `web/` 已移除（user
  要求整合進 GUI）。細節見 `CHANGELOG.md` 2026-09-26。`test_tradingnote_flow_gui` 的 1 個失敗在 HEAD
  版核心模組上同樣失敗，不是這次造成的。
- **Claude（2026-09-23，個股籌碼動能徽章＋量價背離判斷）**：
  `tradingnote_gui.py`（改）、`test_tradingnote_chip_badges.py`（新）。
  `_stock_trend_badges` 新增「量價背離」標籤（趨勢/量能交叉比對）；新增
  `_chip_momentum_badges`（籌碼方向／連買連賣天數／融資餘額變化），接到
  `StockDetailDialog` 新的 `chip_row`；`_populate_trend_badge_row` 重構成
  通用的 `_populate_badge_row(layout, badges, empty_text)`。13 項新測試
  通過，既有測試無新增失敗。詳見 `CHANGELOG.md` 2026-09-23。
- **Claude（2026-09-24，期貨頁 SSF／大額部位抓取失敗改為可見警示）**：
  `tradingnote_gui.py`（`refresh_futures_tab`）。user 回報「期貨連動個股
  查詢失效」——查到 `_filter_futures_table` 用股票代號／名稱搜尋股票期貨
  完全靠 `self._futures_ssf_map`，但這個 map 抓取失敗時（`PriceFetchError`）
  原本靜默退回空字典，沒有任何提示，搜尋悄悄找不到任何股票期貨列，看起來
  就像功能整個壞掉；實測當下 TAIFEX `SSFLists` 端點本身資料／欄位正常，
  推論是當次抓取遇到瞬斷。修法：`fetch()`／`on_done` 多帶
  `ssf_failed`／`large_traders_failed` 兩個 bool，任一為真時在
  `futures_status_label` 附加警告文字，不再完全靜默。沒有新增單元測試
  （這個函式高度耦合 GUI dialog 狀態，跟既有慣例一致，其餘期貨頁邏輯也
  沒有專屬測試）；`py_compile` 過、既有 test_*.py 無新增失敗。詳見
  `CHANGELOG.md` 2026-09-24。**未做**：沒有實機重現「抓取失敗」情境驗證
  警告文字實際顯示效果（本地無法穩定重現網路瞬斷）。
- 系統 `python3` 是 3.9.6（不含 PySide6，也不支援本專案用到的 `X | None`
  型別語法），這個環境要測試/跑 GUI 得用 `/opt/homebrew/bin/python3.12`
  （已裝好 PySide6 0.14／pyqtgraph）。

跟 Codex 共用這個專案資料夾。這份文件是**唯一的當前狀態來源**：只保留現在為真的事實，過時的內容直接刪掉／改寫，不要加註「已過時」保留對照——歷史脈絡、某次改動當時的驗證細節去 `CHANGELOG.md` 查。**每次交接前，把最新、最需要注意的事更新到這份文件，並保持精簡**；細節寫進 `CHANGELOG.md`，不要塞在這裡。

**開始工作前，Git 狀態一律以實際指令為準，不要只看這份文件的文字敘述**（這份文件的前身 HANDOFF.md 曾經因為手寫 commit hash／未 commit 清單，跟實際狀態脫節，見 `CHANGELOG.md` 2026-09-15 條目）：
```
git status
git log --oneline -10
```

## 這是什麼
台股部位紀錄＋產業資金流向＋個股基本面查詢＋期貨盤後行情的桌面程式（PySide6 + pyqtgraph），採「核心邏輯與介面分離」原則：核心模組不依賴任何 UI，CLI 與 GUI 各自 import。位置：`/Users/zhengyufan/Claude/tradingnote`（Windows 環境下路徑不同，以實際 clone 路徑為準），跟同層 `trade-journal` 完全無關、無資料共用。remote `origin` → `https://github.com/qazwsx2wsx-lang/tradingnote.git`。

架構層級的設計決策（模組邊界、依賴方向、資料流、SQLite schema 慣例）看 `ARCHITECTURE.md`（2026-09-15 已重寫，跟現在的模組結構一致）；發現兩者跟程式碼實際行為衝突時，以程式碼實際行為為準。

## 檔案結構
```
tradingnote/
├── tradingnote_core.py         # 核心：部位模型、JSON 持久化、TWSE/TPEX 查價、損益計算、settings.json
├── tradingnote_history.py      # 核心：股票歷史 SQLite、產業分類、產業資金流向分析、個股量比異常清單
├── tradingnote_flow.py         # 核心：資金流向期間模型、儀表板結果集合、統一分析服務與快取
├── tradingnote_institutional.py # 核心：TWSE/TPEx 全市場三大法人買賣超與族群聚合（「法人方向」用）
├── tradingnote_institutional_history.py # 核心：三大法人逐日歷史回補＋類股指標預先計算（「法人資金流去哪？」頁與「主力同步買超」用）
├── sector_overrides.json        # 「法人資金流去哪？」類股覆寫（人工維護）
├── scripts/                     # backfill.py／fetch_daily.py／recompute.py／launchd 範本
├── tradingnote_technical.py    # 核心：由共用 OHLCV 計算 24 類技術指標（MA/EMA/KD/MACD/RSI/...）
├── tradingnote_journal.py      # 核心：交易週誌與持股週曆（2026-09-10 新增）
├── tradingnote_finmind.py      # 核心：FinMind API（本益比/殖利率/三大法人/融資融券等），僅 GUI 使用
├── tradingnote_concepts.py     # 核心：概念股分類（讀 concepts.json，人工維護、非 API 資料）
├── concepts.json                # 人工維護的概念股清單，跟 trade-journal/themes.json 無關、不共用
├── tradingnote_taifex.py       # 核心：TAIFEX 期貨每日行情＋大額交易人未沖銷部位（含歷史）
├── tradingnote_http.py         # 核心：共用 HTTP／數值解析工具
├── tradingnote_cache.py        # 核心：共用檔案／記憶體 TTL 快取工具（含 fetch_with_file_cache）
├── tradingnote_api_config.py   # 核心：集中 TWSE/TPEx/FinMind/TAIFEX 端點網址（2026-09-15 新增）
├── tradingnote_paths.py        # 核心：CLI／GUI 共用的檔案路徑（AppPaths／APP_PATHS）
├── tradingnote_tasks.py        # 核心：GUI 背景執行緒任務共用 helper
├── tools/                       # 開發／維護用獨立腳本（不屬於 app 本體）
│   ├── refresh_concepts.py      # 從官方資料重建 concepts.json
│   ├── generate_concept_md.py   # 產生 docs/CONCEPT.MD 概念股說明文件
│   ├── bench_chart_loading.py   # 圖表建立效能基準
│   └── claude.bat               # Windows：在專案根目錄開 Claude Code
├── docs/                        # CONCEPT.MD（自動產生）、GEMINI.md（FinMind API 參考）
├── tradingnote.py               # CLI（無「個股」「期貨」模組對應指令）
├── tradingnote_gui.py           # GUI：PySide6 + pyqtgraph，左側導覽列＋頁面堆疊（正在拆分，見下方「GUI Design System 重構」）
├── ui/                           # GUI design system + reusable components（2026-09-15 新增，進行中）
│   ├── pages/institutional_flow_page.py # 「法人資金流去哪？」頁（2026-09-26）
│   ├── theme.py                 # 顏色／字型／全域 QSS token（含 dark mode、TONE_COLORS）
│   ├── format.py                # gain_loss_color() 等顯示用格式化 helper
│   └── components/
│       ├── stat_card.py         # StatCard(title/value/unit/change/status)
│       ├── signal_badge.py      # SignalBadge(text, tone)
│       ├── section_card.py      # SectionCard(title, description) + body_layout
│       └── insight_card.py      # InsightCard(headline, detail, tone)：rule-based 數據轉敘述
├── ARCHITECTURE.md               # 本專案架構文件（模組邊界／依賴方向／資料流，2026-09-15 重寫）
└── data/                         # 執行時自動建立，git 已忽略（.gitignore）
    ├── positions.json            # 使用者紀錄的部位
    ├── price_cache.json          # 全市場收盤價快取（30分鐘 TTL）
    ├── settings.json             # auto_check_continuity、finmind_token、backfill_target_days 等本機設定
    └── history.db                # SQLite：股票歷史價格／產業分類／估值／週誌快照 + 期貨大額交易人歷史
```

## GUI 現況：七個分頁（左側導覽列，2026-09-14 由上方分頁改版）
1. **資金流向分析**（預設頁）：pyqtgraph 泡泡圖，2026-09-16 改版——**泡泡填色＝所屬分類**（`_category_color()`，依名稱雜湊決定色相，同一分類永遠同一顏色，跟下方清單「分類」欄的色塊 icon 對照一致）、**外框顏色＝方向**（原本整顆泡泡只有紅/綠/灰三色代表當日量價方向，現在改成外框加粗表達，填色跟方向分開兩個視覺通道）。`ScatterPlotItem` 加 `antialias=False`（只針對這個 widget，不動全域抗鋸齒設定）修正 hover 時的卡頓。可觸控板/滾輪縮放平移，點擊泡泡看該產業前十大成分股；工具列「泡泡圖模式」三選一：「動能」（X＝N日累積漲跌%、Y＝量比）／「估值」（X＝最新一筆 PER/PBR 加權中位數、Y＝近5日均額÷近20日均額）／**「主力同步買超」**（新增，X＝三大法人方向一致性分數 -3～+3、Y＝三大法人合計買賣超金額億元，外框依一致性分數而非當日漲跌決定；資料僅反映最新一筆三大法人快照，不受「流向區間」影響，見下方已知缺口）。三種模式都有四象限淡色底與 hover 解讀。下方按分類切換：資金動向清單（「分類」欄已加色塊 icon）、產業熱度排行、個股資金流入前50、個股量比異常清單。另有「法人方向」子頁：從 TWSE T86、TPEx `tpex_3insti_daily_trading` 取得全市場真實淨買賣股數，依族群聚合後以三張左右發散圖顯示外資／投信／自營商（真實買賣超，淨額為股數×收盤價估算，跟新的「主力同步買超」泡泡模式共用同一份 `dashboard.institutional_flow`）。日期選擇一律用日曆式起始日期選擇器，沒有歷史資料的日期反白不能選，結束日固定今天／最新資料。
2. **法人資金流去哪？**（2026-09-26 新增，`ui/pages/institutional_flow_page.py`）：讀 `tradingnote_institutional_history` 預先算好的表。上方盤後結論（一句摘要＋外資／投信／自營商／合計四張 StatCard，金額採交易所公布值）；下方四個分類按鈕切換：法人資金流向泡泡圖（X＝近 5 日、Y＝加速流入、大小可切成交金額／近 20 日絕對值，可展開「過去 30 天」）、今日法人買賣榜、近 5 日土洋操作、可排序熱力圖；右側「法人」切換合計／外資／投信／自營。點泡泡或熱力圖列開 `SectorDetailDialog`（30 日柱狀＋累計線、成分股表）。資料日期下拉只列有資料的交易日；「更新法人資料」背景跑 `ih.backfill()`。
3. **部位紀錄**：`QTableWidget`（含族群／概念股欄位），新增/刪除/查價/重新整理；下方詳細資訊區塊選取部位時背景查 FinMind，跟「個股」頁 `StockDetailDialog` 共用同一套 8 張趨勢圖（歷史股價／三大法人／法人分別／融資融券／VPT／MFI／借券賣出餘額／借券成交）。概念股清單來自 `concepts.json`（人工維護，見 `tradingnote_concepts.py`）。
4. **交易週誌**（2026-09-10 新增）：週一至週日七張卡片顯示持股合計金額變化、絕對變動最大前三檔與日誌摘要；選取日期可看全部持股並編輯自由文字日誌；前/後週、本週、日期跳轉、Ctrl+S、切換日期與關閉程式時自動保存；未來日期不可編輯。
5. **個股**：`QTreeWidget` 依產業族群列出全市場約1700檔股票，選取項目即時顯示右側摘要卡（含趨勢/動能/量能徽章），雙擊叫 FinMind API 查本益比/殖利率/股價淨值比/三大法人（`StockDetailDialog`，同樣有趨勢徽章）。彈窗「顯示完整籌碼面資訊」按鈕點下去才查跟部位紀錄頁相同的詳細資料＋分頁（歷史股價／三大法人／法人分別／融資融券／VPT／MFI／借券賣出餘額／借券成交／技術分析，共 8 張圖），技術分析可切換 KD／MACD／均線／RSI 等 24 類指標（純本地 SQLite 計算，不額外呼叫 API）。
6. **期貨**：預設列出 TAIFEX 全部期貨商品盤後行情，搜尋框輸入才篩選。「標的」欄對照股票期貨標的、大額前10買/賣/淨/買佔比摘要欄；點選某列看完整大額未沖銷部位明細＋趨勢圖，雙擊開大彈窗。大額歷史由背景自動回補（綁定「設定」頁回補天數）。細節較多，見 `CHANGELOG.md` 2026-08-14 那幾則。
7. **設定**：自動檢測開關、回補天數（TWSE／TPEX／期貨大額交易人歷史共用）、回補按鈕。**沒有 FinMind API Token 輸入欄**（見下方「已知缺口」）。

## 已知缺口
- **「設定」頁沒有 FinMind API Token 輸入欄**：`finmind_token` 只能手動編輯 `data/settings.json`，且 GUI 裡有處提示文字會叫使用者「請先在『設定』分頁填入 FinMind API Token」——這個欄位不存在，會誤導使用者。
- **CLI 沒有「個股」「期貨」模組的對應指令**，這兩個目前只有 GUI 在用。
- **架構層級的技術債**（GUI 對話框樣板碼重複、紅綠上色邏輯重複、SQLite 連線邏輯重複等）：詳見 `ARCHITECTURE.md`「已知架構債務」一節。
- **泡泡圖大小是「資金比重%」（絕對金額線性換算）**，還不是能凸顯「小市值但爆量」的相對指標；要做的話需要改用量比之類的相對值決定泡泡半徑。
- **「主力同步買超」泡泡模式只有在流向區間每一天都已回補（`scripts/backfill.py`）時才是區間加總**，否則退回最新一筆快照；GUI 畫面上沒有標示目前用的是哪一種。
- **`_category_color()` 用雜湊值決定色相，不保證任意兩個分類色相距離夠遠**：官方產業約35個分類時實測約 34/35 顏色可視覺區分（1 組偶爾撞色），分類數更多（概念主題/價值鏈細分類數百個）時撞色機率更高；如果使用者回饋易讀性不夠，可以調整 `_category_color()` 的飽和度/明度參數，或改成依實際出現的分類數量動態分配色相。
- 只有 EOD（收盤）/ 盤後資料，非即時報價——刻意選擇，見 `CHANGELOG.md`。
- 一批「已驗證邏輯，但未實機開 GUI 目視驗證」的歷史紀錄散落在 `CHANGELOG.md` 各條目裡（EPS 欄位、視窗放大鈕、泡泡圖近N日修正等），是驗證債務，不是功能缺口，需要時去 `CHANGELOG.md` 逐條找。
- **`gc.disable()` 是根據兩次實機重現的證據（都停在「Garbage-collecting」）做出的緩解措施，不是 100% 證實的根因**：如果之後還是遇到閃退（`PYTHONFAULTHANDLER=1` 執行 `tradingnote_gui.py` 可以重現堆疊），代表還有別的觸發路徑，需要繼續查；見 `CHANGELOG.md` 2026-09-17（續4）。

## GUI Design System 重構（進行中，2026-09-15 啟動）
user 提出完整規格：把 `tradingnote_gui.py` 拆成 `ui/theme.py`＋`ui/components/`＋`ui/pages/`＋`ui/widgets/`，建立 StatCard／SectionCard／SignalBadge／InsightCard／StockHeader 等 reusable component，最終讓首頁變成 progressive-disclosure 的現代分析 dashboard。明確要求**不要一次全部重寫**，小步進行，每輪都要確認 GUI 仍可啟動、既有功能不變。

**已完成**：
- `ui/theme.py`（顏色／字型／QSS）、`ui/format.py`（`gain_loss_color()`，取代 11 處重複三元式）、`ui/components/stat_card.py`（`StatCard`，支援 `bordered=False`／`title=None` 給嵌入既有 `stockHero` 容器用）。已換掉三處：「資金流向」分頁的今日偏流入/偏流出/成交最活躍三張卡、`StockDetailDialog` hero 價格區、交易週誌「個股摘要」的 `stock_preview_price`。
- **Dark mode 已拍板並套用**（2026-09-15 續4）：background `#0F1115`／card `#181B21`（token 名稱 `COLOR_CARD_BG`）／border `#292E38`／主要文字 `#F1F3F5`／次要文字 `#9DA5B4`／accent `#3B82F6`（hover 更亮 `#5B9DF9`，深色底下要「更亮」而非更暗，跟淺色主題方向相反）。QSS 裡原本約 20 處直接寫死的淺色 hex（白色輸入框、表格、下拉選單、disabled、選取底色、泡泡圖四象限底色等）已全部改用 token。
- `assets/chevron-down-dark.svg`／`chevron-up-dark.svg`：下拉選單箭頭原本是給淺色底設計的深色描邊，深色底下會看不清楚，新增了淺色描邊版本。
- `ui/components/signal_badge.py`（`SignalBadge`，tone=positive/negative/info/warning/special/neutral，色彩對照表在 `ui/theme.py` 的 `TONE_COLORS`，`InsightCard` 共用同一份）。用法：「資金流向」分頁泡泡圖圖例（偏流入/偏流出/中性三個徽章，取代原本一句話說明顏色）。
- `ui/components/section_card.py`（`SectionCard`，標題＋描述＋`body_layout` 讓呼叫端塞內容，沿用 `summaryCard` 卡片樣式）。用法：「法人方向」子頁整個包進一張卡（原本是頁面上直接鋪標題+提示+三張圖，沒有卡片邊界）。
- `ui/components/insight_card.py`（`InsightCard`，headline＋detail，rule-based 不接 LLM）。用法：`tradingnote_gui._flow_momentum_insight()`——從族群資金流向資料（量比≥1.5 且當日漲跌 |%|≥0.5 才夠格參與）挑出當天最極端的一筆量價訊號，生成一句話（例如「資金動能增強：{族群}今日成交量為近期均量的X倍，且價格同步走強」），沒有夠格的族群時顯示中性的「暫無明顯資金訊號」，不留白。放在「資金流向」分頁 StatCard 下方。
- **正綠負紅→正紅負綠**（2026-09-15 續7，已拍板並套用）：`COLOR_GAIN`/`COLOR_LOSS`（含對應的 `_TINT`）兩組 hex 直接對調，改成台灣市場「漲紅跌綠」慣例。因為所有呼叫端都是透過 `gain_loss_color()`／常數名稱取色，不是寫死 RGB，這次全部自動套用到全部畫面（個股漲跌、三大法人買賣超、大額交易人淨部位、資金流向徽章、泡泡圖四象限），沒有另外改任何呼叫端程式碼。
- **`SignalBadge` 推廣到「個股概覽」**（2026-09-15 續8／續9；2026-09-23 加量價背離＋籌碼徽章）：`tradingnote_gui._stock_trend_badges()` 解讀既有本地技術指標（`tradingnote_technical.calculate_indicators`，MA20／RSI(14)／量比 20），並交叉判斷第 4 個「量價背離」標籤，沒有新增指標計算，資料不足時顯示一致的「歷史資料不足...」空狀態文字。用在兩處：(1) `StockDetailDialog`（雙擊個股彈出的視窗）hero 下方；(2)「個股查詢」分頁右側 `stock_preview` 摘要面板，選取清單項目時即時更新。兩處共用 `_populate_badge_row()`／`_clear_layout()` 兩個 helper，不是各自重複一份 build/clear 邏輯（`_populate_badge_row` 原名 `_populate_trend_badge_row`，2026-09-23 改成接受已算好的 badges 列表，讓 `_chip_momentum_badges()` 也能共用同一個 render helper）。`StockDetailDialog` 另外新增 `chip_row`，顯示 `_chip_momentum_badges()`（籌碼方向同步分數／連買連賣天數／融資餘額變化），資料來自既有的 background fetch，沒有 FinMind token 時顯示空狀態。

**還沒做**（下一輪候選，任選其一即可，不用照順序）：
- `SectionCard`／`InsightCard` 目前都只各用在一處，還沒推廣到其他頁面。
- 泡泡圖／VPT／MFI／融資融券等 pyqtgraph 圖表系列色（`tradingnote_gui.py` 裡還有一批 `#1f77b4`／`#2ca02c` 之類的分類色，屬於資料序列配色，不是介面底色，這次刻意沒動）。
- Sidebar 分組（市場／分析／交易／資料）、Dashboard／個股頁的 progressive disclosure 重做——規格中風險較高、影響面較大的部分，建議等 component 庫更完整再做。

## 圖表架構統整：第一~三階段（2026-09-17，已完成，暫停在此重新評估）
使用者提出 7 階段圖表架構統整計畫（共用生命週期、延遲建立、統一資料模型…），
雙方同意先做到第三階段（延遲建立／延遲繪製）就停下來重新量測，**已完成**；
第四階段以後（統一資料模型、搬移全部繪圖函式、納入泡泡圖/期貨圖表）先不做，
除非之後發現還有必要。完整計畫見
`C:\Users\Evan\.claude\plans\read-tradingnote-handoff-md-virtual-hopcroft.md`。

**第一階段（效能基準）**：新增 `bench_chart_loading.py`（現已移到 `tools/`，
`QT_QPA_PLATFORM=offscreen`＋合成資料，不打任何 API）。量到「個股完整籌碼」
／「部位紀錄」詳細資訊當時都是一次建立＋populate 全部 8 張明細圖＋技術分析＋
雙資料比較（共 10 個 `StockChart` 實例），`PositionRecordPage`（其實是
`TradingNoteWindow._build_position_detail_section`）完全沒有延遲，一開部位
紀錄頁就建立，不管使用者有沒有選取任何一筆部位；且**光是 10 個空白
`StockChart()` 的建構就佔總成本 7 成左右**，填圖只佔約 3 成——確立階段三
的重點必須是「延遲建立 widget 本身」，不能只延遲填圖。

**第二階段（共用小工具）**：`ui/stock_charts.py` 新增 `apply_chart_theme()`／
`set_date_ticks()`，取代 `tradingnote_gui.py` 裡 6+／9+ 處逐字重複的主題設定
／日期軸 tick 計算；新增 8 個 `_xxx_chart_series(data)`（比照既有
`_technical_comparison_sources()` 寫法），直接從原始資料算「雙資料比較」可用
的序列，不依賴對應的 `StockChart` 是否已建立——這是階段三讓「雙資料比較」
分頁能獨立於其他 8 個分頁的前提。新增測試
`test_chart_series_helpers_match_populated_chart_series`（`test_stock_charts.py`）
驗證這 8 個函式跟「先 populate 真正的 StockChart 再讀 `.series()`」逐一比對
完全一致。

**第三階段（延遲建立＋延遲繪製）**：`ui/stock_charts.py` 新增通用
`LazyTabBuilder`（包裝 `QTabWidget`：分頁標籤一次建好，每個分頁的內容延遲到
第一次切到才建立，換資料源時 `reset()`＋`activate_current()` 只重新
populate 目前作用中的分頁，不強迫重建全部）。`tradingnote_gui.py` 新增
`DetailChartPanel`（8 明細圖＋技術分析＋雙資料比較共 10 分頁，`StockDetailDialog`
與 `TradingNoteWindow` 的部位詳細區塊共用同一份），取代原本的
`_render_detail_block`（拆成純文字的 `_render_detail_summary` ＋
`DetailChartPanel.set_data()`）。新增測試 `LazyTabBuilderTests`／
`DetailChartPanelTests`（`test_stock_charts.py`）驗證：只有作用中分頁建立
widget、切分頁不重建、`reset()` 後已建立的 widget 只重新 populate 不重建、
「雙資料比較」在其他 8 個分頁都沒被造訪過的情況下依然正確。另外用一支
一次性 smoke script（未保留，僅本次驗證用）以合成資料端到端跑過
`StockDetailDialog`（monkeypatch 掉 FinMind 網路呼叫與快取檔案 I/O），確認
真正接線（按鈕點擊→建立面板→背景資料回來→切分頁→雙資料比較）沒有問題。

**效能基準數字對照**（150 個交易日合成資料，每項 20 次取平均，本機量測，
不同機器絕對值會不同，僅供相對比較）：
| 量測項目 | 第一階段（改前） | 第三階段（改後） |
|---|---|---|
| 完整籌碼／部位詳細單次「開啟並顯示資料」總成本 | 335.03ms | **29.93ms**（`DetailChartPanel` 只建立目前作用中分頁） |
| 如果使用者依序切過全部 10 個分頁 | （同上，全部一次做完） | 370.31ms（跟改前總成本量級相近，符合預期——只是把成本從「開啟當下」延到「使用者實際切過去的當下」，沒有魔法省掉工作量，但大多數時候使用者不會切完全部 10 個分頁） |

「開啟並顯示資料」的成本降到約原本的 1/11——`PositionRecordPage` 這邊影響
最大：原本不管有沒有選部位都要付出約 335ms 建立全部 10 張圖，現在只有真的
被選取、且使用者切到的分頁才建立。原始數字存在 `bench_results_baseline.json`
／`bench_results_after_phase3.json`（未進 git，重跑
`python tools/bench_chart_loading.py <label>` 會覆寫/新增同名檔案）。

**未做（誠實揭露）**：沒有在這個環境用滑鼠實際操作驗證主觀「順不順」，改用
offscreen 腳本斷言＋一次性 smoke script 確認接線正確；`PositionRecordPage`
在同一次視窗開啟期間反覆切換部位的長時間穩定性（例如切 50 次會不會有殘留
物件／記憶體成長）沒有驗證；第四階段以後（統一資料模型、搬移繪圖函式、
納入泡泡圖/期貨圖表）維持原樣，沒有動。

## 目前實際尚未 commit 的異動
以 `git status` 為準。本次未 commit 的圖表空間調整見最上方必讀簡介及 `CHANGELOG.md` 2026-09-17「增加圖表空間」。先前圖表抽離、雙資料比較與延遲建立已在現有提交內。

## 如果要繼續開發，建議先讀
1. 這份 `STATUS.md`（現況最新）
2. `ARCHITECTURE.md`（模組邊界、依賴方向、資料流、跨模組共用慣例——2026-09-15 已重寫）
3. `tradingnote_gui.py`（GUI 全貌）
4. 需要某個功能當初為什麼這樣做、驗證過什麼：`CHANGELOG.md`（按日期找對應條目）
