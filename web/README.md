# 法人資金流去哪？

每天台股盤後，整理外資、投信、自營商在各類股的買賣超，一眼看出資金正在加碼或撤出哪些類股。

這個網頁是 tradingnote 的一部分，分成兩層：

| 層 | 位置 | 負責 |
|---|---|---|
| 資料與計算（Python，零外部依賴） | `tradingnote_institutional_history.py`、`scripts/` | 抓證交所／櫃買資料、寫入 `data/history.db`、預先算好所有指標 |
| 顯示（Next.js） | `web/` | 以唯讀方式讀同一個 SQLite，提供 API 與頁面 |

桌面 GUI 的「主力同步買超」泡泡圖也會讀同一批歷史資料（流向區間都有資料時改用區間加總）。

## 安裝

需求：Python 3.12（**不要用系統內建 python3**，TWSE／TPEx 憑證在新版 Python 會驗證失敗，見 `ARCHITECTURE.md`）、Node.js 20 以上。

```bash
cd web
npm install
```

## 回補資料

```bash
/opt/homebrew/bin/python3.12 scripts/backfill.py            # 最近 60 個交易日
/opt/homebrew/bin/python3.12 scripts/backfill.py --days 120 # 自訂天數
```

- 每個請求間隔 3 秒，每個交易日要打 4–6 個請求，60 天約 20 分鐘；可以隨時中斷重跑，已完成的日期會跳過。
- 非交易日會記在 `institutional_calendar` 表，下次不再重打。
- 某天只有部分端點有資料（例如櫃買還沒公布）時整天不寫入，下次重跑再補。

## 啟動網頁

```bash
cd web
npm run dev        # http://localhost:3000
```

資料庫路徑預設為 `../data/history.db`，可用環境變數 `HISTORY_DB` 覆寫。

## 每日排程（交易日 16:30）

`scripts/fetch_daily.py` 會抓今天的資料並順便補上最近漏掉的交易日；非交易日或資料還沒公布時正常結束、不寫入任何東西。

### macOS launchd（建議：電腦睡眠錯過時間，喚醒後會補跑）

```bash
sed "s#__REPO__#$(pwd)#g" scripts/com.tradingnote.fetch-daily.plist > ~/Library/LaunchAgents/com.tradingnote.fetch-daily.plist
launchctl load ~/Library/LaunchAgents/com.tradingnote.fetch-daily.plist
```

### cron（Linux 或一直開機的主機；時區需為 Asia/Taipei）

```cron
30 16 * * 1-5 cd /path/to/tradingnote && /usr/bin/python3.12 scripts/fetch_daily.py >> data/fetch_daily.log 2>&1
```

### GitHub Actions

`data/` 沒有進 git，所以要在 Actions 上跑，得用 cache 把 `history.db` 留到下一次，並把結果交給部署網頁的地方。範例（UTC 08:30 ＝ 台北 16:30）：

```yaml
name: fetch-daily
on:
  schedule: [{ cron: "30 8 * * 1-5" }]
  workflow_dispatch:
jobs:
  fetch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - uses: actions/cache@v4
        with:
          path: data/history.db
          key: history-db-${{ github.run_id }}
          restore-keys: history-db-
      - run: python scripts/fetch_daily.py
      - uses: actions/upload-artifact@v4
        with: { name: history-db, path: data/history.db }
```

## 類股分類

預設用 `industry_map` 的官方產業別（上市櫃，排除 ETF／權證）。要細分或改名，編輯根目錄的 `sector_overrides.json`：

```json
{
  "stocks": { "2330": "晶圓代工", "2454": "IC設計" },
  "rename_sectors": { "航運業": "航運" },
  "exclude_sectors": ["臺灣存託憑證"]
}
```

改完執行 `python scripts/recompute.py`（不打 API，幾秒內完成）。

## API

| 端點 | 說明 |
|---|---|
| `GET /api/dates` | 有資料的交易日（新到舊） |
| `GET /api/summary?date=` | 盤後結論：交易所公布的三大法人金額（億）＋一句摘要 |
| `GET /api/sectors?date=&investor=all\|foreign\|trust\|dealer` | 各類股：當日、近 5／20 日、加速流入、連續買賣、近 5 日漲跌 |
| `GET /api/sectors/:sector/history?days=30&investor=&date=` | 類股每日資金流 |
| `GET /api/sectors/:sector/stocks?date=&investor=` | 成分股明細（依當日買超排序） |

`date` 省略時用最近一個有資料的交易日；給的日期不是交易日時，退回它之前最近一個交易日。

## 計算規則

- 盤後結論金額：直接採用證交所 BFI82U＋櫃買「三大法人買賣金額彙總」（含 ETF）。
- 個股金額（億）＝ 買賣超張數 × 1000 × 當日收盤價 ÷ 1e8；外資＝外陸資＋外資自營商。
- 類股＝成分股加總，外資／投信／自營商分開，另有合計。
- 近 5／20 日：交易日累計。加速流入＝近 5 日平均 − 第 6～10 日平均（不足 10 天顯示「—」）。
- 連續買賣：買超為正、賣超為負的連續天數。近 5 日漲跌：成分股 5 日複利漲跌幅的等權平均。

## 測試

```bash
/opt/homebrew/bin/python3.12 -m unittest test_tradingnote_institutional_history -v
```

## 資料表（`data/history.db`，由 `tradingnote_institutional_history.py` 擁有）

`daily_institutional`（個股股數）、`market_summary`（交易所公布金額，元）、`institutional_calendar`、`sector_map`、`sector_metrics`（類股指標，億）、`stock_metrics`（個股當日／20 日累計／連續天數，億）。價格沿用既有 `daily_prices`。

本網站僅彙整統計公開市場資訊，不構成任何投資建議。
