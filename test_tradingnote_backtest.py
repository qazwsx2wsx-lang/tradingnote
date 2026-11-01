import tempfile
import unittest
from pathlib import Path

import tradingnote_backtest as backtest
import tradingnote_history as history
from tradingnote_backtest import ACTION_HOLD, Signal, TechnicalPriceBar


def _bars(closes, start="2026-01-01"):
    from datetime import date, timedelta
    base = date.fromisoformat(start)
    return [
        TechnicalPriceBar(date=(base + timedelta(days=i)).isoformat(), open=None, high=None,
                           low=None, close=close, volume=1000.0, trading_value=close * 1000)
        for i, close in enumerate(closes)
    ]


def _bars_with_volume(closes, volumes, start="2026-01-01"):
    from datetime import date, timedelta
    base = date.fromisoformat(start)
    return [
        TechnicalPriceBar(date=(base + timedelta(days=i)).isoformat(), open=None, high=None,
                           low=None, close=close, volume=volume, trading_value=close * volume)
        for i, (close, volume) in enumerate(zip(closes, volumes))
    ]


class BuyAndHoldSimulationTests(unittest.TestCase):
    def test_equity_curve_and_forced_close(self):
        decide_fn, params = backtest._build_buy_and_hold({})
        self.assertEqual(params, {})
        bars = _bars([100, 110, 90, 95, 120])

        equity_rows, trade_rows = backtest._simulate_ticker(decide_fn, bars, initial_capital=1000)

        self.assertEqual([round(r.equity, 4) for r in equity_rows], [1000, 1100, 900, 950, 1200])
        self.assertEqual(len(trade_rows), 1)
        trade = trade_rows[0]
        self.assertEqual(trade.entry_price, 100)
        self.assertEqual(trade.exit_price, 120)
        self.assertEqual(trade.shares, 10)
        self.assertEqual(trade.gross_pnl, 200)
        self.assertEqual(trade.net_pnl, 200)
        self.assertAlmostEqual(trade.return_pct, 20.0)
        self.assertEqual(trade.holding_days, 4)
        self.assertEqual(trade.exit_reason, "END_OF_RANGE")

        summary = backtest.compute_summary_metrics(1000, equity_rows, trade_rows)
        self.assertAlmostEqual(summary.total_return_pct, 20.0)
        self.assertAlmostEqual(summary.max_drawdown_pct, 18.181818, places=4)
        self.assertEqual(summary.num_trades, 1)
        self.assertEqual(summary.win_rate_pct, 100.0)
        self.assertIsNone(summary.profit_factor)
        self.assertEqual(summary.avg_holding_days, 4)

    def test_rejects_unknown_params(self):
        with self.assertRaises(ValueError):
            backtest._build_buy_and_hold({"foo": 1})

    def test_insufficient_capital_never_trades(self):
        decide_fn, _ = backtest._build_buy_and_hold({})
        bars = _bars([100, 110, 120])
        equity_rows, trade_rows = backtest._simulate_ticker(decide_fn, bars, initial_capital=50)
        self.assertEqual(trade_rows, [])
        self.assertEqual([r.equity for r in equity_rows], [50, 50, 50])
        self.assertEqual([r.cash for r in equity_rows], [50, 50, 50])


