"""每日選股：用籌碼＋量價條件從全市場篩出名單，每天資料更新後產生並存檔。

要調整條件請直接改這個檔案：
  1. 數值門檻 → 改下面「參數」區的常數。
  2. 增減／改寫條件 → 改 RULES 清單（每條是一個 Rule：key、顯示名稱、說明、判斷函式）。
     判斷函式收到單一個股的指標 dict（見 StockMetrics.as_dict 的欄位，缺資料的欄位是 None），
     回傳 True／False；指標值是 None 的條件一律視為不通過。需要新指標就在 compute_metrics 加欄位。
所有條件都通過才會入選。

目前條件（2026-10-14 定義；同日移除「融資連續減少」條件，融資指標仍會算並顯示在名單，只是不參與篩選。
要加回去：把下面 RULES 最前面註解起來的 margin_down 那條取消註解。
同日 screen_dates() 也不再要求融資融券已存（該資料常比收盤晚公布，若當作門檻會讓選股卡在舊日期不動）；
重新啟用 margin_down 規則時不需要跟著恢復這個門檻，缺資料的日子一樣會被 compute_metrics 算成 None 而篩不過）：
  1. 兩週內大戶增加：集保 400 張以上持股占比，最新一週 比 兩週前 增加超過 BIG_HOLDER_MIN_DELTA_PP 個百分點（目前 0.1）
  2. 增量比 > 1：今日成交量 ÷ 前 5 日均量（即量比）> 1
  3. MFI < 80：14 日資金流量指標（資料庫沒有最高／最低價，典型價用收盤價近似，
     資金流用成交金額）

擁有的表：
  screener_results  每個交易日入選的個股與當時的指標值
  screener_runs     每次篩選的摘要（母體檔數、各條件通過檔數、入選檔數）
價格、融資、集保都只讀取（tradingnote_history／tradingnote_chips 擁有的表）。
"""

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import tradingnote_chips as chips
import tradingnote_history as history

# ---------- 參數（要調整就改這裡） ----------
BIG_HOLDER_WEEKS_BACK = 2     # 與幾週前的集保週資料比較
BIG_HOLDER_MIN_DELTA_PP = 0.1  # 大戶占比至少要增加幾個百分點（> 這個值才通過；過濾四捨五入等級的雜訊）
MARGIN_DOWN_DAYS = 3          # 融資餘額連續減少幾個交易日（目前只用於顯示「融資連減」欄；規則已停用）
VOLUME_RATIO_DAYS = 5         # 量比的均量天數（前 N 日，不含今日）
VOLUME_RATIO_MIN = 1.0        # 量比要大於這個值
MFI_PERIOD = 14
MFI_MAX = 80.0                # MFI 要小於這個值
MIN_PRICE_ROWS = 500          # 一個市場當天至少有幾檔報價才算「完整的一天」
PRICE_SLACK_DAYS = 5          # 多抓幾個交易日，容許個股有幾天沒報價或收盤價空白
HOLDER_GAP_TOLERANCE_DAYS = 3  # 基準週與最新週的間隔要在 7 × 週數 ± 這個天數內，否則視為缺週
HOLDER_MAX_STALE_DAYS = 14    # 最新集保日距篩選日超過幾天，視為集保資料過舊


@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    description: str
    test: Callable[[dict], bool]


def _num(value):
    return value is not None


RULES = (
    Rule("big_holder", f"{BIG_HOLDER_WEEKS_BACK} 週內大戶增加",
         f"集保 400 張以上持股占比，最新一週比 {BIG_HOLDER_WEEKS_BACK} 週前增加",
         lambda m: _num(m["big_delta"]) and m["big_delta"] > BIG_HOLDER_MIN_DELTA_PP),
    # 2026-10-14 停用：融資連續減少（只顯示指標，不參與篩選）。要啟用就取消註解：
    # Rule("margin_down", "融資連續減少",
    #      f"融資餘額連續 {MARGIN_DOWN_DAYS} 個交易日比前一日少",
    #      lambda m: _num(m["margin_down_days"]) and m["margin_down_days"] >= MARGIN_DOWN_DAYS),
    Rule("volume_ratio", f"增量比 > {VOLUME_RATIO_MIN:g}",
         f"今日成交量 ÷ 前 {VOLUME_RATIO_DAYS} 日均量 > {VOLUME_RATIO_MIN:g}",
         lambda m: _num(m["volume_ratio"]) and m["volume_ratio"] > VOLUME_RATIO_MIN),
    Rule("mfi", f"MFI < {MFI_MAX:g}",
         f"{MFI_PERIOD} 日 MFI（收盤價近似）低於 {MFI_MAX:g}，不在超買區",
         lambda m: _num(m["mfi"]) and m["mfi"] < MFI_MAX),
)


