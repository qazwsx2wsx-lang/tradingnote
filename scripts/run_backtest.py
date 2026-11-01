#!/usr/bin/env python3
"""對一檔或幾檔指定股票跑回測，結果寫進 history.db 的 backtest_* 資料表。

    python scripts/run_backtest.py --strategy buy_and_hold --ticker 2330 \
        --start 2023-01-01 --end 2026-10-31

    python scripts/run_backtest.py --strategy sma_crossover --ticker 2330 --ticker 2317 \
        --start 2023-01-01 --end 2026-10-31 --capital 1000000 \
        --param fast=5 --param slow=20 --commission-bps 14.25

    python scripts/run_backtest.py --strategy kd_bottom_volume_breakout --ticker 2330 \
        --start 2023-01-01 --end 2026-10-31 --capital 1000000 \
        --param kd_threshold=20 --param volume_ratio_threshold=1.5 \
        --param entry_fraction=0.8 --param breakout_lookback=20 --param stop_loss_pct=15 \
        --param ma_exit_period=10

策略清單：
  buy_and_hold（無參數）
  sma_crossover（fast、slow，預設 5／20）
  kd_bottom_volume_breakout（kd_period 預設 9、kd_threshold 預設 20、
    volume_ratio_threshold 預設 1.5、entry_fraction 預設 0.8、
    breakout_lookback 預設 20、stop_loss_pct 預設 15、ma_exit_period 預設 10）
"""

import argparse

import _bootstrap  # noqa: F401
import tradingnote_backtest as backtest
from tradingnote_paths import APP_PATHS


def _parse_param(item):
    key, _, raw = item.partition("=")
    for cast in (int, float):
        try:
            return key, cast(raw)
        except ValueError:
            continue
    return key, raw


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategy", required=True, choices=sorted(backtest.STRATEGIES_BY_KEY))
    parser.add_argument("--ticker", action="append", required=True, dest="tickers")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--commission-bps", type=float, default=0.0)
    parser.add_argument("--slippage-bps", type=float, default=0.0)
    parser.add_argument("--risk-free-rate", type=float, default=0.0)
    parser.add_argument("--param", action="append", default=[], metavar="key=value")
    args = parser.parse_args()

    params = dict(_parse_param(item) for item in args.param)
    run_id = backtest.run_backtest(
        APP_PATHS.history_db,
        strategy_key=args.strategy,
        params=params,
        tickers=args.tickers,
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
        commission_bps=args.commission_bps,
        slippage_bps=args.slippage_bps,
        risk_free_rate_annual=args.risk_free_rate,
    )

    run = backtest.get_run(APP_PATHS.history_db, run_id)
    print(f"run_id={run_id} status={run['status']}" + (f" ({run['error_message']})" if run["error_message"] else ""))
    for ticker in args.tickers:
        summary = backtest.get_summary(APP_PATHS.history_db, run_id, ticker)
        if summary is None:
            print(f"  {ticker}: 無結果")
            continue
        print(
            f"  {ticker}: 總報酬 {summary['total_return_pct']:.2f}%  "
            f"最大回撤 {summary['max_drawdown_pct']:.2f}%  "
            f"交易次數 {summary['num_trades']}  "
            f"勝率 {summary['win_rate_pct']}  "
            f"CAGR {summary['cagr_pct']}  "
            f"Sharpe {summary['sharpe_ratio']}"
        )


if __name__ == "__main__":
    main()