class SmaCrossoverSimulationTests(unittest.TestCase):
    def test_crossover_buy_and_sell(self):
        decide_fn, params = backtest._build_sma_crossover({"fast": 2, "slow": 3})
        self.assertEqual(params, {"fast": 2, "slow": 3})
        bars = _bars([10, 10, 10, 12, 14, 9, 8, 8])

        equity_rows, trade_rows = backtest._simulate_ticker(decide_fn, bars, initial_capital=120)

        self.assertEqual(len(trade_rows), 1)
        trade = trade_rows[0]
        self.assertEqual(trade.entry_price, 12)
        self.assertEqual(trade.exit_price, 9)
        self.assertEqual(trade.shares, 10)
        self.assertEqual(trade.gross_pnl, -30)
        self.assertEqual(trade.net_pnl, -30)
        self.assertAlmostEqual(trade.return_pct, -25.0)
        self.assertEqual(trade.holding_days, 2)
        self.assertEqual(trade.exit_reason, "SIGNAL")
        self.assertEqual(equity_rows[-1].equity, 90)

        summary = backtest.compute_summary_metrics(120, equity_rows, trade_rows)
        self.assertEqual(summary.num_trades, 1)
        self.assertEqual(summary.win_rate_pct, 0.0)
        self.assertEqual(summary.profit_factor, 0.0)

    def test_build_validates_params(self):
        with self.assertRaises(ValueError):
            backtest._build_sma_crossover({"fast": 10, "slow": 5})
        with self.assertRaises(ValueError):
            backtest._build_sma_crossover({"fast": 0, "slow": 5})
        with self.assertRaises(ValueError):
            backtest._build_sma_crossover({"bogus": 1})

    def test_no_lookahead_bars_truncated_to_today(self):
        seen_lengths = []

        def spy_decide(context):
            seen_lengths.append(len(context.bars))
            return Signal(ACTION_HOLD)

        bars = _bars([10, 11, 12, 13, 14])
        backtest._simulate_ticker(spy_decide, bars, initial_capital=1000)
        self.assertEqual(seen_lengths, [1, 2, 3, 4, 5])


class MetricsEdgeCaseTests(unittest.TestCase):
    def test_sharpe_none_when_flat_or_too_short(self):
        flat = _bars([100, 100, 100])
        decide_fn, _ = backtest._build_buy_and_hold({})
        equity_rows, trade_rows = backtest._simulate_ticker(
            lambda ctx: Signal(ACTION_HOLD), flat, initial_capital=1000
        )
        summary = backtest.compute_summary_metrics(1000, equity_rows, trade_rows)
        self.assertIsNone(summary.sharpe_ratio)

        equity_rows_short, trade_rows_short = backtest._simulate_ticker(
            lambda ctx: Signal(ACTION_HOLD), _bars([100]), initial_capital=1000
        )
        summary_short = backtest.compute_summary_metrics(1000, equity_rows_short, trade_rows_short)
        self.assertIsNone(summary_short.sharpe_ratio)

    def test_cagr_none_when_no_elapsed_time_or_non_positive_equity(self):
        equity_rows, trade_rows = backtest._simulate_ticker(
            lambda ctx: Signal(ACTION_HOLD), _bars([100]), initial_capital=1000
        )
        summary = backtest.compute_summary_metrics(1000, equity_rows, trade_rows)
        self.assertIsNone(summary.cagr_pct)


