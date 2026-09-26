# 法人資金流去哪？— 資料回補與每日排程

GUI 左側導覽「法人資金流去哪？」頁讀的是 `data/history.db` 裡預先算好的法人歷史表
（`tradingnote_institutional_history.py`）。這裡的腳本負責把資料抓進來。所有腳本都要用
Python 3.12 執行（**不要用系統內建 python3**，TWSE／TPEx 憑證在新版 Python 會驗證失敗，
見 `ARCHITECTURE.md`）。

## 回補

```bash
/opt/homebrew/bin/python3.12 scripts/backfill.py            # 最近 60 個交易日
/opt/homebrew/bin/python3.12 scripts/backfill.py --days 120 # 自訂天數
```

- 每個請求間隔 3 秒，每個交易日 4–6 個請求，60 天約 20 分鐘；可以隨時中斷重跑，已完成的日期會跳過。
- 非交易日記在 `institutional_calendar` 表，下次不再重打。
- 某天只有部分端點有資料（例如櫃買還沒公布）時整天不寫入，下次重跑再補。
- GUI 頁面上的「更新法人資料」按鈕做的是同一件事（背景執行，不會卡住畫面）。

## 每日排程（交易日 16:30）

`scripts/fetch_daily.py` 會抓今天的資料並順便補上最近漏掉的交易日；非交易日或資料還沒公布時
正常結束、不寫入任何東西。

### macOS launchd（建議：電腦睡眠錯過時間，喚醒後會補跑）

```bash
sed "s#__REPO__#$(pwd)#g" scripts/com.tradingnote.fetch-daily.plist > ~/Library/LaunchAgents/com.tradingnote.fetch-daily.plist
launchctl load ~/Library/LaunchAgents/com.tradingnote.fetch-daily.plist
```

### cron（Linux／Windows WSL 或一直開機的主機；時區需為 Asia/Taipei）

```cron
30 16 * * 1-5 cd /path/to/tradingnote && python3.12 scripts/fetch_daily.py >> data/fetch_daily.log 2>&1
```

### Windows 工作排程器

建立每週一～五 16:30 的工作，程式為 `.venv\Scripts\python.exe`，引數 `scripts\fetch_daily.py`，
起始位置設為 tradingnote 資料夾。

## 類股分類

預設用 `industry_map` 的官方產業別（上市櫃，排除 ETF／權證）。要細分或改名，編輯根目錄的
`sector_overrides.json`：

```json
{
  "stocks": { "2330": "晶圓代工", "2454": "IC設計" },
  "rename_sectors": { "航運業": "航運" },
  "exclude_sectors": ["臺灣存託憑證"]
}
```

改完執行 `python3.12 scripts/recompute.py`（不打 API，幾秒內完成），GUI 頁面切回來就會更新。

## 計算規則

- 盤後結論金額：直接採用證交所 BFI82U＋櫃買「三大法人買賣金額彙總」（含 ETF）。
- 個股金額（億）＝ 買賣超張數 × 1000 × 當日收盤價 ÷ 1e8；外資＝外陸資＋外資自營商。
- 類股＝成分股加總，外資／投信／自營商分開，另有合計。
- 近 5／20 日：交易日累計。加速流入＝近 5 日平均 − 第 6～10 日平均（不足 10 天不計算）。
- 連續買賣：買超為正、賣超為負的連續天數。近 5 日漲跌：成分股 5 日複利漲跌幅的等權平均。

## 測試

```bash
/opt/homebrew/bin/python3.12 -m unittest test_tradingnote_institutional_history -v
```
