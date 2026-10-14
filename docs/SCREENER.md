# 每日選股（tradingnote_screener.py）

資料每天更新後，從全市場四碼個股（產業對照表內、當天有報價）篩出同時符合所有條件的名單，結果存進 `history.db`，
資金流向頁「每日選股」顯示。

## 目前的條件（2026-10-14 定義，全部都要通過）
| # | 條件 | 定義 | 參數 |
|---|---|---|---|
| 1 | 兩週內大戶增加 | 集保 400 張以上持股占比（級別 12–15），最新一週 − 兩週前 > 0 個百分點 | `BIG_HOLDER_WEEKS_BACK`、`BIG_HOLDER_MIN_DELTA_PP` |
| 2 | 增量比 > 1 | 今日成交量 ÷ 前 N 日均量（量比） | `VOLUME_RATIO_DAYS`（5）、`VOLUME_RATIO_MIN`（1.0） |
| 3 | MFI < 80 | 14 日 MFI；資料庫沒有最高／最低價，典型價用收盤價近似、資金流用成交金額 | `MFI_PERIOD`、`MFI_MAX` |

「增量比」我解讀成量比（今日量 ÷ 前 5 日均量）；若原意不同，改 `compute_metrics` 的 `volume_ratio` 即可。
指標值是 None（資料不足）的條件一律不通過。

**已停用**：融資連續減少（2026-10-14 移除）。融資連減天數與變化仍會算並顯示在名單，只是不參與篩選；
要啟用就取消 `RULES` 裡 `margin_down` 那段註解（參數 `MARGIN_DOWN_DAYS`，預設 3）。

## 怎麼調整
- 改門檻：直接改 `tradingnote_screener.py` 上方「參數」常數。
- 增減或改寫條件：改 `RULES`（每條是 `Rule(key, label, description, test)`，`test` 收到單一個股的指標 dict）。
  需要新指標就在 `compute_metrics` 加欄位，並在 `screener_results` 表加欄位（新增欄位要處理既有資料庫，或刪掉 `screener_results`
  讓它重建）。
- 改完後在「每日選股」按「重新篩選最新一天」；排程 `scripts/fetch_daily.py` 會在更新完法人與籌碼資料後自動篩選。

## 需要的資料
- 報價：兩個市場當天都完整，且融資融券已存（`screen_dates`）。
- 集保：條件 1 需要最新一週與兩週前的資料；用「補集保大戶歷史…」選「最近 3 週」補齊（每週資料，之後每週由排程累積）。
- 融資：`tradingnote_chips.backfill_margin`（排程每天更新）。

## 存檔
`screener_results`（每天入選個股與指標）、`screener_runs`（母體檔數、各條件單獨通過檔數、集保比較的兩週日期）。
