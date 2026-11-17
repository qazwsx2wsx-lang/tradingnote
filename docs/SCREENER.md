# 每日選股（tradingnote_screener.py）

資料每天更新後，從全市場四碼個股（產業對照表內、當天有報價）篩出同時符合所有條件的名單，結果存進 `history.db`，
資金流向頁「每日選股」顯示。

## 目前的條件（2026-10-14 定義、2026-10-30 加 KD 鈍化、2026-11-17 移除大戶增加，全部都要通過）
| # | 條件 | 定義 | 參數 |
|---|---|---|---|
| 2 | 增量比 > 1 | 今日成交量 ÷ 前 N 日均量（量比） | `VOLUME_RATIO_DAYS`（5）、`VOLUME_RATIO_MIN`（1.0） |
| 3 | MFI < 80 | 14 日 MFI；典型價用收盤價近似、資金流用成交金額 | `MFI_PERIOD`、`MFI_MAX` |
| 4 | KD 鈍化 | K 值連續 3 個交易日（含篩選日）≥ 80（高檔鈍化）**或** ≤ 20（低檔鈍化）；KD 用最高／最低價，與個股彈窗 KD 圖同一個函式（9 日 RSV、2/3 與 1/3 平滑）。K 值要連續算出 30 天才採用 | `KD_PERIOD`（9）、`KD_HIGH`、`KD_LOW`、`KD_DULL_DAYS`（3）、`KD_WARMUP_DAYS`（30） |

「增量比」我解讀成量比（今日量 ÷ 前 5 日均量）；若原意不同，改 `compute_metrics` 的 `volume_ratio` 即可。
指標值是 None（資料不足）的條件一律不通過。

**已停用**：
- 兩週內大戶增加（2026-11-17 移除）：集保週資料只靠 `scripts/fetch_daily.py` 累積，斷更（2026-10-16 之後）時這條讓名單整個變 0 檔。大戶占比Δ／走勢仍會算並顯示在名單，只是不參與篩選；要啟用就取消 `RULES` 裡 `big_holder` 那段註解（參數 `BIG_HOLDER_WEEKS_BACK`、`BIG_HOLDER_MIN_DELTA_PP`）。表格的 # 2～4 沿用原編號。
- 融資連續減少（2026-10-14 移除）。融資連減天數與變化仍會算並顯示在名單，只是不參與篩選；
要啟用就取消 `RULES` 裡 `margin_down` 那段註解（參數 `MARGIN_DOWN_DAYS`，預設 3）。

## 怎麼調整
- 改門檻：直接改 `tradingnote_screener.py` 上方「參數」常數。
- 增減或改寫條件：改 `RULES`（每條是 `Rule(key, label, description, test)`，`test` 收到單一個股的指標 dict）。
  需要新指標就在 `compute_metrics` 加欄位，並在 `screener_results` 表加欄位（新增欄位要處理既有資料庫，或刪掉 `screener_results`
  讓它重建）。
- 改完後在「每日選股」按「重新篩選最新一天」；排程 `scripts/fetch_daily.py` 會在更新完法人與籌碼資料後自動篩選。

## 需要的資料
- 報價：兩個市場當天都完整（`screen_dates`；不要求融資融券已存）。
- 集保：只用於名單的大戶占比Δ／走勢欄（不影響入選）；需要最新一週與兩週前的資料，用「補集保大戶歷史…」選「最近 3 週」補齊（每週資料，之後每週由排程累積）。
- 融資：`tradingnote_chips.backfill_margin`（排程每天更新）。
- 最高／最低價：條件 4 需要最近約 40 個交易日的高低價（`daily_prices.high/low`，2026-10-30 起才開始存）。舊日子用 `python scripts/backfill_high_low.py` 補（預設最近 `KD_BACKFILL_DAYS`＝60 個交易日，上市、上櫃一天各一次官方 API）；`scripts/fetch_daily.py` 每天也會順便補。缺高低價的股票 KD 為 None、條件不通過，篩選結果的 warnings 會列出檔數。

## 存檔
`screener_results`（每天入選個股與指標）、`screener_runs`（母體檔數、各條件單獨通過檔數、集保比較的兩週日期）。
