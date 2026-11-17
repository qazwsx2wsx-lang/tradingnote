import tempfile
import unittest
from datetime import date
from pathlib import Path

import tradingnote_futures_backtest as fb
import tradingnote_tradejournal as tj


def _trade(side="LONG", entry=100.0, qty=1, size=2000, stop=None, month="202611",
           entry_date="2026-11-02", net=0.0, exit_date="2026-11-10", commission=0.0):
    return fb.ClosedTrade(
        trade_id=1, symbol="2330", futures_code="CDF", contract_month=month, side=side,
        contract_size=size, qty=qty, entry_date=entry_date, exit_date=exit_date,
        avg_entry=entry, avg_exit=entry, gross_pnl=net, commission=commission, tax=0.0,
        net_pnl=net, stop=stop, strategy="未分類",
    )


def _bar(day, o, h, l, c, settlement=None):
    return {"date": day, "open": o, "high": h, "low": l, "close": c, "settlement": settlement}


ENTRY_DAY = _bar("2026-11-02", 100, 130, 70, 100)  # 進場當天的 K 一律不檢查


class ThirdWednesdayTests(unittest.TestCase):
    def test_known_months(self):
        self.assertEqual(fb.third_wednesday("202611"), date(2026, 11, 18))
        self.assertEqual(fb.third_wednesday("2026/07"), date(2026, 7, 15))
        self.assertEqual(fb.third_wednesday("202704"), date(2027, 4, 21))


class SimulateExitTests(unittest.TestCase):
    def test_fixed_stop_and_tax(self):
        sim = fb.simulate_exit(
            _trade(), [ENTRY_DAY, _bar("2026-11-03", 99, 101, 94, 96)],
            fb.ExitRule(stop_mode="pct", stop_pct=5),
        )
        self.assertEqual(sim.reason, fb.EXIT_STOP)
        self.assertEqual(sim.exit_price, 95)
        self.assertAlmostEqual(sim.gross_pnl, -10000)
        self.assertAlmostEqual(sim.tax, 4 + 4)
        self.assertAlmostEqual(sim.net_pnl, -10008)
        self.assertEqual(sim.bars_held, 1)

    def test_gap_through_stop_fills_at_open(self):
        sim = fb.simulate_exit(
            _trade(), [_bar("2026-11-03", 93, 94, 90, 92)],
            fb.ExitRule(stop_mode="pct", stop_pct=5),
        )
        self.assertEqual(sim.exit_price, 93)

    def test_same_bar_stop_and_target_assumes_stop(self):
        sim = fb.simulate_exit(
            _trade(), [_bar("2026-11-03", 100, 106, 94, 100)],
            fb.ExitRule(stop_mode="pct", stop_pct=5, take_profit_mode="pct", take_profit_pct=5),
        )
        self.assertEqual(sim.reason, fb.EXIT_STOP)

    def test_take_profit_pct_and_r(self):
        bars = [_bar("2026-11-03", 101, 106, 99, 104), _bar("2026-11-04", 104, 109, 103, 108)]
        sim = fb.simulate_exit(_trade(), bars, fb.ExitRule(take_profit_mode="pct", take_profit_pct=5))
        self.assertEqual((sim.reason, sim.exit_price, sim.exit_date), (fb.EXIT_TARGET, 105, "2026-11-03"))

        sim = fb.simulate_exit(
            _trade(stop=96), bars,
            fb.ExitRule(stop_mode="plan", take_profit_mode="r", take_profit_r=2),
        )
        self.assertEqual((sim.exit_price, sim.exit_date), (108, "2026-11-04"))

    def test_max_hold_days_exits_at_close(self):
        bars = [_bar("2026-11-03", 100, 101, 99, 100.5), _bar("2026-11-04", 100, 102, 99, 101.5),
                _bar("2026-11-05", 101, 103, 100, 102)]
        sim = fb.simulate_exit(_trade(), bars, fb.ExitRule(max_hold_days=2))
        self.assertEqual((sim.reason, sim.exit_price, sim.exit_date), (fb.EXIT_TIME, 101.5, "2026-11-04"))

    def test_trailing_stop_uses_prior_extreme(self):
        bars = [_bar("2026-11-03", 101, 110, 101, 108), _bar("2026-11-04", 107, 109, 104, 105)]
        sim = fb.simulate_exit(_trade(), bars, fb.ExitRule(trailing_pct=5))
        self.assertEqual(sim.reason, fb.EXIT_TRAIL)
        self.assertAlmostEqual(sim.exit_price, 104.5)

    def test_expiry_settlement_and_bars_after_expiry_ignored(self):
        bars = [_bar("2026-11-03", 100, 101, 99, 100),
                _bar("2026-11-18", 118, 121, 117, 119, settlement=120),
                _bar("2026-11-19", 50, 50, 50, 50)]
        sim = fb.simulate_exit(_trade(), bars, fb.ExitRule())
        self.assertEqual((sim.reason, sim.exit_price, sim.exit_date), (fb.EXIT_EXPIRY, 120, "2026-11-18"))

    def test_runs_out_of_data(self):
        bars = [_bar("2026-11-03", 100, 101, 99, 100), _bar("2026-11-05", 101, 103, 100, 102)]
        sim = fb.simulate_exit(_trade(), bars, fb.ExitRule())
        self.assertEqual((sim.reason, sim.exit_price), (fb.EXIT_NO_DATA, 102))

    def test_no_bars_after_entry_returns_none(self):
        self.assertIsNone(fb.simulate_exit(_trade(), [ENTRY_DAY], fb.ExitRule()))

    def test_short_stop_and_mini_size(self):
        sim = fb.simulate_exit(
            _trade(side="SHORT", size=100, qty=3), [_bar("2026-11-03", 101, 106, 100, 104)],
            fb.ExitRule(stop_mode="pct", stop_pct=5),
        )
        self.assertEqual(sim.exit_price, 105)
        self.assertAlmostEqual(sim.gross_pnl, -5 * 3 * 100)

    def test_short_trailing_stop(self):
        bars = [_bar("2026-11-03", 99, 99, 90, 92), _bar("2026-11-04", 93, 95, 92, 94)]
        sim = fb.simulate_exit(_trade(side="SHORT"), bars, fb.ExitRule(trailing_pct=5))
        self.assertEqual(sim.reason, fb.EXIT_TRAIL)
        self.assertAlmostEqual(sim.exit_price, 94.5)