class KdBottomVolumeBreakoutTests(unittest.TestCase):
    # 這段收盤價先走 10 天跌勢把 KD 壓到低檔，第 11 天（index 10）大漲，
    # 用 kd_period=3 算出來的 k/d 在 index 9（9.8/7.15 一帶，皆 < 20）之後，
    # index 10 剛好黃金交叉（k 34.6 > d 16.3），前高（index 0~9 收盤最高）是 100。
    CLOSES = [100, 95, 90, 85, 80, 76, 73, 70, 68, 67, 95]

    @staticmethod
    def _context(bars_list, shares=0.0, avg_cost=None):
        return backtest.StrategyContext(db_path=None, ticker=None, bars=tuple(bars_list),
                                         cash=1000.0, shares=shares, avg_cost=avg_cost)

    def test_build_validates_params(self):
        for bad in ({"entry_fraction": 1.5}, {"entry_fraction": 0}, {"stop_loss_pct": 0},
                    {"breakout_lookback": 0}, {"kd_period": 1}, {"volume_ratio_threshold": 0},
                    {"ma_exit_period": 0}, {"bogus": 1}):
            with self.assertRaises(ValueError):
                backtest._build_kd_bottom_volume_breakout(bad)

    def test_entry_fires_on_golden_cross_at_bottom_with_volume_surge(self):
        decide_fn, resolved = backtest._build_kd_bottom_volume_breakout(
            {"kd_period": 3, "entry_fraction": 0.5}
        )
        self.assertEqual(resolved["entry_fraction"], 0.5)
        volumes = [1000] * 10 + [1600]  # 最後一天量增 1.6 倍 > 預設門檻 1.5
        bars = _bars_with_volume(self.CLOSES, volumes)
        signal = decide_fn(self._context(bars))
        self.assertEqual(signal.action, backtest.ACTION_BUY)
        self.assertAlmostEqual(signal.size_fraction, 0.5)

    def test_no_entry_without_volume_surge(self):
        decide_fn, _ = backtest._build_kd_bottom_volume_breakout({"kd_period": 3})
        bars = _bars_with_volume(self.CLOSES, [1000] * 11)  # 量比 1.0，沒有放大
        signal = decide_fn(self._context(bars))
        self.assertEqual(signal.action, backtest.ACTION_HOLD)

    def test_no_entry_when_not_at_bottom(self):
        decide_fn, _ = backtest._build_kd_bottom_volume_breakout({"kd_period": 3, "kd_threshold": 5})
        bars = _bars_with_volume(self.CLOSES, [1000] * 10 + [1600])
        signal = decide_fn(self._context(bars))  # d[9]=7.15 不低於門檻 5，落底條件不成立
        self.assertEqual(signal.action, backtest.ACTION_HOLD)

    def test_position_sized_by_entry_fraction(self):
        decide_fn, _ = backtest._build_kd_bottom_volume_breakout(
            {"kd_period": 3, "entry_fraction": 0.5, "breakout_lookback": 20, "stop_loss_pct": 15}
        )
        # 加一天持平收盤（95，沒有站上前高 100 也沒跌破停損線），讓進場那天的
        # equity row 不是模擬結束時被強制平倉覆寫掉的最後一列。
        closes = self.CLOSES + [95]
        volumes = [1000] * 10 + [1600, 1000]
        bars = _bars_with_volume(closes, volumes)
        equity_rows, trade_rows = backtest._simulate_ticker(decide_fn, bars, initial_capital=950)
        entry_row = equity_rows[10]
        self.assertEqual(entry_row.shares, 5)  # floor(950*0.5/95) = 5
        self.assertAlmostEqual(entry_row.cash, 475.0)
        self.assertEqual(trade_rows[0].exit_reason, "END_OF_RANGE")  # 區間結束強制平倉，不是策略出場

    def test_exit_on_breakout_above_prior_high(self):
        decide_fn, _ = backtest._build_kd_bottom_volume_breakout(
            {"kd_period": 3, "entry_fraction": 1.0, "breakout_lookback": 20, "stop_loss_pct": 15}
        )
        closes = self.CLOSES + [101]  # 前高是 100（index 0~9 最高收盤），101 站上前高
        volumes = [1000] * 10 + [1600, 1000]
        bars = _bars_with_volume(closes, volumes)
        _, trade_rows = backtest._simulate_ticker(decide_fn, bars, initial_capital=1000)
        self.assertEqual(len(trade_rows), 1)
        trade = trade_rows[0]
        self.assertEqual(trade.entry_price, 95)
        self.assertEqual(trade.exit_price, 101)
        self.assertEqual(trade.exit_reason, "SIGNAL")

    def test_exit_on_stop_loss(self):
        decide_fn, _ = backtest._build_kd_bottom_volume_breakout(
            {"kd_period": 3, "entry_fraction": 1.0, "breakout_lookback": 20, "stop_loss_pct": 15}
        )
        closes = self.CLOSES + [80]  # 進場價 95，停損線 95*0.85=80.75，80 已跌破
        volumes = [1000] * 10 + [1600, 1000]
        bars = _bars_with_volume(closes, volumes)
        _, trade_rows = backtest._simulate_ticker(decide_fn, bars, initial_capital=1000)
        self.assertEqual(len(trade_rows), 1)
        trade = trade_rows[0]
        self.assertEqual(trade.entry_price, 95)
        self.assertEqual(trade.exit_price, 80)
        self.assertEqual(trade.exit_reason, "SIGNAL")

    def test_exit_when_close_breaks_below_ma_exit_period(self):
        # 直接灌一個「已經持有倉位」的 context，繞開進場判斷邏輯，單獨測試
        # 「跌破 ma_exit_period 日線」這個出場條件：9 天平盤 100、第 10 天跌到
        # 95，MA10=99.5，95 < 99.5 觸發出場；進場價設得很低（50）讓停損線
        # （50*0.85=42.5）跟前高（None，從沒進過 shares==0 分支）都不會先擋到。
        decide_fn, _ = backtest._build_kd_bottom_volume_breakout(
            {"kd_period": 3, "ma_exit_period": 10, "stop_loss_pct": 15}
        )
        bars = _bars_with_volume([100] * 9 + [95], [1000] * 10)
        context = self._context(bars, shares=10.0, avg_cost=50.0)
        signal = decide_fn(context)
        self.assertEqual(signal.action, backtest.ACTION_SELL)

    def test_no_exit_when_close_still_above_ma_exit_period(self):
        decide_fn, _ = backtest._build_kd_bottom_volume_breakout(
            {"kd_period": 3, "ma_exit_period": 10, "stop_loss_pct": 15}
        )
        bars = _bars_with_volume([100] * 9 + [101], [1000] * 10)  # 101 > MA10 (~100.1)
        context = self._context(bars, shares=10.0, avg_cost=50.0)
        signal = decide_fn(context)
        self.assertEqual(signal.action, backtest.ACTION_HOLD)

    def test_end_to_end_via_run_backtest(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            closes = self.CLOSES + [101]
            volumes = [1000] * 10 + [1600, 1000]
            from datetime import date, timedelta
            base = date.fromisoformat("2026-01-01")
            rows = [
                ((base + timedelta(days=i)).isoformat(), "2330", "TWSE", "x", c, 0.0, v, c * v)
                for i, (c, v) in enumerate(zip(closes, volumes))
            ]
            history.upsert_daily_prices(db, rows)

            run_id = backtest.run_backtest(
                db, strategy_key="kd_bottom_volume_breakout",
                params={"kd_period": 3, "entry_fraction": 1.0, "breakout_lookback": 20,
                        "stop_loss_pct": 15},
                tickers=["2330"], start_date="2026-01-01", end_date="2026-01-31",
                initial_capital=1000,
            )
            run = backtest.get_run(db, run_id)
            self.assertEqual(run["status"], "DONE")
            trades = backtest.get_trades(db, run_id, "2330")
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]["entry_price"], 95)
            self.assertEqual(trades[0]["exit_price"], 101)