# ---------- DB ----------

def _connect(db_path):
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS screener_results (
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            name TEXT,
            industry TEXT,
            market TEXT,
            close REAL,
            change_pct REAL,
            big_delta REAL,
            big_latest REAL,
            big_trail TEXT,
            margin_down_days INTEGER,
            margin_change_pct REAL,
            volume_ratio REAL,
            mfi REAL,
            PRIMARY KEY (date, ticker)
        );
        CREATE TABLE IF NOT EXISTS screener_runs (
            date TEXT PRIMARY KEY,
            universe INTEGER NOT NULL,
            matched INTEGER NOT NULL,
            rule_counts TEXT NOT NULL,
            holder_dates TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    # 既有資料庫補欄位：big_trail＝兩週比較期間每週的大戶占比（JSON，[[日期, 占比], ...]）
    columns = {row[1] for row in conn.execute("PRAGMA table_info(screener_results)")}
    if "big_trail" not in columns:
        conn.execute("ALTER TABLE screener_results ADD COLUMN big_trail TEXT")
        conn.commit()
    return conn


# ---------- 指標 ----------

def screen_dates(db_path):
    """可以篩選的交易日（由舊到新）：兩個市場報價都完整的日子（不要求融資融券已存，見上方說明）。"""
    conn = history._connect(db_path)
    try:
        counts = defaultdict(dict)
        for date, market, n in conn.execute(
                "SELECT date, market, COUNT(*) FROM daily_prices GROUP BY date, market"):
            counts[date][market] = n
    finally:
        conn.close()
    return sorted(d for d, c in counts.items()
                  if c.get("TWSE", 0) >= MIN_PRICE_ROWS and c.get("TPEX", 0) >= MIN_PRICE_ROWS)


def calculate_mfi(closes, trading_values, period=MFI_PERIOD):
    """MFI：典型價用收盤價，原始資金流用成交金額；資料不足 period+1 筆回傳 None。"""
    if len(closes) < period + 1:
        return None
    positive = negative = 0.0
    for i in range(len(closes) - period, len(closes)):
        if closes[i] > closes[i - 1]:
            positive += trading_values[i]
        elif closes[i] < closes[i - 1]:
            negative += trading_values[i]
    return 100.0 if negative == 0 else 100 - 100 / (1 + positive / negative)


def holder_window(holder_dates, as_of):
    """由集保日期（由舊到新）決定比較區間，回傳 (base, latest, trail_dates, 問題說明或 None)。

    以日曆天數驗證：基準週必須落在最新週往前 7 × BIG_HOLDER_WEEKS_BACK 天（±容許天數）內，
    最新週也不能比篩選日舊太多；不符合就回傳 None 區間與原因，大戶條件會全部不通過。"""
    holder_dates = [d for d in holder_dates if d <= as_of]
    if not holder_dates:
        return None, None, [], "沒有集保資料"
    latest = holder_dates[-1]
    latest_day = datetime.strptime(latest, "%Y-%m-%d").date()
    stale = (datetime.strptime(as_of, "%Y-%m-%d").date() - latest_day).days
    if stale > HOLDER_MAX_STALE_DAYS:
        return None, None, [], f"集保最新資料 {latest} 距篩選日 {as_of} 已 {stale} 天，大戶條件已略過"
    target = BIG_HOLDER_WEEKS_BACK * 7
    candidates = [(abs((latest_day - datetime.strptime(d, "%Y-%m-%d").date()).days - target), d)
                  for d in holder_dates[:-1]]
    if not candidates or min(candidates)[0] > HOLDER_GAP_TOLERANCE_DAYS:
        return None, None, [], (f"集保缺少 {latest} 往前 {BIG_HOLDER_WEEKS_BACK} 週的資料，"
                                "大戶條件已略過（請補回集保歷史）")
    base = min(candidates)[1]
    return base, latest, [d for d in holder_dates if base <= d <= latest], None


def _margin_down_days(balances):
    """由新到舊算融資餘額連續減少的天數（balances 由舊到新）。"""
    days = 0
    for i in range(len(balances) - 1, 0, -1):
        if balances[i] < balances[i - 1]:
            days += 1
        else:
            break
    return days


def compute_metrics(db_path, as_of, directory=None, warnings=None):
    """回傳 {ticker: 指標 dict}，母體＝as_of 當天有報價且在產業對照表內的四碼個股。
    資料有問題（集保缺週／過舊、MFI 資料不足）時，說明文字會附加到傳入的 warnings 清單。"""
    # max_age_days 設很大：篩選只讀現有產業對照表，不觸發網路重抓（對照表由其他流程更新）
    directory = directory if directory is not None else history.get_industry_directory(
        db_path, max_age_days=10 ** 6)
    need_days = max(MFI_PERIOD + 1, VOLUME_RATIO_DAYS + 1)
    warnings = [] if warnings is None else warnings
    mfi_missing = 0
    conn = history._connect(db_path)
    try:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_prices WHERE date <= ? ORDER BY date DESC LIMIT ?",
            (as_of, need_days + PRICE_SLACK_DAYS))][::-1]
        if not dates:
            return {}, (None, None)
        series = defaultdict(list)
        for date, ticker, close, change, volume, value in conn.execute(
                "SELECT date, ticker, close, change_pct, volume, trading_value FROM daily_prices "
                "WHERE date >= ? AND date <= ? ORDER BY date", (dates[0], as_of)):
            series[ticker].append((date, close, change, volume, value))
    finally:
        conn.close()

    margin_days = max(MARGIN_DOWN_DAYS + 1, 2)
    conn = _connect(db_path)
    try:
        mdates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_margin WHERE date <= ? ORDER BY date DESC LIMIT ?",
            (as_of, margin_days))][::-1]
        margin = defaultdict(dict)
        for date, ticker, balance in conn.execute(
                "SELECT date, ticker, margin_balance FROM daily_margin WHERE date >= ? AND date <= ?",
                (mdates[0], as_of)) if mdates else ():
            margin[ticker][date] = balance
    finally:
        conn.close()

    base, latest, trail_dates, holder_problem = holder_window(chips.tdcc_dates(db_path), as_of)
    if holder_problem:
        warnings.append(holder_problem)
    big = {}
    if latest:
        conn = chips._connect(db_path)
        try:
            for date in trail_dates:
                big[date] = dict(conn.execute(
                    "SELECT ticker, SUM(pct) FROM tdcc_holders WHERE data_date = ? "
                    f"AND level BETWEEN {chips.BIG_HOLDER_LEVEL} AND {chips.HUGE_HOLDER_LEVEL} "
                    "GROUP BY ticker", (date,)))
        finally:
            conn.close()

    metrics = {}
    for ticker, info in directory.items():
        rows = series.get(ticker)
        if not (len(ticker) == 4 and ticker.isdigit()) or not rows or rows[-1][0] != as_of:
            continue
        # 沒成交的日子（沒有報價列，或收盤價空白）以前一個收盤價延續、資金流算 0（平盤，不計入正負），
        # 這樣冷門股停牌／沒成交也能算；第一個有收盤價之前的日子不補，所以新上市天數不足仍會是 None
        by_date = {r[0]: r for r in rows}
        closes, values = [], []
        for d in dates:
            r = by_date.get(d)
            if r is not None and r[1] is not None:
                closes.append(r[1])
                values.append(r[4] or 0.0)
            elif closes:
                closes.append(closes[-1])
                values.append(0.0)
        mfi = calculate_mfi(closes, values)
        if mfi is None:
            mfi_missing += 1
        volumes = [r[3] for r in rows]
        today = rows[-1]
        prev = [v for v in volumes[:-1][-VOLUME_RATIO_DAYS:] if v is not None]
        volume_ratio = None
        if len(prev) == VOLUME_RATIO_DAYS and today[3] is not None and sum(prev) > 0:
            volume_ratio = today[3] / (sum(prev) / VOLUME_RATIO_DAYS)

        balances = [margin[ticker][d] for d in mdates if d in margin.get(ticker, {})]
        margin_down = margin_change = None
        if len(balances) == len(mdates) and len(balances) >= 2:
            margin_down = _margin_down_days(balances)
            if margin_down:
                start = balances[-1 - margin_down]
                margin_change = (balances[-1] - start) * 100.0 / start if start else None

        big_delta = big_latest = big_trail = None
        if latest and ticker in big.get(latest, {}) and ticker in big.get(base, {}):
            big_latest = big[latest][ticker]
            big_delta = big_latest - big[base][ticker]
            big_trail = [(d, round(big[d][ticker], 2)) for d in trail_dates if ticker in big.get(d, {})]

        metrics[ticker] = {
            "ticker": ticker, "name": info.get("name"), "industry": info.get("industry"),
            "market": info.get("market"),
            "close": today[1], "change_pct": today[2],
            "big_delta": big_delta, "big_latest": big_latest, "big_trail": big_trail,
            "margin_down_days": margin_down, "margin_change_pct": margin_change,
            "volume_ratio": volume_ratio,
            "mfi": mfi,
        }
    if mfi_missing:
        warnings.append(f"{mfi_missing} 檔因有效報價不足 {MFI_PERIOD + 1} 筆（多半是新上市）無法計算 MFI")
    return metrics, (base, latest)


