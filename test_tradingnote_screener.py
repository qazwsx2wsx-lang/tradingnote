import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import tradingnote_chips as chips
import tradingnote_history as history
import tradingnote_screener as screener
import tradingnote_technical as technical


class RuleTests(unittest.TestCase):
    def test_mfi_all_up_is_100_and_needs_enough_data(self):
        closes = list(range(1, 17))
        self.assertEqual(screener.calculate_mfi(closes, [10.0] * 16), 100.0)
        self.assertIsNone(screener.calculate_mfi(closes[:10], [10.0] * 10))

    def test_mfi_balanced(self):
        closes = [10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10, 11, 10]
        self.assertAlmostEqual(screener.calculate_mfi(closes, [1.0] * 15), 100 * 7 / 14)

    def test_holder_window_uses_calendar_gap(self):
        ok = ["2026-09-25", "2026-10-02", "2026-10-09"]
        base, latest, trail, problem = screener.holder_window(ok, "2026-10-14")
        self.assertEqual((base, latest, trail, problem), ("2026-09-25", "2026-10-09", ok, None))
        # 漏掉 10-02 時，不能拿更早的 09-18（21 天前）當兩週前
        missing = ["2026-09-18", "2026-10-09"]
        self.assertIsNone(screener.holder_window(missing, "2026-10-14")[0])
        self.assertIsNotNone(screener.holder_window(missing, "2026-10-14")[3])

    def test_holder_window_stale(self):
        _, latest, _, problem = screener.holder_window(
            ["2026-09-04", "2026-09-11", "2026-09-18"], "2026-10-14")
        self.assertIsNone(latest)
        self.assertIn("已", problem)

    def test_margin_down_days(self):
        self.assertEqual(screener._margin_down_days([10, 9, 8, 7]), 3)
        self.assertEqual(screener._margin_down_days([10, 12, 8, 7]), 2)
        self.assertEqual(screener._margin_down_days([10, 11]), 0)

    def test_kd_dullness(self):
        self.assertEqual(screener.kd_dullness([50, 81, 85, 90]), ("高檔", 3))
        self.assertEqual(screener.kd_dullness([50, 70, 85, 90]), ("高檔", 2))
        self.assertEqual(screener.kd_dullness([30, 19, 20, 5, 10]), ("低檔", 4))
        # 中間跌出高檔就從頭數
        self.assertEqual(screener.kd_dullness([90, 90, 79, 85]), ("高檔", 1))
        self.assertEqual(screener.kd_dullness([10, 10, 50]), (None, 0))

    def test_calculate_kd_needs_high_low_for_whole_warmup(self):
        n = screener.KD_PERIOD - 1 + screener.KD_WARMUP_DAYS
        bars = [(c + 1, c - 1, c) for c in range(100, 100 + n)]
        k, d = screener.calculate_kd(bars)
        self.assertEqual(len(k), screener.KD_WARMUP_DAYS)
        self.assertGreater(k[-1], 80)
        self.assertIsNone(screener.calculate_kd(bars[1:]))  # 少一天
        gap = list(bars)
        gap[-5] = (None, None, gap[-5][2])
        self.assertIsNone(screener.calculate_kd(gap))

    def test_rules_require_all_and_none_fails(self):
        good = {"big_delta": 0.5, "margin_down_days": 3, "volume_ratio": 1.2, "mfi": 60.0,
                "kd_dull_days": 3, "ticker": "1111"}
        bad_mfi = dict(good, mfi=85.0, ticker="2222")
        no_holder = dict(good, big_delta=None, ticker="3333")       # 大戶條件已停用：沒集保也照樣入選
        holder_down = dict(good, big_delta=-1.0, ticker="3334")
        not_dull = dict(good, kd_dull_days=2, ticker="4444")
        no_kd = dict(good, kd_dull_days=None, ticker="5555")
        no_volume = dict(good, volume_ratio=None, ticker="6666")
        matched, counts = screener.apply_rules(
            {m["ticker"]: m for m in (good, bad_mfi, no_holder, holder_down, not_dull, no_kd, no_volume)})
        self.assertEqual([m["ticker"] for m in matched], ["1111", "3333", "3334"])
        self.assertEqual(counts, {"volume_ratio": 6, "mfi": 6, "kd_dull": 5})
        self.assertNotIn("big_holder", {rule.key for rule in screener.RULES})


DAYS = [(date(2026, 8, 1) + timedelta(days=n)).isoformat() for n in range(45)]
AS_OF = DAYS[-1]


def _target_rows(ticker, name, with_high_low=True):
    """價格交錯漲跌（MFI 約 50）、長下影線讓收盤貼近 9 日高點（K 高檔鈍化）、今日放量。"""
    rows = []
    for i, day in enumerate(DAYS):
        close = 100.0 - (i % 2)
        high_low = (close + 0.1, close - 20) if with_high_low else ()
        rows.append((day, ticker, "TWSE", name, close, 0.0,
                     5000 if i == len(DAYS) - 1 else 1000, 1e6) + high_low)
    return rows


