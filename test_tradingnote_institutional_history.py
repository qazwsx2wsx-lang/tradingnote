import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import tradingnote_history as history
import tradingnote_institutional_history as ih


class AccelerationTests(unittest.TestCase):
    def test_recent_minus_previous_average(self):
        # 前 5 天平均 2、近 5 天平均 10 → 每天多買 8 億
        values = [2, 2, 2, 2, 2, 6, 8, 10, 12, 14]
        self.assertAlmostEqual(ih.acceleration(values), 8.0)

    def test_uses_only_last_ten_days(self):
        values = [1000, -1000] + [0] * 5 + [5] * 5
        self.assertAlmostEqual(ih.acceleration(values), 5.0)

    def test_deceleration_is_negative(self):
        self.assertAlmostEqual(ih.acceleration([10] * 5 + [-10] * 5), -20.0)

    def test_insufficient_history_returns_none(self):
        self.assertIsNone(ih.acceleration([1] * 9))
        self.assertIsNone(ih.acceleration([]))


class StreakTests(unittest.TestCase):
    def test_consecutive_buys_positive(self):
        self.assertEqual(ih.streak([-3, 1, 2, 3, 4]), 4)

    def test_consecutive_sells_negative(self):
        self.assertEqual(ih.streak([5, 5, -1, -2]), -2)

    def test_zero_breaks_streak(self):
        self.assertEqual(ih.streak([1, 1, 0, 2]), 1)
        self.assertEqual(ih.streak([1, 1, 0]), 0)

    def test_empty(self):
        self.assertEqual(ih.streak([]), 0)

    def test_whole_series_same_sign(self):
        self.assertEqual(ih.streak([1] * 7), 7)


class WindowAndChangeTests(unittest.TestCase):
    def test_window_sum_counts_trading_days(self):
        values = list(range(1, 26))  # 25 個交易日
        self.assertEqual(ih.window_sum(values, 5), 21 + 22 + 23 + 24 + 25)
        self.assertEqual(ih.window_sum(values, 20), sum(range(6, 26)))

    def test_window_sum_short_history(self):
        self.assertEqual(ih.window_sum([1, 2], 5), 3)

    def test_stock_amount_in_yi(self):
        # 2,000 張（2,000,000 股）× 500 元 ＝ 10 億
        self.assertAlmostEqual(ih.stock_amount(2_000_000, 500), 10.0)

    def test_compound_and_equal_weight(self):
        self.assertAlmostEqual(ih.compound_change_pct([10, 10]), 21.0)
        self.assertAlmostEqual(ih.equal_weight_change([10, -2, None]), 4.0)
        self.assertIsNone(ih.equal_weight_change([None]))


class ParserTests(unittest.TestCase):
    def test_parse_t86_foreign_includes_foreign_dealer(self):
        row = ["2330", "台積電", "0", "0", "1,000", "0", "0", "200", "0", "0", "-50",
               "300", "0", "0", "0", "0", "0", "0", "1,450"]
        rows = ih.parse_t86({"stat": "OK", "data": [row]})
        self.assertEqual(rows, [("2330", "台積電", 1200, -50, 300)])

    def test_parse_t86_non_trading_day(self):
        self.assertIsNone(ih.parse_t86({"stat": "很抱歉，沒有符合條件的資料!"}))

    def test_parse_tpex_insti_uses_total_columns(self):
        row = ["6488", "環球晶", "0", "0", "100", "0", "0", "5", "0", "0", "105",
               "0", "0", "-7", "0", "0", "1", "0", "0", "2", "0", "0", "3", "101"]
        rows = ih.parse_tpex_insti({"stat": "ok", "tables": [{"data": [row]}]})
        self.assertEqual(rows, [("6488", "環球晶", 105, -7, 3)])
        self.assertIsNone(ih.parse_tpex_insti({"stat": "ok", "tables": [{"data": []}]}))

    def test_parse_bfi82u(self):
        data = [["自營商(自行買賣)", "", "", "10"], ["自營商(避險)", "", "", "-4"],
                ["投信", "", "", "7"], ["外資及陸資(不含外資自營商)", "", "", "100"],
                ["外資自營商", "", "", "1"], ["合計", "", "", "114"]]
        self.assertEqual(ih.parse_bfi82u({"stat": "OK", "data": data}), (101.0, 7.0, 6.0))

    def test_parse_tpex_summary(self):
        data = [["外資及陸資合計", "", "", "-30"], ["　外資及陸資(不含自營商)", "", "", "-30"],
                ["投信", "", "", "5"], ["自營商合計", "", "", "2"], ["三大法人合計*", "", "", "-23"]]
        self.assertEqual(
            ih.parse_tpex_summary({"stat": "ok", "tables": [{"data": data}]}), (-30.0, 5.0, 2.0))

    def test_parse_tpex_daily_quotes(self):
        payload = {"stat": "ok", "tables": [{"fields": ["代號"], "data": [
            ["6488", "環球晶", "110.00", "10.00 ", "", "", "", "", "1,000", "110,000"],
            ["7777", "無成交", "----", "", "", "", "", "", "0", "0"],
        ]}]}
        records = history.parse_tpex_daily_quotes(payload)
        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0]["change_pct"], 10.0)


