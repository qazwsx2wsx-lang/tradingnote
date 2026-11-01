"""通用回測框架：把 daily_prices 累積的歷史收盤價餵給可抽換的策略，逐日模擬
買賣，結果（權益曲線、成交紀錄、績效摘要）寫回同一個 history.db，讓之後要接的
策略（選股規則／技術指標／法人籌碼）共用同一套引擎，外部平台也能直接用 SQL
讀取結果，不需要跑 Python。

目前刻意保留、沒有隱藏的限制：
- daily_prices 只存收盤價，策略當天判斷買賣，成交也在「同一天」的收盤價，
  等於有一點先天的未來函數；之後要修正可以加 fill_lag_bars 讓成交延後 N 個
  交易日。
- 一次 run_backtest 裡的多檔股票是互相獨立的模擬，各自有一份 initial_capital，
  不是共用資金池的投資組合模式。
- 同一檔股票同一時間只有「空手」或「用 Signal.size_fraction 指定比例的現金進場」
  兩種狀態，沒有分批加碼、沒有放空；size_fraction 預設 1.0（全倉）。
- 資金不夠買一張（零股）時直接跳過那次訊號，不會額外記錄一筆「訊號被忽略」。
- daily_prices 只存收盤價，沒有逐日最高最低價；需要 KD 之類指標的策略只能拿
  收盤價近似「高」「低」，不是真正的日內 KD（見 kd_bottom_volume_breakout）。
"""

import json
import math
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from tradingnote_technical import TechnicalPriceBar, _simple_moving_average, _calculate_kd

ACTION_BUY = "BUY"
ACTION_SELL = "SELL"
ACTION_HOLD = "HOLD"

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class Signal:
    action: str
    size_fraction: float = 1.0  # BUY 時用多少比例的現金進場；SELL/HOLD 不理會這個欄位


@dataclass(frozen=True)
class StrategyContext:
    """餵給策略決策函式的唯一輸入。bars 由引擎截到「今天」為止（bars[-1] 就是
    今天），策略拿不到今天以後的資料，避免偷看未來——這是引擎結構上的保證，
    不只是文件上的約定。"""

    db_path: str
    ticker: str
    bars: tuple  # tuple[TechnicalPriceBar, ...]，依日期升冪排序
    cash: float
    shares: float
    avg_cost: float | None


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    description: str
    build: Callable[[dict], tuple]  # raw_params -> (decide_fn, resolved_params)


@dataclass
class _EquityRow:
    date: str
    cash: float
    shares: float
    close: float
    position_value: float
    equity: float


@dataclass
class _TradeRow:
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    shares: float
    gross_pnl: float
    commission_total: float
    net_pnl: float
    return_pct: float
    holding_days: int
    exit_reason: str


@dataclass
class _SummaryRow:
    initial_capital: float
    final_equity: float
    first_trading_date: str
    last_trading_date: str
    total_return_pct: float
    cagr_pct: float | None
    max_drawdown_pct: float
    num_trades: int
    win_rate_pct: float | None
    profit_factor: float | None
    avg_holding_days: float | None
    avg_win_pct: float | None
    avg_loss_pct: float | None
    sharpe_ratio: float | None


# ---------- 策略（要加新策略就在這裡加一個 build 函式，再放進 STRATEGIES） ----------

def _build_buy_and_hold(raw_params):
    if raw_params:
        raise ValueError(f"buy_and_hold 不接受參數：{sorted(raw_params)}")

    def decide(context):
        return Signal(ACTION_HOLD if context.shares else ACTION_BUY)

    return decide, {}


def _build_sma_crossover(raw_params):
    params = dict(raw_params)
    fast = int(params.pop("fast", 5))
    slow = int(params.pop("slow", 20))
    if params:
        raise ValueError(f"sma_crossover 不認得的參數：{sorted(params)}")
    if fast < 1:
        raise ValueError("fast 必須 >= 1")
    if slow <= fast:
        raise ValueError("slow 必須大於 fast")

    def decide(context):
        closes = [bar.close for bar in context.bars]
        if len(closes) < slow + 1:
            return Signal(ACTION_HOLD)
        fast_ma = _simple_moving_average(closes, fast)
        slow_ma = _simple_moving_average(closes, slow)
        today = len(closes) - 1
        values = (fast_ma[today], slow_ma[today], fast_ma[today - 1], slow_ma[today - 1])
        if any(value is None for value in values):
            return Signal(ACTION_HOLD)
        fast_today, slow_today, fast_prev, slow_prev = values
        crossed_up = fast_prev <= slow_prev and fast_today > slow_today
        crossed_down = fast_prev >= slow_prev and fast_today < slow_today
        if crossed_up and not context.shares:
            return Signal(ACTION_BUY)
        if crossed_down and context.shares:
            return Signal(ACTION_SELL)
        return Signal(ACTION_HOLD)

    return decide, {"fast": fast, "slow": slow}