class ComputeStatsTests(unittest.TestCase):
    def test_known_sequence(self):
        nets = [100, -50, -30, 200, -10]
        trades = [
            _trade(net=n, exit_date=f"2026-11-0{i + 3}", side="LONG" if i % 2 == 0 else "SHORT")
            for i, n in enumerate(nets)
        ]
        stats = fb.compute_stats(trades)
        self.assertEqual((stats.count, stats.wins, stats.losses), (5, 2, 3))
        self.assertAlmostEqual(stats.total_net, 210)
        self.assertAlmostEqual(stats.win_rate, 0.4)
        self.assertAlmostEqual(stats.profit_factor, 300 / 90)
        self.assertAlmostEqual(stats.payoff_ratio, 150 / 30)
        self.assertAlmostEqual(stats.expectancy, 42)
        self.assertAlmostEqual(stats.max_drawdown, 80)
        self.assertEqual(
            (stats.max_drawdown_peak_date, stats.max_drawdown_trough_date),
            ("2026-11-03", "2026-11-05"),
        )
        self.assertEqual(stats.max_consecutive_losses, 2)
        self.assertEqual(stats.equity_curve[-1], ("2026-11-07", 210))
        self.assertEqual({g.key: g.count for g in stats.by_side}, {"多": 3, "空": 2})

    def test_empty_and_no_losses(self):
        self.assertEqual(fb.compute_stats([]).count, 0)
        stats = fb.compute_stats([_trade(net=10)])
        self.assertIsNone(stats.profit_factor)
        self.assertEqual(stats.max_drawdown, 0)

    def test_r_multiple(self):
        trade = _trade(stop=95, net=10000)  # 風險 5 點 × 2000 = 10000
        self.assertAlmostEqual(trade.r_multiple, 1.0)
        self.assertIsNone(_trade(net=1).r_multiple)


CSV_HEADER = "日期,標的代號,合約,合約月份,買賣,口數,價格,手續費,期交稅,策略,停損價,備註\n"


class CsvImportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp_dir.name)
        self.db_path = self.dir / "history.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write(self, body, encoding="utf-8-sig"):
        path = self.dir / "trades.csv"
        path.write_text(CSV_HEADER + body, encoding=encoding)
        return path

    def test_add_reduce_and_reverse(self):
        path = self._write(
            "2026-10-01,2330,標準型,202611,買,2,1000,40,,突破,980,\n"
            "2026-10-02,2330,標準型,202611,買,1,1010,20,,,,\n"
            "2026-10-05,2330,標準型,202611,賣,3,1020,60,,,,\n"
            "2026-10-06,2330,標準型,202611,賣,1,1025,20,,,,\n"
            "2026-10-08,2330,標準型,202611,買,2,1015,40,,,,反手\n"
        )
        rows, errors = fb.parse_ssf_csv(path)
        self.assertEqual(errors, [])
        result = fb.import_ssf_rows(self.db_path, rows)
        self.assertEqual((result.added, result.skipped, result.trades_opened), (5, 0, 3))

        t1, t2, t3 = (tj.get_trade(self.db_path, i) for i in result.trade_ids)
        self.assertEqual((t1.side, t1.status, t1.contract_size), ("LONG", "CLOSED", 2000))
        self.assertAlmostEqual(t1.realized_pnl, (1020 - 3010 / 3) * 3 * 2000)
        self.assertEqual(t1.initial_stop_loss, 980)
        self.assertEqual(t1.closed_at, "2026-10-05")
        self.assertEqual((t2.side, t2.status), ("SHORT", "CLOSED"))
        self.assertAlmostEqual(t2.realized_pnl, (1025 - 1015) * 2000)
        self.assertAlmostEqual(t2.total_commission, 20 + 20)  # 反手那筆的手續費一半歸平倉
        self.assertEqual((t3.side, t3.status, t3.total_qty), ("LONG", "OPEN", 1))
        self.assertAlmostEqual(t3.total_commission, 20)

        results = fb.closed_trade_results(self.db_path)
        self.assertEqual([r.trade_id for r in results], result.trade_ids[:2])
        self.assertEqual(results[0].strategy, "突破")
        self.assertEqual(results[0].qty, 3)

        again = fb.import_ssf_rows(self.db_path, fb.parse_ssf_csv(path)[0])
        self.assertEqual((again.added, again.skipped), (0, 5))

    def test_reimport_with_new_rows_continues_open_trade(self):
        first = self._write("2026-10-01,2330,小型,202611,賣,2,1000,10,,,,\n")
        fb.import_ssf_rows(self.db_path, fb.parse_ssf_csv(first)[0])
        second = self._write(
            "2026-10-01,2330,小型,202611,賣,2,1000,10,,,,\n"
            "2026-10-03,2330,小型,202611,買,2,990,10,,,,\n"
        )
        result = fb.import_ssf_rows(self.db_path, fb.parse_ssf_csv(second)[0])
        self.assertEqual((result.added, result.skipped, result.trades_opened), (1, 1, 0))
        trade = tj.list_trades(self.db_path, instrument="SSF")[0]
        self.assertEqual(trade.status, "CLOSED")
        self.assertAlmostEqual(trade.realized_pnl, 10 * 2 * 100)

    def test_cp950_and_errors(self):
        path = self._write("2026/10/01,2330,標準型,2026/11,買,1,1000,,,,,\n", encoding="cp950")
        rows, errors = fb.parse_ssf_csv(path)
        self.assertEqual(errors, [])
        self.assertEqual((rows[0].executed_at, rows[0].contract_month), ("2026-10-01", "202611"))

        bad = self._write(
            "2026-10-01,2330,大型,202611,買,1,1000,,,,,\n"
            "2026-10-01,2330,標準型,202613,買,1,1000,,,,,\n"
            "2026-10-01,2330,標準型,202611,多,1,1000,,,,,\n"
            "2026-10-01,2330,標準型,202611,買,0,1000,,,,,\n"
        )
        rows, errors = fb.parse_ssf_csv(bad)
        self.assertEqual([line for line, _ in errors], [2, 3, 4, 5])

    def test_missing_columns(self):
        path = self.dir / "bad.csv"
        path.write_text("日期,價格\n2026-10-01,1\n", encoding="utf-8")
        rows, errors = fb.parse_ssf_csv(path)
        self.assertEqual(rows, [])
        self.assertIn("缺少欄位", errors[0][1])

    def test_template_round_trip(self):
        path = self.dir / "template.csv"
        fb.write_template(path)
        rows, errors = fb.parse_ssf_csv(path)
        self.assertEqual((len(rows), errors), (2, []))


class RunWhatIfTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _closed(self, code, entry, exit_price):
        trade_id = tj.open_trade(
            self.db_path, symbol="2330", side="LONG", initial_stop_loss=0, opened_at="2026-11-02",
            instrument="SSF", contract_size=2000, futures_code=code, contract_month="202611",
        )
        tj.record_execution(self.db_path, trade_id, action="BUY", price=entry, qty=1,
                            executed_at="2026-11-02", tax=0)
        tj.record_execution(self.db_path, trade_id, action="SELL", price=exit_price, qty=1,
                            executed_at="2026-11-06", tax=0)
        return trade_id

    def test_compares_same_trades_and_skips_missing_bars(self):
        self._closed("CDF", 100, 90)  # 實際虧 10 點
        self._closed(None, 100, 110)  # 沒有期貨代號 → 跳過

        def fetch(trade):
            if trade.futures_code is None:
                return []
            return [_bar("2026-11-03", 99, 100, 94, 95)]

        report = fb.run_whatif(
            self.db_path, fb.ExitRule(stop_mode="pct", stop_pct=5), fetch_bars=fetch
        )
        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.actual.count, 1)
        self.assertAlmostEqual(report.actual.total_net, -20000)
        self.assertAlmostEqual(report.simulated.total_net, -10000 - 8)
        usable = [row for row in report.rows if row.sim is not None][0]
        self.assertAlmostEqual(usable.diff, 9992)
        self.assertIn("缺期貨代號", [row for row in report.rows if row.sim is None][0].note)


if __name__ == "__main__":
    unittest.main()
