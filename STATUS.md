# tradingnote 現況（STATUS.md）

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
├── refresh_concepts.py         # 獨立腳本：從官方資料重建 concepts.json
├── generate_concept_md.py      # 獨立腳本：產生概念股說明文件
├── tradingnote.py               # CLI（無「個股」「期貨」模組對應指令）
├── tradingnote_gui.py           # GUI：PySide6 + pyqtgraph，左側導覽列＋頁面堆疊（正在拆分，見下方「GUI Design System 重構」）
├── ui/                           # GUI design system + reusable components（2026-09-15 新增，進行中）
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

## GUI 現況：六個分頁（左側導覽列，2026-09-14 由上方分頁改版）
1. **資金流向分析**（預設頁）：pyqtgraph 泡泡圖（大小＝資金比重%，泡泡綠／紅／灰表達當日量價推估的偏流入／偏流出／中性），可觸控板/滾輪縮放平移，點擊泡泡看該產業前十大成分股；工具列「泡泡圖模式」可切換「動能」（X＝N日累積漲跌%、Y＝量比）／「估值」（X＝最新一筆 PER/PBR 加權中位數、Y＝近5日均額÷近20日均額）；兩種模式都有四象限淡色底與 hover 解讀。下方按分類切換：資金動向清單、產業熱度排行、個股資金流入前50、個股量比異常清單。另有「法人方向」子頁：從 TWSE T86、TPEx `tpex_3insti_daily_trading` 取得全市場真實淨買賣股數，依族群聚合後以三張左右發散圖顯示外資／投信／自營商（真實買賣超，淨額為股數×收盤價估算）。日期選擇一律用日曆式起始日期選擇器，沒有歷史資料的日期反白不能選，結束日固定今天／最新資料。
2. **部位紀錄**：`QTableWidget`（含族群／概念股欄位），新增/刪除/查價/重新整理；下方詳細資訊區塊選取部位時背景查 FinMind 顯示本益比/殖利率/股價淨值比＋三大法人 120 日累計買賣超趨勢圖。概念股清單來自 `concepts.json`（人工維護，見 `tradingnote_concepts.py`）。
3. **交易週誌**（2026-09-10 新增）：週一至週日七張卡片顯示持股合計金額變化、絕對變動最大前三檔與日誌摘要；選取日期可看全部持股並編輯自由文字日誌；前/後週、本週、日期跳轉、Ctrl+S、切換日期與關閉程式時自動保存；未來日期不可編輯。
4. **個股**：`QTreeWidget` 依產業族群列出全市場約1700檔股票，雙擊叫 FinMind API 查本益比/殖利率/股價淨值比/三大法人（`StockDetailDialog`）。彈窗「顯示完整籌碼面資訊」按鈕點下去才查跟部位紀錄頁相同的詳細資料＋分頁（歷史股價／三大法人／融資融券／VPT／MFI／技術分析），技術分析可切換 KD／MACD／均線／RSI 等 24 類指標（純本地 SQLite 計算，不額外呼叫 API）。
5. **期貨**：預設列出 TAIFEX 全部期貨商品盤後行情，搜尋框輸入才篩選。「標的」欄對照股票期貨標的、大額前10買/賣/淨/買佔比摘要欄；點選某列看完整大額未沖銷部位明細＋趨勢圖，雙擊開大彈窗。大額歷史由背景自動回補（綁定「設定」頁回補天數）。細節較多，見 `CHANGELOG.md` 2026-08-14 那幾則。
6. **設定**：自動檢測開關、回補天數（TWSE／TPEX／期貨大額交易人歷史共用）、回補按鈕。**沒有 FinMind API Token 輸入欄**（見下方「已知缺口」）。

## 已知缺口
- **「設定」頁沒有 FinMind API Token 輸入欄**：`finmind_token` 只能手動編輯 `data/settings.json`，且 GUI 裡有處提示文字會叫使用者「請先在『設定』分頁填入 FinMind API Token」——這個欄位不存在，會誤導使用者。
- **CLI 沒有「個股」「期貨」模組的對應指令**，這兩個目前只有 GUI 在用。
- **架構層級的技術債**（GUI 對話框樣板碼重複、紅綠上色邏輯重複、SQLite 連線邏輯重複等）：詳見 `ARCHITECTURE.md`「已知架構債務」一節。
- **泡泡圖大小是「資金比重%」（絕對金額線性換算）**，還不是能凸顯「小市值但爆量」的相對指標；要做的話需要改用量比之類的相對值決定泡泡半徑。
- 只有 EOD（收盤）/ 盤後資料，非即時報價——刻意選擇，見 `CHANGELOG.md`。
- 一批「已驗證邏輯，但未實機開 GUI 目視驗證」的歷史紀錄散落在 `CHANGELOG.md` 各條目裡（EPS 欄位、視窗放大鈕、泡泡圖近N日修正等），是驗證債務，不是功能缺口，需要時去 `CHANGELOG.md` 逐條找。

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

**還沒做**（下一輪候選，任選其一即可，不用照順序）：
- `SignalBadge`／`SectionCard`／`InsightCard` 目前都只各用在一處，還沒推廣到其他頁面（例如個股概覽的「技術面：偏多」之類——這些「趨勢判斷」邏輯目前程式裡還沒有，需要先設計規則，不是單純套用元件）。
- 泡泡圖／VPT／MFI／融資融券等 pyqtgraph 圖表系列色（`tradingnote_gui.py` 裡還有一批 `#1f77b4`／`#2ca02c` 之類的分類色，屬於資料序列配色，不是介面底色，這次刻意沒動）。
- Sidebar 分組（市場／分析／交易／資料）、Dashboard／個股頁的 progressive disclosure 重做——規格中風險較高、影響面較大的部分，建議等 component 庫更完整再做。

## 目前實際尚未 commit 的異動（2026-09-15）
以 `git status` 為準，這裡只是提示去哪找細節：
1. 個股本地技術線圖擴充（`tradingnote_technical.py`，24類指標圖表目錄）——見 `CHANGELOG.md` 2026-09-10。
2. 全新交易週誌功能（`tradingnote_journal.py` + 3 個測試檔，測試檔尚未 `git add`）——見 `CHANGELOG.md` 2026-09-10。
3. 架構重複整理＋文件重整（新增 `tradingnote_api_config.py`／`STATUS.md`／`CHANGELOG.md`，重寫 `ARCHITECTURE.md`，修改 `tradingnote_core.py`／`tradingnote_history.py`／`tradingnote_institutional.py`／`tradingnote_taifex.py`／`tradingnote_finmind.py`／`tradingnote_cache.py`／`refresh_concepts.py`，`tradingnote_gui.py` 拿掉左側導覽列圖示）——見 `CHANGELOG.md` 2026-09-15。
4. GUI design system 啟動＋dark mode 套用（新增 `ui/` 目錄、`assets/chevron-*-dark.svg`）——見上方「GUI Design System 重構」與 `CHANGELOG.md` 2026-09-15（續3／續4）。

## 如果要繼續開發，建議先讀
1. 這份 `STATUS.md`（現況最新）
2. `ARCHITECTURE.md`（模組邊界、依賴方向、資料流、跨模組共用慣例——2026-09-15 已重寫）
3. `tradingnote_gui.py`（GUI 全貌）
4. 需要某個功能當初為什麼這樣做、驗證過什麼：`CHANGELOG.md`（按日期找對應條目）