def _kd_from_closes(bars, period):
    """daily_prices 沒有存逐日最高／最低價，只能拿收盤價近似：把每天的「高」
    「低」都當作收盤價去跑 _calculate_kd 的 RSV 公式，等於用「區間內最高／
    最低收盤價」取代「區間內最高／最低價」。跟真正的日內 KD 不同，但這是
    目前累積資料能算出的最接近版本，策略說明裡也會註明這個近似。"""
    synthetic = tuple(
        TechnicalPriceBar(date=bar.date, open=None, high=bar.close, low=bar.close,
                           close=bar.close, volume=bar.volume, trading_value=bar.trading_value)
        for bar in bars
    )
    return _calculate_kd(synthetic, period)


def _build_kd_bottom_volume_breakout(raw_params):
    params = dict(raw_params)
    kd_period = int(params.pop("kd_period", 9))
    kd_threshold = float(params.pop("kd_threshold", 20.0))
    volume_ratio_threshold = float(params.pop("volume_ratio_threshold", 1.5))
    entry_fraction = float(params.pop("entry_fraction", 0.8))
    breakout_lookback = int(params.pop("breakout_lookback", 20))
    stop_loss_pct = float(params.pop("stop_loss_pct", 15.0))
    ma_exit_period = int(params.pop("ma_exit_period", 10))
    if params:
        raise ValueError(f"kd_bottom_volume_breakout 不認得的參數：{sorted(params)}")
    if kd_period < 2:
        raise ValueError("kd_period 必須 >= 2")
    if not 0 < entry_fraction <= 1:
        raise ValueError("entry_fraction 必須在 (0, 1] 之間")
    if breakout_lookback < 1:
        raise ValueError("breakout_lookback 必須 >= 1")
    if stop_loss_pct <= 0:
        raise ValueError("stop_loss_pct 必須 > 0")
    if volume_ratio_threshold <= 0:
        raise ValueError("volume_ratio_threshold 必須 > 0")
    if ma_exit_period < 1:
        raise ValueError("ma_exit_period 必須 >= 1")

    # entry_breakout 記住「進場前 breakout_lookback 日的收盤最高點」，只在這一
    # 筆倉位存續期間有意義；run_backtest 對每檔股票都會重新呼叫一次 build()，
    # 所以這個 closure 狀態不會在多檔股票的同一次 run 裡互相汙染。
    entry_breakout = {"level": None}

    def decide(context):
        bars = context.bars
        today = len(bars) - 1
        if today < kd_period:
            return Signal(ACTION_HOLD)
        closes = [bar.close for bar in bars]
        volumes = [bar.volume for bar in bars]

        if context.shares == 0:
            # 沒有倉位，任何殘留的前高都是上一筆（可能進場失敗的）舊狀態，先清掉。
            entry_breakout["level"] = None
            if today < 1:
                return Signal(ACTION_HOLD)
            kd = _kd_from_closes(bars, kd_period)
            k, d = kd["k"], kd["d"]
            if None in (k[today], d[today], k[today - 1], d[today - 1]):
                return Signal(ACTION_HOLD)
            golden_cross = k[today - 1] <= d[today - 1] and k[today] > d[today]
            was_at_bottom = k[today - 1] < kd_threshold and d[today - 1] < kd_threshold
            prev_volume = volumes[today - 1]
            volume_ratio = (volumes[today] / prev_volume) if prev_volume else None
            volume_surge = volume_ratio is not None and volume_ratio > volume_ratio_threshold
            if golden_cross and was_at_bottom and volume_surge:
                window = closes[max(0, today - breakout_lookback):today]
                entry_breakout["level"] = max(window) if window else None
                return Signal(ACTION_BUY, size_fraction=entry_fraction)
            return Signal(ACTION_HOLD)

        current_close = closes[today]
        stop_price = context.avg_cost * (1 - stop_loss_pct / 100) if context.avg_cost else None
        if stop_price is not None and current_close <= stop_price:
            entry_breakout["level"] = None
            return Signal(ACTION_SELL)
        breakout_level = entry_breakout["level"]
        if breakout_level is not None and current_close > breakout_level:
            entry_breakout["level"] = None
            return Signal(ACTION_SELL)
        ma_values = _simple_moving_average(closes, ma_exit_period)
        if ma_values[today] is not None and current_close < ma_values[today]:
            entry_breakout["level"] = None
            return Signal(ACTION_SELL)
        return Signal(ACTION_HOLD)

    resolved = {
        "kd_period": kd_period,
        "kd_threshold": kd_threshold,
        "volume_ratio_threshold": volume_ratio_threshold,
        "entry_fraction": entry_fraction,
        "breakout_lookback": breakout_lookback,
        "stop_loss_pct": stop_loss_pct,
        "ma_exit_period": ma_exit_period,
    }
    return decide, resolved


