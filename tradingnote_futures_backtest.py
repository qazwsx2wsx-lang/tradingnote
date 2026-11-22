"""個股期（SSF）實單績效統計、What-if 出場模擬、通用 CSV 匯入。

純資料層，不碰 Qt。資料來源是 tradingnote_tradejournal 的 trades／
trade_executions（instrument='SSF'），模擬用的日 K 由呼叫端提供
（預設 default_fetch_bars 走 tradingnote_taifex 的期交所個股期日 K）。

模擬假設（畫面與 docs/FUTURES_BACKTEST.md 同步說明）：
- 進場固定為實際的「第一筆進場日＋全部進場口數的平均價」，加碼視為同日進場。
- 從進場「隔天」的 K 棒開始檢查出場（進場當天盤中先後順序未知）。
- 開盤跳空越過停損／停利 → 以開盤價成交；同一根 K 同時碰到停損與停利 →
  保守當作先停損。
- 移動停損用「前一根為止」的最高（多）／最低（空）價計算，當根更新留給下一根。
- 合約最後交易日（第三個星期三）以結算價（無則收盤）強制出場。
- K 棒用到最後一天還沒出場、合約也還沒到期 → 標「未到期（最新收盤）」以最後收盤計。
- 只用一般（日盤）K 棒，夜盤不納入。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import tradingnote_tradejournal as tj
from tradingnote_core import TICKER_RE

CONTRACT_SIZES = {"標準型": 2000, "小型": 100}
SSF_TAX_RATE = tj.SSF_TAX_RATE


# ---------------------------------------------------------------------------
# 實單結果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosedTrade:
    trade_id: int
    symbol: str
    futures_code: str | None
    contract_month: str | None
    side: str
    contract_size: float
    qty: float
    entry_date: str
    exit_date: str
    avg_entry: float
    avg_exit: float
    gross_pnl: float
    commission: float
    tax: float
    net_pnl: float
    stop: float | None
    strategy: str
    tags: tuple[str, ...] = ()

    @property
    def holding_days(self):
        return (date.fromisoformat(self.exit_date) - date.fromisoformat(self.entry_date)).days

    @property
    def r_multiple(self):
        if not self.stop or self.stop <= 0:
            return None
        risk = abs(self.avg_entry - self.stop) * self.qty * self.contract_size
        return self.net_pnl / risk if risk > 0 else None


def _day(value):
    return str(value)[:10] if value else ""


def closed_trade_results(
    db_path,
    *,
    date_from=None,
    date_to=None,
    side=None,
    strategy_id=None,
    instrument=tj.INSTRUMENT_SSF,
):
    """已平倉交易 → ClosedTrade（依出場日排序）。date_from／date_to 篩出場日。"""
    strategies = {s.strategy_id: s.name for s in tj.list_strategies(db_path)}
    trades = tj.list_trades(
        db_path, status="CLOSED", strategy_id=strategy_id, instrument=instrument
    )
    results = []
    for summary in trades:
        if side and summary.side != side:
            continue
        trade = tj.get_trade(db_path, summary.trade_id)
        entry_action = "BUY" if trade.side == "LONG" else "SELL"
        entries = [e for e in trade.executions if e.action == entry_action]
        exits = [e for e in trade.executions if e.action != entry_action]
        if not entries or not exits:
            continue  # 用 close_trade 手動結案、沒有成交的交易不算
        exit_date = _day(exits[-1].executed_at)
        if date_from and exit_date < date_from:
            continue
        if date_to and exit_date > date_to:
            continue
        qty = sum(e.qty for e in entries)
        results.append(
            ClosedTrade(
                trade_id=trade.trade_id,
                symbol=trade.symbol,
                futures_code=trade.futures_code,
                contract_month=trade.contract_month,
                side=trade.side,
                contract_size=trade.contract_size or 1.0,
                qty=qty,
                entry_date=_day(entries[0].executed_at),
                exit_date=exit_date,
                avg_entry=trade.avg_entry_price or 0.0,
                avg_exit=trade.avg_exit_price or 0.0,
                gross_pnl=trade.realized_pnl,
                commission=trade.total_commission,
                tax=trade.total_tax,
                net_pnl=trade.net_pnl,
                stop=trade.initial_stop_loss if (trade.initial_stop_loss or 0) > 0 else None,
                strategy=strategies.get(trade.strategy_id, "未分類"),
                tags=tuple(t.name for t in trade.tags),
            )
        )
    results.sort(key=lambda r: (r.exit_date, r.trade_id))
    return results


# ---------------------------------------------------------------------------
# 績效統計
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupStats:
    key: str
    count: int
    wins: int
    net_pnl: float

    @property
    def win_rate(self):
        return self.wins / self.count if self.count else None


@dataclass(frozen=True)
class Stats:
    count: int = 0
    wins: int = 0
    losses: int = 0
    total_net: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    avg_win: float | None = None
    avg_loss: float | None = None
    largest_win: float | None = None
    largest_loss: float | None = None
    max_drawdown: float = 0.0
    max_drawdown_peak_date: str | None = None
    max_drawdown_trough_date: str | None = None
    max_consecutive_losses: int = 0
    avg_holding_days: float | None = None
    avg_r: float | None = None
    # 權益曲線：[(出場日, 累計淨損益)]，同一天多筆合併成一點
    equity_curve: tuple[tuple[str, float], ...] = ()
    drawdown_curve: tuple[tuple[str, float], ...] = ()
    by_month: tuple[GroupStats, ...] = ()
    by_side: tuple[GroupStats, ...] = ()
    by_strategy: tuple[GroupStats, ...] = ()

    @property
    def win_rate(self):
        return self.wins / self.count if self.count else None

    @property
    def profit_factor(self):
        """總獲利÷總虧損；沒有虧損單時回傳 None（畫面顯示 ∞ 或 —）。"""
        return self.gross_profit / abs(self.gross_loss) if self.gross_loss else None

    @property
    def payoff_ratio(self):
        if self.avg_win is None or not self.avg_loss:
            return None
        return self.avg_win / abs(self.avg_loss)

    @property
    def expectancy(self):
        return self.total_net / self.count if self.count else None


def _group(results, key_fn):
    groups = {}
    for r in results:
        key = key_fn(r)
        count, wins, net = groups.get(key, (0, 0, 0.0))
        groups[key] = (count + 1, wins + (1 if r.net_pnl > 0 else 0), net + r.net_pnl)
    return tuple(
        GroupStats(key=k, count=c, wins=w, net_pnl=n)
        for k, (c, w, n) in sorted(groups.items())
    )


def compute_stats(results):
    """results：有 exit_date／net_pnl／side／strategy／entry_date 屬性的物件
    （ClosedTrade）。淨損益＞0 算贏、＜0 算輸，剛好 0 兩邊都不算。"""
    results = sorted(results, key=lambda r: (r.exit_date, getattr(r, "trade_id", 0)))
    if not results:
        return Stats()
    wins = [r.net_pnl for r in results if r.net_pnl > 0]
    losses = [r.net_pnl for r in results if r.net_pnl < 0]

    equity = 0.0
    peak = 0.0
    peak_date = None
    max_dd = 0.0
    dd_peak_date = dd_trough_date = None
    curve = {}
    dd_curve = {}
    streak = max_streak = 0
    for r in results:
        equity += r.net_pnl
        curve[r.exit_date] = equity
        if equity > peak:
            peak, peak_date = equity, r.exit_date
        drawdown = peak - equity
        dd_curve[r.exit_date] = -drawdown
        if drawdown > max_dd:
            max_dd, dd_peak_date, dd_trough_date = drawdown, peak_date, r.exit_date
        if r.net_pnl < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        elif r.net_pnl > 0:
            streak = 0

    holding = [r.holding_days for r in results if hasattr(r, "holding_days")]
    r_values = [r.r_multiple for r in results if getattr(r, "r_multiple", None) is not None]
    return Stats(
        count=len(results),
        wins=len(wins),
        losses=len(losses),
        total_net=equity,
        gross_profit=sum(wins),
        gross_loss=sum(losses),
        avg_win=sum(wins) / len(wins) if wins else None,
        avg_loss=sum(losses) / len(losses) if losses else None,
        largest_win=max(wins) if wins else None,
        largest_loss=min(losses) if losses else None,
        max_drawdown=max_dd,
        max_drawdown_peak_date=dd_peak_date,
        max_drawdown_trough_date=dd_trough_date,
        max_consecutive_losses=max_streak,
        avg_holding_days=sum(holding) / len(holding) if holding else None,
        avg_r=sum(r_values) / len(r_values) if r_values else None,
        equity_curve=tuple(curve.items()),
        drawdown_curve=tuple(dd_curve.items()),
        by_month=_group(results, lambda r: r.exit_date[:7]),
        by_side=_group(results, lambda r: "多" if r.side == "LONG" else "空"),
        by_strategy=_group(results, lambda r: r.strategy),
    )


# ---------------------------------------------------------------------------
# What-if 出場模擬
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExitRule:
    """stop_mode：'none'／'pct'（進場價的 stop_pct%）／'plan'（交易原本設的停損）。
    take_profit_mode：'none'／'pct'（take_profit_pct%）／'r'（停損距離的 take_profit_r 倍，
    停損距離取本規則的停損；沒有停損時退回原計畫停損，再沒有就不停利）。
    max_hold_days：進場後第 N 根 K 收盤出場。trailing_pct：移動停損%。"""

    stop_mode: str = "none"
    stop_pct: float | None = None
    take_profit_mode: str = "none"
    take_profit_pct: float | None = None
    take_profit_r: float | None = None
    max_hold_days: int | None = None
    trailing_pct: float | None = None

    def describe(self):
        parts = []
        if self.stop_mode == "pct" and self.stop_pct:
            parts.append(f"停損 {self.stop_pct:g}%")
        elif self.stop_mode == "plan":
            parts.append("原計畫停損")
        if self.take_profit_mode == "pct" and self.take_profit_pct:
            parts.append(f"停利 {self.take_profit_pct:g}%")
        elif self.take_profit_mode == "r" and self.take_profit_r:
            parts.append(f"停利 {self.take_profit_r:g}R")
        if self.trailing_pct:
            parts.append(f"移動停損 {self.trailing_pct:g}%")
        if self.max_hold_days:
            parts.append(f"最多持有 {self.max_hold_days} 天")
        return "、".join(parts) or "抱到到期"


EXIT_STOP = "停損"
EXIT_TARGET = "停利"
EXIT_TRAIL = "移動停損"
EXIT_TIME = "持有天數"
EXIT_EXPIRY = "到期結算"
EXIT_NO_DATA = "未到期（最新收盤）"


@dataclass(frozen=True)
class SimResult:
    exit_date: str
    exit_price: float
    reason: str
    gross_pnl: float
    commission: float
    tax: float
    net_pnl: float
    bars_held: int


def third_wednesday(contract_month):
    """個股期最後交易日＝到期月份第三個星期三（不處理遇假日順延）。"""
    text = str(contract_month).replace("/", "").replace("-", "")
    year, month = int(text[:4]), int(text[4:6])
    first = date(year, month, 1)
    offset = (2 - first.weekday()) % 7  # Wednesday == 2
    return first + timedelta(days=offset + 14)


def _stop_price(trade, rule):
    sign = 1 if trade.side == "LONG" else -1
    if rule.stop_mode == "pct" and rule.stop_pct:
        return trade.avg_entry * (1 - sign * rule.stop_pct / 100)
    if rule.stop_mode == "plan" and trade.stop:
        return trade.stop
    return None


def _target_price(trade, rule, stop):
    sign = 1 if trade.side == "LONG" else -1
    if rule.take_profit_mode == "pct" and rule.take_profit_pct:
        return trade.avg_entry * (1 + sign * rule.take_profit_pct / 100)
    if rule.take_profit_mode == "r" and rule.take_profit_r:
        basis = stop if stop is not None else trade.stop
        if basis is None:
            return None
        distance = abs(trade.avg_entry - basis)
        if distance <= 0:
            return None
        return trade.avg_entry + sign * distance * rule.take_profit_r
    return None


def _result(trade, exit_date, exit_price, reason, bars_held):
    sign = 1 if trade.side == "LONG" else -1
    gross = (exit_price - trade.avg_entry) * sign * trade.qty * trade.contract_size
    tax = tj.ssf_tax(trade.avg_entry, trade.contract_size, trade.qty) + tj.ssf_tax(
        exit_price, trade.contract_size, trade.qty
    )
    # 手續費：口數與進出次數相同，沿用實單手續費
    commission = trade.commission
    return SimResult(
        exit_date=exit_date,
        exit_price=exit_price,
        reason=reason,
        gross_pnl=gross,
        commission=commission,
        tax=tax,
        net_pnl=gross - commission - tax,
        bars_held=bars_held,
    )


def simulate_exit(trade, bars, rule):
    """trade：ClosedTrade；bars：[{date, open, high, low, close, settlement}]（日盤、
    該合約月份、依日期排序）。沒有任何進場後 K 棒時回傳 None。"""
    expiry = third_wednesday(trade.contract_month).isoformat() if trade.contract_month else None
    after = [b for b in bars if b["date"] > trade.entry_date and (expiry is None or b["date"] <= expiry)]
    if not after:
        return None
    is_long = trade.side == "LONG"
    stop = _stop_price(trade, rule)
    target = _target_price(trade, rule, stop)
    trail_pct = rule.trailing_pct or 0
    extreme = None  # 進場後到前一根為止的最高（多）／最低（空）

    for index, bar in enumerate(after, start=1):
        o, h, l = bar["open"], bar["high"], bar["low"]
        c = bar["close"]
        if o is None or h is None or l is None or c is None:
            continue
        trail_stop = None
        if trail_pct and extreme is not None:
            trail_stop = extreme * (1 - trail_pct / 100) if is_long else extreme * (1 + trail_pct / 100)

        # 有效停損：固定停損與移動停損取比較「緊」的那個
        effective, reason = stop, EXIT_STOP
        if trail_stop is not None:
            if effective is None or (is_long and trail_stop > effective) or (not is_long and trail_stop < effective):
                effective, reason = trail_stop, EXIT_TRAIL

        if effective is not None:
            if is_long and o <= effective or not is_long and o >= effective:
                return _result(trade, bar["date"], o, reason, index)
            if is_long and l <= effective or not is_long and h >= effective:
                return _result(trade, bar["date"], effective, reason, index)
        if target is not None:
            if is_long and o >= target or not is_long and o <= target:
                return _result(trade, bar["date"], o, EXIT_TARGET, index)
            if is_long and h >= target or not is_long and l <= target:
                return _result(trade, bar["date"], target, EXIT_TARGET, index)
        if expiry is not None and bar["date"] == expiry:
            price = bar.get("settlement") or c
            return _result(trade, bar["date"], price, EXIT_EXPIRY, index)
        if rule.max_hold_days and index >= rule.max_hold_days:
            return _result(trade, bar["date"], c, EXIT_TIME, index)

        if is_long:
            extreme = h if extreme is None else max(extreme, h)
        else:
            extreme = l if extreme is None else min(extreme, l)

    # 合約還沒到期（或沒有月份）但 K 棒用完：今天還沒收盤、交易還在進行，或期交所缺資料
    last = after[-1]
    return _result(trade, last["date"], last["close"], EXIT_NO_DATA, len(after))


@dataclass(frozen=True)
class WhatIfRow:
    trade: ClosedTrade
    sim: SimResult | None
    note: str = ""

    @property
    def diff(self):
        return None if self.sim is None else self.sim.net_pnl - self.trade.net_pnl


@dataclass(frozen=True)
class WhatIfReport:
    rule: ExitRule
    rows: tuple[WhatIfRow, ...]
    actual: Stats
    simulated: Stats
    skipped: int = 0


def default_fetch_bars(db_path):
    """預設 K 棒來源：期交所個股期日 K（tradingnote_taifex，缺的日期會連網補）。"""
    import tradingnote_taifex as taifex

    def fetch(trade):
        if not trade.futures_code or not trade.contract_month:
            return []
        expiry = third_wednesday(trade.contract_month)
        end = min(expiry, date.today()).isoformat()
        taifex.ensure_futures_daily(db_path, trade.futures_code, trade.entry_date, end)
        return taifex.get_futures_bars(
            db_path, trade.futures_code, trade.contract_month, trade.entry_date, end
        )

    return fetch


def run_whatif(db_path, rule, *, fetch_bars=None, on_progress=None, **filters):
    """對篩選後的每筆已平倉 SSF 交易跑 simulate_exit，回傳實際 vs 模擬對照。
    抓不到 K 棒的交易不納入兩邊統計（skipped），確保比較的是同一批交易。"""
    fetch_bars = fetch_bars or default_fetch_bars(db_path)
    trades = closed_trade_results(db_path, **filters)
    rows = []
    for index, trade in enumerate(trades, start=1):
        note = ""
        sim = None
        try:
            bars = fetch_bars(trade)
            sim = simulate_exit(trade, bars, rule)
            if sim is None:
                note = "沒有進場後的期貨日 K" if trade.futures_code else "缺期貨代號／月份"
        except Exception as exc:  # 單筆抓資料失敗不擋整批
            note = f"抓取 K 棒失敗：{exc}"
        rows.append(WhatIfRow(trade=trade, sim=sim, note=note))
        if on_progress:
            on_progress(index, len(trades))
    usable = [row for row in rows if row.sim is not None]
    actual = compute_stats([row.trade for row in usable])
    simulated = compute_stats(
        [replace(row.trade, exit_date=row.sim.exit_date, net_pnl=row.sim.net_pnl) for row in usable]
    )
    return WhatIfReport(
        rule=rule,
        rows=tuple(rows),
        actual=actual,
        simulated=simulated,
        skipped=len(rows) - len(usable),
    )


# ---------------------------------------------------------------------------
# 通用 CSV 匯入
# ---------------------------------------------------------------------------

CSV_COLUMNS = (
    "日期", "標的代號", "合約", "合約月份", "買賣", "口數", "價格",
    "手續費", "期交稅", "策略", "停損價", "備註",
)
REQUIRED_COLUMNS = CSV_COLUMNS[:7]
CSV_TEMPLATE = (
    ",".join(CSV_COLUMNS)
    + "\n2026-10-01,2330,標準型,202611,買,2,1000,40,,突破,980,範例：多單進場\n"
    + "2026-10-05,2330,標準型,202611,賣,2,1015,40,,,,範例：多單出場\n"
)

_ACTION_ALIASES = {"買": "BUY", "B": "BUY", "BUY": "BUY", "賣": "SELL", "S": "SELL", "SELL": "SELL"}
_SIZE_ALIASES = {"標準型": 2000, "標準": 2000, "STD": 2000, "小型": 100, "小": 100, "MINI": 100}


@dataclass(frozen=True)
class CsvRow:
    line: int
    executed_at: str
    symbol: str
    contract_size: int
    contract_month: str
    action: str
    qty: float
    price: float
    commission: float
    tax: float | None
    strategy: str | None
    stop: float | None
    note: str | None
    source_key: str = ""


@dataclass
class ImportResult:
    added: int = 0
    skipped: int = 0
    trades_opened: int = 0
    trade_ids: list = field(default_factory=list)


def write_template(path):
    Path(path).write_text(CSV_TEMPLATE, encoding="utf-8-sig")


def _read_text(path):
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "cp950"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("無法辨識檔案編碼（請存成 UTF-8 或 Big5）。")


def _parse_date(text):
    """YYYY-MM-DD（可加 HH:MM[:SS]，斜線亦可）→ 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM:SS'。"""
    text = text.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).isoformat(sep=" ")
        except ValueError:
            continue
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValueError(f"日期格式不正確：{text}（應為 YYYY-MM-DD）")


