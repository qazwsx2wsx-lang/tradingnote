# AGENTS.md

## 交接文件規則
- **`STATUS.md`** 是唯一的當前狀態來源。每次修改完程式碼要交接時，更新最上方的「必讀簡介」區塊——目前 git 狀態、未 commit 的異動、最近最需要注意的事——保持精簡。
- **`CHANGELOG.md`** 放某次改動的動機／改法／驗證細節，不要塞進 `STATUS.md` 的簡介。
- 目的：讓新開的 session 只需要讀 `STATUS.md` 最上面那一小段就能掌握現況，不用整份讀完才能開始工作，節省 token。

## 收工前自我驗證（硬性規則）
- **改過任何 `.py` 之後，收工／交接前一定要跑**：`.venv/Scripts/python.exe verify.py`，印出「驗證通過」（exit 0）才算完成。
  檢查內容：用的是專案 `.venv`、全部 `.py` 能編譯、全部 unittest（含 `test_architecture.py` 架構規則與 offscreen GUI 測試）。測試跑在臨時資料目錄，不碰 `data/`。
- **沒過就繼續修，不准收工**。不准用「既有失敗」「跟這次無關」帶過；真的修不了，就在 `STATUS.md` 必讀簡介寫明哪個檢查沒過、原因、下一步。
- 新功能要附測試；只跑過自己寫的單一測試檔不算驗證。改了 UI 要說明有沒有實機點過，沒點過就寫「未實機點擊」。
- Claude Code 有 Stop hook 自動執行（`.claude/settings.local.json` → `verify.py --hook`）：程式碼跟上次通過時一樣就直接放行，失敗會擋下收工並回傳失敗訊息，連續 3 次失敗才放行。
  其他工具（Codex 等）沒有 hook，必須自己手動跑。
- `STATUS.md` 必讀簡介要寫上最後一次 verify 結果（例如「verify 通過，336 tests」）。

## 專案背景
完整背景見 `STATUS.md`；架構說明見 `ARCHITECTURE.md`（發現衝突以 `STATUS.md` 及程式碼實際行為為準）。