STRATEGIES = (
    Strategy("buy_and_hold", "買進持有",
             "第一天買進後永久持有，用來驗證引擎本身的機制（成交、權益曲線、強制平倉）。",
             _build_buy_and_hold),
    Strategy("sma_crossover", "均線交叉",
             "快線向上穿越慢線買進，向下穿越賣出；只做多，同一時間最多一筆倉位。",
             _build_sma_crossover),
    Strategy(
        "kd_bottom_volume_breakout", "KD 落底黃金交叉＋量增突破",
        "KD 在低檔（K、D 皆低於 kd_threshold）黃金交叉，且當日成交量較前一日放大超過 "
        "volume_ratio_threshold 倍時，用 entry_fraction 比例的現金進場；三個出場條件任一成立就 "
        "全部出清：收盤站上進場前 breakout_lookback 日內的收盤最高點（獲利出清）、收盤跌破進場價 "
        "stop_loss_pct%（停損），或收盤跌破 ma_exit_period 日均線（趨勢轉弱出清）。daily_prices "
        "沒有存逐日最高最低價，KD 用收盤價近似，不是真正的日內 KD。",
        _build_kd_bottom_volume_breakout,
    ),
)
STRATEGIES_BY_KEY = {strategy.key: strategy for strategy in STRATEGIES}


# ---------- DB ----------