def _parse_month(text):
    digits = text.strip().replace("/", "").replace("-", "")
    if len(digits) != 6 or not digits.isdigit() or not 1 <= int(digits[4:]) <= 12:
        raise ValueError(f"合約月份格式不正確：{text}（應為 YYYYMM）")
    return digits


def _optional_float(text, label):
    text = (text or "").strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{label}不是數字：{text}")


def parse_ssf_csv(path):
    """回傳 (rows, errors)。errors＝[(行號, 訊息)]；有錯誤時呼叫端不應匯入。"""
    reader = csv.DictReader(io.StringIO(_read_text(path)))
    headers = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        return [], [(1, f"缺少欄位：{'、'.join(missing)}")]
    rows, errors = [], []
    seen = {}
    for line, raw in enumerate(reader, start=2):
        record = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if not any(record.values()):
            continue
        try:
            symbol = record["標的代號"].upper()
            if not TICKER_RE.match(symbol):
                raise ValueError(f"標的代號格式不正確：{record['標的代號']}（填股票代號，如 2330）")
            size_text = record["合約"].upper() if record["合約"].isascii() else record["合約"]
            if size_text not in _SIZE_ALIASES:
                raise ValueError(f"合約應為「標準型」或「小型」：{record['合約']}")
            action = _ACTION_ALIASES.get(record["買賣"].upper())
            if action is None:
                raise ValueError(f"買賣應為「買」或「賣」：{record['買賣']}")
            qty = _optional_float(record["口數"], "口數")
            price = _optional_float(record["價格"], "價格")
            if not qty or qty <= 0:
                raise ValueError("口數必須大於 0")
            if not price or price <= 0:
                raise ValueError("價格必須大於 0")
            row = CsvRow(
                line=line,
                executed_at=_parse_date(record["日期"]),
                symbol=symbol,
                contract_size=_SIZE_ALIASES[size_text],
                contract_month=_parse_month(record["合約月份"]),
                action=action,
                qty=qty,
                price=price,
                commission=_optional_float(record.get("手續費"), "手續費") or 0.0,
                tax=_optional_float(record.get("期交稅"), "期交稅"),
                strategy=record.get("策略") or None,
                stop=_optional_float(record.get("停損價"), "停損價"),
                note=record.get("備註") or None,
            )
        except ValueError as exc:
            errors.append((line, str(exc)))
            continue
        base = "|".join(
            str(v) for v in (row.executed_at, row.symbol, row.contract_size,
                             row.contract_month, row.action, row.qty, row.price)
        )
        occurrence = seen.get(base, 0)
        seen[base] = occurrence + 1
        rows.append(replace(row, source_key=f"csv:{base}|{occurrence}"))
    rows.sort(key=lambda r: (r.executed_at, r.line))
    return rows, errors