def apply_rules(metrics, rules=RULES):
    """回傳 (入選指標列, {rule.key: 單獨通過檔數})。"""
    counts = {rule.key: 0 for rule in rules}
    matched = []
    for m in metrics.values():
        results = [rule.test(m) for rule in rules]
        for rule, ok in zip(rules, results):
            counts[rule.key] += ok
        if all(results):
            matched.append(m)
    matched.sort(key=lambda m: (-(m["big_delta"] or 0), m["ticker"]))
    return matched, counts


def run_screen(db_path, as_of=None, directory=None):
    """篩選並存檔（同一天重跑會覆蓋）。回傳 {"date","universe","matched","rule_counts","rows"}；
    沒有可篩選的日子回傳 None。"""
    dates = screen_dates(db_path)
    if as_of is None:
        as_of = dates[-1] if dates else None
    if as_of is None:
        return None
    warnings = []
    metrics, holder_dates = compute_metrics(db_path, as_of, directory, warnings)
    matched, counts = apply_rules(metrics)
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM screener_results WHERE date = ?", (as_of,))
        conn.executemany(
            "INSERT INTO screener_results (date, ticker, name, industry, market, close, change_pct, big_delta, big_latest, big_trail, margin_down_days, margin_change_pct, volume_ratio, mfi) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(as_of, m["ticker"], m["name"], m["industry"], m["market"], m["close"], m["change_pct"],
              m["big_delta"], m["big_latest"], json.dumps(m["big_trail"]), m["margin_down_days"], m["margin_change_pct"],
              m["volume_ratio"], m["mfi"]) for m in matched])
        conn.execute(
            "INSERT OR REPLACE INTO screener_runs VALUES (?,?,?,?,?,?)",
            (as_of, len(metrics), len(matched), json.dumps(counts),
             json.dumps(holder_dates), datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    finally:
        conn.close()
    return {"date": as_of, "universe": len(metrics), "matched": len(matched),
            "rule_counts": counts, "holder_dates": holder_dates, "rows": matched,
            "warnings": warnings}


def run_dates(db_path):
    """已存的篩選日期（新到舊）。"""
    conn = _connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT date FROM screener_runs ORDER BY date DESC")]
    finally:
        conn.close()


def load_run(db_path, date):
    """讀回某天已存的篩選結果（格式同 run_screen）；沒有回傳 None。"""
    conn = _connect(db_path)
    try:
        run = conn.execute(
            "SELECT universe, matched, rule_counts, holder_dates FROM screener_runs WHERE date = ?",
            (date,)).fetchone()
        if run is None:
            return None
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM screener_results WHERE date = ? ORDER BY big_delta DESC, ticker", (date,))]
        for row in rows:
            row["big_trail"] = json.loads(row["big_trail"]) if row.get("big_trail") else None
    finally:
        conn.close()
    return {"date": date, "universe": run[0], "matched": run[1],
            "rule_counts": json.loads(run[2]), "holder_dates": tuple(json.loads(run[3] or "[null,null]")),
            "rows": rows}
