# AGENTS.md

## 交接文件規則
- **`STATUS.md`** 是唯一的當前狀態來源。每次修改完程式碼要交接時，更新最上方的「必讀簡介」區塊——目前 git 狀態、未 commit 的異動、最近最需要注意的事——保持精簡。
- **`CHANGELOG.md`** 放某次改動的動機／改法／驗證細節，不要塞進 `STATUS.md` 的簡介。
- 目的：讓新開的 session 只需要讀 `STATUS.md` 最上面那一小段就能掌握現況，不用整份讀完才能開始工作，節省 token。

## 專案背景
完整背景見 `STATUS.md`；架構說明見 `ARCHITECTURE.md`（發現衝突以 `STATUS.md` 及程式碼實際行為為準）。
