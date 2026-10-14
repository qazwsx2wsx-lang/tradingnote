import tempfile
import unittest
from pathlib import Path

import tradingnote_chips as chips
import tradingnote_history as history
import tradingnote_screener as screener


class RuleTests(unittest.TestCase):
    def test_mfi_all_up_is_100_and_needs_enough_data(self):
        closes = list(range(1, 17))
        self.assertEqual(screener.calculate_mfi(closes, [10.0] * 16), 100.0)
        self.assertIsNone(screener.calculate_mfi(closes[:10], [10.0] * 10))

    def test_mfi_balanced(self):
        closes = [10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10]
        self.assertAlmostEqual(screener.calculate_mfi(closes, [1.0] * 15), 100 * 7 / 14)

    def test_margin_down_days(self):
        self.assertEqual(screener._margin_down_days([10, 9, 8, 7]), 3)
        self.assertEqual(screener._margin_down_days([10, 12, 8, 7]), 2)
        self.assertEqual(screener._margin_down_days([10, 11]), 0)

    def test_rules_require_all_and_none_fails(self):
        good = {"big_delta": 0.5, "margin_down_days": 3, "volume_ratio": 1.2, "mfi": 60.0,
                "ticker": "1111"}
        bad_mfi = dict(good, mfi=85.0, ticker="2222")
        missing = dict(good, big_delta=None, ticker="3333")
        matched, counts = screener.apply_rules({m["ticker"]: m for m in (good, bad_mfi, missing)})
        self.assertEqual([m["ticker"] for m in matched], ["1111"])
        self.assertEqual(counts, {"big_holder": 2, "volume_ratio": 3, "mfi": 2})


class PipelineTests(unittest.TestCase):
    def test_run_screen_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            days = [f"2026-09-{n:02d}" for n in range(1, 21)]
            prices = []
            for i, day in enumerate(days):
                for market, base in (("TWSE", 1000), ("TPEX", 5000)):
                    # 補足兩個市場的筆數門檻用的假股票
                    for k in range(screener.MIN_PRICE_ROWS):
                        prices.append((day, f"{base + k}", market, "x", 10.0 + i * 0.1, 0.0, 1000, 1e4))
                # 目標股 2330：價格先跌再漲，今日放量
                prices.append((day, "2330", "TWSE", "台積電", 100.0 - (i % 2), 0.0,
                               5000 if i == len(days) - 1 else 1000, 1e6))
            history.upsert_daily_prices(db, prices)
            conn = chips._connect(db)
            for day in days[-4:]:
                for market in ("TWSE", "TPEX"):
                    conn.execute("INSERT INTO daily_margin VALUES (?,?,?,0,0,0,?,0,0,0,0,0)",
                                 (day, "2330" if market == "TWSE" else "5000", market,
                                  {days[-4]: 100, days[-3]: 90, days[-2]: 80, days[-1]: 70}[day]))
            for date, pct in (("2026-09-06", 50.0), ("2026-09-13", 50.5), ("2026-09-20", 51.0)):
                conn.execute("INSERT INTO tdcc_holders VALUES (?,?,?,?,?,?)", (date, "2330", 15, 1, 1, pct))
            conn.commit()
            conn.close()
            directory = {"2330": {"name": "台積電", "industry": "半導體業", "market": "TWSE"}}
            # MFI：價格交錯漲跌，約 50，低於 80
            result = screener.run_screen(db, directory=directory)
            self.assertEqual(result["date"], days[-1])
            self.assertEqual(result["universe"], 1)
            self.assertEqual([r["ticker"] for r in result["rows"]], ["2330"])
            row = result["rows"][0]
            self.assertAlmostEqual(row["big_delta"], 1.0)
            self.assertEqual(row["margin_down_days"], 3)
            self.assertAlmostEqual(row["volume_ratio"], 5.0)
            self.assertEqual(len(row["big_trail"]), 3)
            self.assertEqual(screener.run_dates(db), [days[-1]])
            loaded = screener.load_run(db, days[-1])
            self.assertEqual(loaded["matched"], 1)
            self.assertEqual(loaded["rows"][0]["industry"], "半導體業")
            self.assertEqual([p for _d, p in loaded["rows"][0]["big_trail"]], [50.0, 50.5, 51.0])


if __name__ == "__main__":
    unittest.main()