def _connect(db_path):
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS backtest_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_key TEXT NOT NULL,
            strategy_label TEXT NOT NULL,
            params_json TEXT NOT NULL DEFAULT '{}',
            tickers_json TEXT NOT NULL,
            requested_start_date TEXT NOT NULL,
            requested_end_date TEXT NOT NULL,
            initial_capital REAL NOT NULL,
            commission_bps REAL NOT NULL DEFAULT 0.0,
            slippage_bps REAL NOT NULL DEFAULT 0.0,
            risk_free_rate_annual REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL CHECK(status IN ('RUNNING', 'DONE', 'FAILED')) DEFAULT 'RUNNING',
            error_message TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy
            ON backtest_runs (strategy_key, created_at DESC);

        CREATE TABLE IF NOT EXISTS backtest_equity_curve (
            run_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            cash REAL NOT NULL,
            shares REAL NOT NULL,
            close REAL NOT NULL,
            position_value REAL NOT NULL,
            equity REAL NOT NULL,
            PRIMARY KEY (run_id, ticker, date),
            FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS backtest_trades (
            trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            entry_date TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_date TEXT NOT NULL,
            exit_price REAL NOT NULL,
            shares REAL NOT NULL,
            gross_pnl REAL NOT NULL,
            commission_total REAL NOT NULL DEFAULT 0.0,
            net_pnl REAL NOT NULL,
            return_pct REAL NOT NULL,
            holding_days INTEGER NOT NULL,
            exit_reason TEXT NOT NULL CHECK(exit_reason IN ('SIGNAL', 'END_OF_RANGE')),
            FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_backtest_trades_run_ticker
            ON backtest_trades (run_id, ticker);

        CREATE TABLE IF NOT EXISTS backtest_summary (
            run_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            initial_capital REAL NOT NULL,
            final_equity REAL NOT NULL,
            first_trading_date TEXT NOT NULL,
            last_trading_date TEXT NOT NULL,
            total_return_pct REAL NOT NULL,
            cagr_pct REAL,
            max_drawdown_pct REAL NOT NULL,
            num_trades INTEGER NOT NULL,
            win_rate_pct REAL,
            profit_factor REAL,
            avg_holding_days REAL,
            avg_win_pct REAL,
            avg_loss_pct REAL,
            sharpe_ratio REAL,
            PRIMARY KEY (run_id, ticker),
            FOREIGN KEY (run_id) REFERENCES backtest_runs(run_id) ON DELETE CASCADE
        );
        """
    )
    conn.commit()
    return conn


def _load_price_bars(conn, ticker, start_date, end_date):
    """daily_prices 依日期升冪的收盤價序列（get_ticker_history 是 DESC 排序、
    缺 market/name，不適合這裡要的「依序逐日模擬」，所以另開一個查詢）。"""
    rows = conn.execute(
        "SELECT date, close, volume, trading_value FROM daily_prices "
        "WHERE ticker = ? AND date BETWEEN ? AND ? AND close IS NOT NULL "
        "ORDER BY date ASC",
        (ticker, start_date, end_date),
    ).fetchall()
    return [
        TechnicalPriceBar(date=row[0], open=None, high=None, low=None,
                           close=row[1], volume=row[2] or 0.0, trading_value=row[3])
        for row in rows
    ]


# ---------- 模擬（純函式，不碰 DB，方便直接餵合成資料做單元測試） ----------

def _close_trade(open_trade, exit_date, exit_price, exit_commission, exit_bar_index, exit_reason):
    shares = open_trade["entry_shares"]
    entry_price = open_trade["entry_price"]
    gross_pnl = (exit_price - entry_price) * shares
    commission_total = open_trade["entry_commission"] + exit_commission
    net_pnl = gross_pnl - commission_total
    cost_basis = entry_price * shares
    return_pct = (net_pnl / cost_basis * 100) if cost_basis else 0.0
    holding_days = exit_bar_index - open_trade["entry_bar_index"]
    return _TradeRow(
        entry_date=open_trade["entry_date"], entry_price=entry_price,
        exit_date=exit_date, exit_price=exit_price, shares=shares,
        gross_pnl=gross_pnl, commission_total=commission_total, net_pnl=net_pnl,
        return_pct=return_pct, holding_days=holding_days, exit_reason=exit_reason,
    )


def _simulate_ticker(decide_fn, bars, initial_capital, commission_bps=0.0, slippage_bps=0.0,
                      db_path=None, ticker=None):
    cash = initial_capital
    shares = 0.0
    avg_cost = None
    open_trade = None
    equity_rows = []
    trade_rows = []

    for index, bar in enumerate(bars):
        context = StrategyContext(db_path, ticker, tuple(bars[: index + 1]), cash, shares, avg_cost)
        signal = decide_fn(context)

        if signal.action == ACTION_BUY and shares == 0:
            fraction = max(0.0, min(1.0, signal.size_fraction))
            fill_price = bar.close * (1 + slippage_bps / 10000)
            cost_per_share = fill_price * (1 + commission_bps / 10000)
            shares_to_buy = math.floor(cash * fraction / cost_per_share) if cost_per_share else 0
            if shares_to_buy >= 1:
                commission = shares_to_buy * fill_price * commission_bps / 10000
                cash -= shares_to_buy * fill_price + commission
                shares = float(shares_to_buy)
                avg_cost = fill_price
                open_trade = {
                    "entry_date": bar.date,
                    "entry_price": fill_price,
                    "entry_shares": shares,
                    "entry_bar_index": index,
                    "entry_commission": commission,
                }
        elif signal.action == ACTION_SELL and shares > 0:
            fill_price = bar.close * (1 - slippage_bps / 10000)
            proceeds = shares * fill_price
            exit_commission = proceeds * commission_bps / 10000
            cash += proceeds - exit_commission
            trade_rows.append(
                _close_trade(open_trade, bar.date, fill_price, exit_commission, index, "SIGNAL")
            )
            shares = 0.0
            avg_cost = None
            open_trade = None

        position_value = shares * bar.close
        equity_rows.append(
            _EquityRow(bar.date, cash, shares, bar.close, position_value, cash + position_value)
        )

    if shares > 0 and bars:
        last = bars[-1]
        fill_price = last.close * (1 - slippage_bps / 10000)
        proceeds = shares * fill_price
        exit_commission = proceeds * commission_bps / 10000
        cash += proceeds - exit_commission
        trade_rows.append(
            _close_trade(open_trade, last.date, fill_price, exit_commission, len(bars) - 1,
                          "END_OF_RANGE")
        )
        equity_rows[-1] = _EquityRow(last.date, cash, 0.0, last.close, 0.0, cash)

    return equity_rows, trade_rows


# ---------- 績效指標（純函式） ----------

def _compute_max_drawdown(equity_values):
    peak = equity_values[0]
    max_drawdown = 0.0
    for value in equity_values:
        peak = max(peak, value)
        if peak:
            max_drawdown = max(max_drawdown, (peak - value) / peak * 100)
    return max_drawdown


def _compute_cagr(initial_capital, final_equity, first_date, last_date):
    if initial_capital <= 0 or final_equity <= 0:
        return None
    start = datetime.strptime(first_date, "%Y-%m-%d").date()
    end = datetime.strptime(last_date, "%Y-%m-%d").date()
    days = (end - start).days
    if days <= 0:
        return None
    years = days / 365.25
    return ((final_equity / initial_capital) ** (1 / years) - 1) * 100


def _compute_sharpe(equity_values, risk_free_rate_annual):
    if len(equity_values) < 2:
        return None
    returns = [curr / prev - 1 for prev, curr in zip(equity_values, equity_values[1:]) if prev]
    if len(returns) < 2:
        return None
    daily_rf = (1 + risk_free_rate_annual) ** (1 / TRADING_DAYS_PER_YEAR) - 1
    excess = [r - daily_rf for r in returns]
    stdev = statistics.pstdev(excess)
    if not stdev:
        return None
    return statistics.mean(excess) / stdev * math.sqrt(TRADING_DAYS_PER_YEAR)


def compute_summary_metrics(initial_capital, equity_rows, trade_rows, risk_free_rate_annual=0.0):
    if not equity_rows:
        raise ValueError("equity_rows 不能是空的")

    final_equity = equity_rows[-1].equity
    first_date = equity_rows[0].date
    last_date = equity_rows[-1].date
    total_return_pct = (final_equity / initial_capital - 1) * 100 if initial_capital else 0.0

    wins = [trade for trade in trade_rows if trade.net_pnl > 0]
    losses = [trade for trade in trade_rows if trade.net_pnl < 0]
    num_trades = len(trade_rows)

    if not losses:
        profit_factor = None  # 沒有輸的交易，比率沒有意義（分母為零）
    elif not wins:
        profit_factor = 0.0
    else:
        profit_factor = sum(t.net_pnl for t in wins) / abs(sum(t.net_pnl for t in losses))

    return _SummaryRow(
        initial_capital=initial_capital,
        final_equity=final_equity,
        first_trading_date=first_date,
        last_trading_date=last_date,
        total_return_pct=total_return_pct,
        cagr_pct=_compute_cagr(initial_capital, final_equity, first_date, last_date),
        max_drawdown_pct=_compute_max_drawdown([row.equity for row in equity_rows]),
        num_trades=num_trades,
        win_rate_pct=(len(wins) / num_trades * 100) if num_trades else None,
        profit_factor=profit_factor,
        avg_holding_days=(sum(t.holding_days for t in trade_rows) / num_trades) if num_trades else None,
        avg_win_pct=(sum(t.return_pct for t in wins) / len(wins)) if wins else None,
        avg_loss_pct=(sum(t.return_pct for t in losses) / len(losses)) if losses else None,
        sharpe_ratio=_compute_sharpe([row.equity for row in equity_rows], risk_free_rate_annual),
    )


# ---------- 執行＋存檔 ----------

def run_backtest(db_path, *, strategy_key, params, tickers, start_date, end_date,
                  initial_capital, commission_bps=0.0, slippage_bps=0.0,
                  risk_free_rate_annual=0.0):
    if strategy_key not in STRATEGIES_BY_KEY:
        raise ValueError(f"未知的策略：{strategy_key}")
    strategy = STRATEGIES_BY_KEY[strategy_key]
    _, resolved_params = strategy.build(dict(params or {}))  # 先驗證參數、取得要存檔的 resolved params

    conn = _connect(db_path)
    try:
        created_at = datetime.now().isoformat(timespec="seconds")
        cursor = conn.execute(
            "INSERT INTO backtest_runs (strategy_key, strategy_label, params_json, tickers_json, "
            "requested_start_date, requested_end_date, initial_capital, commission_bps, "
            "slippage_bps, risk_free_rate_annual, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?)",
            (strategy.key, strategy.label, json.dumps(resolved_params), json.dumps(list(tickers)),
             start_date, end_date, initial_capital, commission_bps, slippage_bps,
             risk_free_rate_annual, created_at),
        )
        run_id = cursor.lastrowid
        conn.commit()

        errors = []
        succeeded = 0
        for ticker in tickers:
            try:
                # 每檔股票重新 build() 一次：策略若像 kd_bottom_volume_breakout 一樣用
                # closure 記住「進場前的前高」這種跨 bar 狀態，不會在多檔股票的同一次
                # run 裡互相汙染。
                decide_fn, _ = strategy.build(dict(params or {}))
                bars = _load_price_bars(conn, ticker, start_date, end_date)
                if not bars:
                    errors.append(f"{ticker}：指定期間內查無價格資料")
                    continue
                equity_rows, trade_rows = _simulate_ticker(
                    decide_fn, bars, initial_capital, commission_bps, slippage_bps,
                    db_path=db_path, ticker=ticker,
                )
                summary = compute_summary_metrics(initial_capital, equity_rows, trade_rows,
                                                    risk_free_rate_annual)
            except Exception as exc:  # noqa: BLE001 - 單一股票失敗不該讓整個 run 掛掉
                errors.append(f"{ticker}：{exc}")
                continue

            conn.executemany(
                "INSERT INTO backtest_equity_curve (run_id, ticker, date, cash, shares, close, "
                "position_value, equity) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, ticker, row.date, row.cash, row.shares, row.close, row.position_value,
                  row.equity) for row in equity_rows],
            )
            conn.executemany(
                "INSERT INTO backtest_trades (run_id, ticker, entry_date, entry_price, exit_date, "
                "exit_price, shares, gross_pnl, commission_total, net_pnl, return_pct, "
                "holding_days, exit_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, ticker, t.entry_date, t.entry_price, t.exit_date, t.exit_price, t.shares,
                  t.gross_pnl, t.commission_total, t.net_pnl, t.return_pct, t.holding_days,
                  t.exit_reason) for t in trade_rows],
            )
            conn.execute(
                "INSERT INTO backtest_summary (run_id, ticker, initial_capital, final_equity, "
                "first_trading_date, last_trading_date, total_return_pct, cagr_pct, "
                "max_drawdown_pct, num_trades, win_rate_pct, profit_factor, avg_holding_days, "
                "avg_win_pct, avg_loss_pct, sharpe_ratio) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, ticker, summary.initial_capital, summary.final_equity,
                 summary.first_trading_date, summary.last_trading_date, summary.total_return_pct,
                 summary.cagr_pct, summary.max_drawdown_pct, summary.num_trades,
                 summary.win_rate_pct, summary.profit_factor, summary.avg_holding_days,
                 summary.avg_win_pct, summary.avg_loss_pct, summary.sharpe_ratio),
            )
            succeeded += 1

        if succeeded:
            status, error_message = "DONE", ("; ".join(errors) if errors else None)
        else:
            status, error_message = "FAILED", ("; ".join(errors) if errors else "沒有任何股票成功模擬")
        conn.execute(
            "UPDATE backtest_runs SET status = ?, error_message = ?, finished_at = ? WHERE run_id = ?",
            (status, error_message, datetime.now().isoformat(timespec="seconds"), run_id),
        )
        conn.commit()
        return run_id
    finally:
        conn.close()


# ---------- 查詢（給 CLI／GUI／之後的外部平台用） ----------

def get_run(db_path, run_id):
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_runs(db_path, limit=50):
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_equity_curve(db_path, run_id, ticker):
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM backtest_equity_curve WHERE run_id = ? AND ticker = ? ORDER BY date ASC",
            (run_id, ticker),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_trades(db_path, run_id, ticker):
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM backtest_trades WHERE run_id = ? AND ticker = ? ORDER BY entry_date ASC",
            (run_id, ticker),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_summary(db_path, run_id, ticker=None):
    conn = _connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if ticker is not None:
            row = conn.execute(
                "SELECT * FROM backtest_summary WHERE run_id = ? AND ticker = ?", (run_id, ticker)
            ).fetchone()
            return dict(row) if row else None
        rows = conn.execute("SELECT * FROM backtest_summary WHERE run_id = ?", (run_id,)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