def _seed(db, extra_rows=(), margin=True):
    prices = []
    for i, day in enumerate(DAYS):
        for market, base in (("TWSE", 1000), ("TPEX", 5000)):
            # 補足兩個市場的筆數門檻用的假股票
            for k in range(screener.MIN_PRICE_ROWS):
                prices.append((day, f"{base + k}", market, "x", 10.0 + i * 0.1, 0.0, 1000, 1e4))
    history.upsert_daily_prices(db, prices + _target_rows("2330", "台積電") + list(extra_rows))
    conn = chips._connect(db)
    if margin:
        for n, day in enumerate(DAYS[-4:]):
            conn.execute("INSERT INTO daily_margin VALUES (?,?,?,0,0,0,?,0,0,0,0,0)",
                         (day, "2330", "TWSE", 100 - n * 10))
    for weeks_back, pct in ((2, 50.0), (1, 50.5), (0, 51.0)):
        holder_date = (date.fromisoformat(AS_OF) - timedelta(days=7 * weeks_back)).isoformat()
        conn.execute("INSERT INTO tdcc_holders VALUES (?,?,?,?,?,?)", (holder_date, "2330", 15, 1, 1, pct))
    conn.commit()
    conn.close()


class PipelineTests(unittest.TestCase):
    DIRECTORY = {"2330": {"name": "台積電", "industry": "半導體業", "market": "TWSE"}}

    def test_run_screen_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            _seed(db)
            result = screener.run_screen(db, directory=self.DIRECTORY)
            self.assertEqual(result["date"], AS_OF)
            self.assertEqual(result["universe"], 1)
            self.assertEqual([r["ticker"] for r in result["rows"]], ["2330"])
            row = result["rows"][0]
            self.assertAlmostEqual(row["big_delta"], 1.0)
            self.assertEqual(row["margin_down_days"], 3)
            self.assertAlmostEqual(row["volume_ratio"], 5.0)
            self.assertLess(row["mfi"], screener.MFI_MAX)
            self.assertEqual(len(row["big_trail"]), 3)
            self.assertEqual(row["kd_state"], "高檔")
            self.assertGreaterEqual(row["kd_dull_days"], screener.KD_DULL_DAYS)
            # 與個股彈窗 KD 圖（calculate_indicators）同一份資料算出的數值一致
            chart = technical.calculate_indicators([
                dict(date=r[0], max=r[8], min=r[9], close=r[4]) for r in _target_rows("2330", "台積電")])
            self.assertAlmostEqual(row["kd_k"], chart["kd"]["k"][-1], places=3)
            self.assertAlmostEqual(row["kd_d"], chart["kd"]["d"][-1], places=3)
            self.assertEqual(screener.run_dates(db), [AS_OF])
            loaded = screener.load_run(db, AS_OF)
            self.assertEqual(loaded["matched"], 1)
            self.assertEqual(loaded["rows"][0]["industry"], "半導體業")
            self.assertEqual(loaded["rows"][0]["kd_state"], "高檔")
            self.assertEqual([p for _d, p in loaded["rows"][0]["big_trail"]], [50.0, 50.5, 51.0])

    def test_missing_high_low_fails_kd_with_warning(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            _seed(db, _target_rows("2317", "鴻海", with_high_low=False))
            conn = chips._connect(db)
            for weeks_back, pct in ((2, 50.0), (0, 51.0)):
                holder_date = (date.fromisoformat(AS_OF) - timedelta(days=7 * weeks_back)).isoformat()
                conn.execute("INSERT INTO tdcc_holders VALUES (?,?,?,?,?,?)", (holder_date, "2317", 15, 1, 1, pct))
            conn.commit()
            conn.close()
            directory = dict(self.DIRECTORY, **{"2317": {"name": "鴻海", "industry": "其他", "market": "TWSE"}})
            result = screener.run_screen(db, directory=directory)
            self.assertEqual(result["universe"], 2)
            self.assertEqual([r["ticker"] for r in result["rows"]], ["2330"])
            self.assertEqual(result["rule_counts"]["kd_dull"], 1)
            self.assertNotIn("big_holder", result["rule_counts"])
            self.assertTrue(any("KD" in w for w in result["warnings"]))

    def test_run_screen_does_not_require_margin_data(self):
        """最新一天沒有融資融券資料（例如尚未公布）時，screen_dates／run_screen 仍要能選到那一天。"""
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            _seed(db, margin=False)
            self.assertEqual(screener.screen_dates(db)[-1], AS_OF)
            result = screener.run_screen(db, directory=self.DIRECTORY)
            self.assertEqual(result["date"], AS_OF)
            row = result["rows"][0]
            self.assertIsNone(row["margin_down_days"])
            self.assertIsNone(row["margin_change_pct"])

    def test_old_results_table_gets_kd_columns(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE screener_results (date TEXT, ticker TEXT, big_trail TEXT, "
                         "PRIMARY KEY (date, ticker))")
            conn.commit()
            conn.close()
            screener._connect(db).close()
            conn = sqlite3.connect(db)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(screener_results)")}
            conn.close()
            self.assertTrue({"kd_k", "kd_d", "kd_state", "kd_dull_days"} <= columns)


if __name__ == "__main__":
    unittest.main()