def _strategy_id(db_path, name, cache):
    if not name:
        return None
    if name not in cache:
        existing = {s.name: s.strategy_id for s in tj.list_strategies(db_path)}
        cache.update(existing)
        if name not in cache:
            cache[name] = tj.create_strategy(db_path, name)
    return cache[name]


def import_ssf_rows(db_path, rows, code_lookup=None):
    """把 parse_ssf_csv 的結果寫成交易：依（標的, 合約型, 月份）配對，空手開新倉、
    同向加碼、反向減碼，反手時拆成平倉＋新倉。已匯入過的列（source_key 相同）跳過，
    既有未平倉的 SSF 交易會接續使用。code_lookup(stock_id, contract_size) → 期貨代號
    （GUI 傳期交所清單查詢；查詢失敗或不傳時代號留空，What-if 會跳過該筆）。"""
    result = ImportResult()
    existing_keys = tj.existing_source_keys(db_path, [r.source_key for r in rows])
    state = {}
    for trade in tj.list_trades(db_path, status="OPEN", instrument=tj.INSTRUMENT_SSF):
        if trade.total_qty > 0:
            key = (trade.symbol, int(trade.contract_size), trade.contract_month)
            state[key] = [trade.trade_id, trade.side, trade.total_qty]
    strategies = {}
    code_cache = {}

    for row in rows:
        if row.source_key in existing_keys:
            result.skipped += 1
            continue
        key = (row.symbol, row.contract_size, row.contract_month)
        remaining = row.qty
        current = state.get(key)
        if current is not None:
            trade_id, side, open_qty = current
            entry_action = "BUY" if side == "LONG" else "SELL"
            if row.action == entry_action:
                tj.record_execution(
                    db_path, trade_id, action=row.action, price=row.price, qty=row.qty,
                    commission=row.commission, tax=row.tax, executed_at=row.executed_at,
                    source_key=row.source_key,
                )
                current[2] = open_qty + row.qty
                remaining = 0
            else:
                close_qty = min(open_qty, row.qty)
                share = close_qty / row.qty
                tj.record_execution(
                    db_path, trade_id, action=row.action, price=row.price, qty=close_qty,
                    commission=row.commission * share,
                    tax=None if row.tax is None else row.tax * share,
                    executed_at=row.executed_at, source_key=row.source_key,
                )
                remaining = row.qty - close_qty
                if open_qty - close_qty <= 1e-9:
                    state.pop(key)
                else:
                    current[2] = open_qty - close_qty
        if remaining > 1e-9:
            share = remaining / row.qty
            side = "LONG" if row.action == "BUY" else "SHORT"
            code_key = (row.symbol, row.contract_size)
            if code_key not in code_cache:
                try:
                    code = code_lookup(row.symbol, row.contract_size) if code_lookup else None
                except Exception:  # noqa: BLE001 - 查不到代號不擋匯入
                    code = None
                code_cache[code_key] = code or None
            trade_id = tj.open_trade(
                db_path,
                symbol=row.symbol,
                side=side,
                initial_stop_loss=row.stop or 0,
                strategy_id=_strategy_id(db_path, row.strategy, strategies),
                initial_entry_price=row.price,
                opened_at=row.executed_at,
                notes=row.note,
                instrument=tj.INSTRUMENT_SSF,
                contract_size=row.contract_size,
                futures_code=code_cache[code_key],
                contract_month=row.contract_month,
            )
            # 反手的那筆成交拆成兩段時，第二段 source_key 加後綴避免與第一段相同
            source_key = row.source_key if share == 1 else f"{row.source_key}|open"
            tj.record_execution(
                db_path, trade_id, action=row.action, price=row.price, qty=remaining,
                commission=row.commission * share,
                tax=None if row.tax is None else row.tax * share,
                executed_at=row.executed_at, source_key=source_key,
            )
            state[key] = [trade_id, side, remaining]
            result.trades_opened += 1
            result.trade_ids.append(trade_id)
        result.added += 1
    return result