class SectorMapTests(unittest.TestCase):
    def test_overrides_split_rename_and_exclude(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "o.json"
            path.write_text(json.dumps({
                "stocks": {"2330": "晶圓代工"},
                "rename_sectors": {"航運業": "航運"},
                "exclude_sectors": ["臺灣存託憑證"],
            }), encoding="utf-8")
            directory = {
                "2330": {"name": "台積電", "industry": "半導體業", "market": "TWSE"},
                "2454": {"name": "聯發科", "industry": "半導體業", "market": "TWSE"},
                "2603": {"name": "長榮", "industry": "航運業", "market": "TWSE"},
                "9103": {"name": "美德醫", "industry": "臺灣存託憑證", "market": "TWSE"},
            }
            mapping = ih.build_sector_map(directory, path)
        self.assertEqual(mapping["2330"][2], "晶圓代工")
        self.assertEqual(mapping["2454"][2], "半導體業")
        self.assertEqual(mapping["2603"][2], "航運")
        self.assertNotIn("9103", mapping)


class ComputeMetricsTests(unittest.TestCase):
    """12 個交易日、兩檔同類股：驗證寫入 sector_metrics／stock_metrics 的數值。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "history.db"
        self.dates = [date(2026, 9, d).isoformat() for d in range(1, 13)]
        prices, inst = [], []
        for i, d in enumerate(self.dates):
            prices += [(d, "1111", "TWSE", "甲", 100.0, 1.0, 1, 5e8),
                       (d, "2222", "TPEX", "乙", 50.0, -1.0, 1, 1e8)]
            # 甲：外資前 6 天賣 1000 張、後 6 天買 2000 張；乙：投信每天買 1000 張
            inst += [(d, "1111", "TWSE", "甲", -1_000_000 if i < 6 else 2_000_000, 0, 0),
                     (d, "2222", "TPEX", "乙", 0, 1_000_000, 0)]
        history.upsert_daily_prices(self.db, prices)
        conn = ih._connect(self.db)
        conn.executemany("INSERT INTO daily_institutional VALUES (?, ?, ?, ?, ?, ?, ?)", inst)
        for d in self.dates:
            conn.executemany("INSERT INTO market_summary VALUES (?, ?, 0, 0, 0)",
                             [(d, "TWSE"), (d, "TPEX")])
        conn.executemany("INSERT INTO sector_map VALUES (?, ?, ?, ?)",
                         [("1111", "甲", "TWSE", "測試"), ("2222", "乙", "TPEX", "測試")])
        conn.commit()
        conn.close()
        ih.compute_metrics(self.db)

    def row(self, table, where, params):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchone()
        finally:
            conn.close()

    def test_sector_foreign_metrics_on_last_day(self):
        r = self.row("sector_metrics", "date=? AND investor='foreign'", (self.dates[-1],))
        self.assertAlmostEqual(r["day_amt"], 2.0)       # 2000 張 × 100 元 = 2 億
        self.assertAlmostEqual(r["sum5"], 10.0)
        self.assertAlmostEqual(r["sum20"], 6 * 2.0 - 6 * 1.0)
        # 近 5 日（第 8～12 天）平均 2；前 5 日（第 3～7 天）= -1,-1,-1,-1,+2 → -0.4
        self.assertAlmostEqual(r["accel"], 2.4)
        self.assertEqual(r["streak"], 6)
        self.assertAlmostEqual(r["trading_value"], 6.0)
        self.assertEqual(r["stock_count"], 2)
        # 甲 5 日 +5.1%、乙 -4.9% 等權平均
        expected = (ih.compound_change_pct([1] * 5) + ih.compound_change_pct([-1] * 5)) / 2
        self.assertAlmostEqual(r["change5_pct"], expected)

    def test_sector_all_is_sum_of_investors(self):
        r = self.row("sector_metrics", "date=? AND investor='all'", (self.dates[-1],))
        self.assertAlmostEqual(r["day_amt"], 2.0 + 0.5)

    def test_accel_none_before_ten_days(self):
        r = self.row("sector_metrics", "date=? AND investor='foreign'", (self.dates[8],))
        self.assertIsNone(r["accel"])

    def test_stock_foreign_streak_and_sum20(self):
        r = self.row("stock_metrics", "date=? AND ticker='1111' AND investor='foreign'",
                     (self.dates[-1],))
        self.assertEqual(r["streak"], 6)
        self.assertAlmostEqual(r["sum20"], 6.0)
        r = self.row("stock_metrics", "date=? AND ticker='1111' AND investor='foreign'",
                     (self.dates[3],))
        self.assertEqual(r["streak"], -4)


    def test_group_flow_for_dates_sums_daily_amounts(self):
        flows = ih.aggregate_group_flow_for_dates(
            self.db, {"甲乙": {"1111", "2222"}}, self.dates[-2:])
        self.assertEqual(len(flows), 1)
        self.assertAlmostEqual(flows[0].foreign_value, 2 * 2_000_000 * 100)
        self.assertAlmostEqual(flows[0].trust_value, 2 * 1_000_000 * 50)
        self.assertEqual(flows[0].covered_stocks, 2)

    def test_group_flow_for_dates_requires_full_coverage(self):
        self.assertIsNone(ih.aggregate_group_flow_for_dates(
            self.db, {"甲乙": {"1111"}}, [self.dates[-1], "2026-09-13"]))


class BackfillSkipTests(unittest.TestCase):
    def test_non_trading_days_skipped_and_remembered(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "history.db"
            calls = []

            def fake_fetch(db_path, day, delay, log):
                calls.append(day)
                if day == date(2026, 9, 22):
                    return False  # 假日
                conn = ih._connect(db_path)
                conn.executemany("INSERT INTO market_summary VALUES (?, ?, 0, 0, 0)",
                                 [(day.isoformat(), "TWSE"), (day.isoformat(), "TPEX")])
                conn.commit()
                conn.close()
                return True

            with patch.object(ih, "fetch_day", fake_fetch), \
                    patch.object(ih, "taipei_today", return_value=date(2026, 9, 25)), \
                    patch.object(ih, "refresh_sector_map"), \
                    patch.object(ih, "compute_metrics"):
                written = ih.backfill(db, target_days=3, end_date=date(2026, 9, 24), log=lambda *_: None)
                self.assertEqual(written, ["2026-09-24", "2026-09-23", "2026-09-21"])
                calls.clear()
                ih.backfill(db, target_days=4, end_date=date(2026, 9, 24), log=lambda *_: None)
            # 第二次：已完成日期與已知假日都不再打 API，只補更早的 9/18（9/19、9/20 是週末）
            self.assertEqual(calls, [date(2026, 9, 18)])


if __name__ == "__main__":
    unittest.main()