class RunBacktestDbIntegrationTests(unittest.TestCase):
    def _seed(self, db, ticker, closes, start="2026-01-01"):
        from datetime import date, timedelta
        base = date.fromisoformat(start)
        rows = [
            ((base + timedelta(days=i)).isoformat(), ticker, "TWSE", "x", close, 0.0, 1000, close * 1000)
            for i, close in enumerate(closes)
        ]
        history.upsert_daily_prices(db, rows)

    def test_end_to_end_buy_and_hold_persists_all_tables(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            self._seed(db, "2330", [100, 110, 90, 95, 120])

            run_id = backtest.run_backtest(
                db, strategy_key="buy_and_hold", params={}, tickers=["2330"],
                start_date="2026-01-01", end_date="2026-01-31", initial_capital=1000,
            )

            conn = backtest._connect(db)
            run_row = conn.execute(
                "SELECT status, strategy_key, error_message FROM backtest_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            self.assertEqual(run_row, ("DONE", "buy_and_hold", None))

            equity = conn.execute(
                "SELECT date, equity FROM backtest_equity_curve WHERE run_id=? AND ticker=? ORDER BY date",
                (run_id, "2330"),
            ).fetchall()
            self.assertEqual([e for _, e in equity], [1000, 1100, 900, 950, 1200])

            trades = conn.execute(
                "SELECT entry_price, exit_price, net_pnl, exit_reason FROM backtest_trades "
                "WHERE run_id=? AND ticker=?",
                (run_id, "2330"),
            ).fetchall()
            self.assertEqual(trades, [(100, 120, 200, "END_OF_RANGE")])

            summary = conn.execute(
                "SELECT total_return_pct, num_trades FROM backtest_summary WHERE run_id=? AND ticker=?",
                (run_id, "2330"),
            ).fetchone()
            self.assertAlmostEqual(summary[0], 20.0)
            self.assertEqual(summary[1], 1)
            conn.close()

    def test_multi_ticker_run_has_independent_capital_per_ticker(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            self._seed(db, "AAA", [100, 100, 100])
            self._seed(db, "BBB", [50, 50, 50])

            run_id = backtest.run_backtest(
                db, strategy_key="buy_and_hold", params={}, tickers=["AAA", "BBB"],
                start_date="2026-01-01", end_date="2026-01-31", initial_capital=1000,
            )

            summaries = {
                row["ticker"]: row for row in backtest.get_summary(db, run_id)
            }
            self.assertEqual(summaries["AAA"]["initial_capital"], 1000)
            self.assertEqual(summaries["BBB"]["initial_capital"], 1000)
            self.assertEqual(summaries["AAA"]["final_equity"], 1000)
            self.assertEqual(summaries["BBB"]["final_equity"], 1000)

    def test_missing_data_ticker_fails_gracefully(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            self._seed(db, "2330", [100, 110, 120])

            run_id = backtest.run_backtest(
                db, strategy_key="buy_and_hold", params={}, tickers=["2330", "9999"],
                start_date="2026-01-01", end_date="2026-01-31", initial_capital=1000,
            )
            run = backtest.get_run(db, run_id)
            self.assertEqual(run["status"], "DONE")
            self.assertIn("9999", run["error_message"])
            self.assertIsNotNone(backtest.get_summary(db, run_id, "2330"))
            self.assertIsNone(backtest.get_summary(db, run_id, "9999"))

    def test_all_tickers_missing_marks_run_failed(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            run_id = backtest.run_backtest(
                db, strategy_key="buy_and_hold", params={}, tickers=["9999"],
                start_date="2026-01-01", end_date="2026-01-31", initial_capital=1000,
            )
            run = backtest.get_run(db, run_id)
            self.assertEqual(run["status"], "FAILED")
            self.assertIsNotNone(run["error_message"])

    def test_idempotent_schema_creation(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            backtest._connect(db).close()
            backtest._connect(db).close()  # should not raise

    def test_delete_run_cascades_to_children(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            self._seed(db, "2330", [100, 110, 120])
            run_id = backtest.run_backtest(
                db, strategy_key="buy_and_hold", params={}, tickers=["2330"],
                start_date="2026-01-01", end_date="2026-01-31", initial_capital=1000,
            )
            conn = backtest._connect(db)
            conn.execute("DELETE FROM backtest_runs WHERE run_id=?", (run_id,))
            conn.commit()
            for table in ("backtest_equity_curve", "backtest_trades", "backtest_summary"):
                remaining = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id=?", (run_id,)).fetchone()[0]
                self.assertEqual(remaining, 0, table)
            conn.close()

    def test_unknown_strategy_key_raises(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            with self.assertRaises(ValueError):
                backtest.run_backtest(
                    db, strategy_key="bogus", params={}, tickers=["2330"],
                    start_date="2026-01-01", end_date="2026-01-31", initial_capital=1000,
                )


if __name__ == "__main__":
    unittest.main()
