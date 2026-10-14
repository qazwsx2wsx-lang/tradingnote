import tempfile
import unittest
from pathlib import Path

import tradingnote_chips as chips
import tradingnote_history as history


def _twse_payload():
    fields = ["代號", "名稱", "買進", "賣出", "現金償還", "前日餘額", "今日餘額", "次一營業日限額",
              "買進", "賣出", "現券償還", "前日餘額", "今日餘額", "次一營業日限額", "資券互抵", "註記"]
    row = ["2330", "台積電", "1,322", "353", "13", "31,586", "32,542", "9", "7", "1", "0", "38", "39", "9", "0", " "]
    return {"stat": "OK", "tables": [{}, {"fields": fields, "data": [row]}]}


def _tpex_payload():
    fields = ["代號", "名稱", "前資餘額(張)", "資買", "資賣", "現償", "資餘額", "資屬證金", "資使用率(%)",
              "資限額", "前券餘額(張)", "券賣", "券買", "券償", "券餘額", "券屬證金", "券使用率(%)",
              "券限額", "資券相抵(張)", "備註"]
    row = ["6488", "環球晶", "16,000", "400", "929", "6", "16,501", "0", "1", "9", "480", "2", "11", "2",
           "489", "0", "1", "9", "2", ""]
    return {"tables": [{"fields": fields, "data": [row]}]}


class MarginParseTests(unittest.TestCase):
    def test_twse_columns(self):
        self.assertEqual(chips.parse_twse_margin(_twse_payload()),
                         [("2330", 1322, 353, 13, 32542, 1, 7, 0, 39, 0)])

    def test_twse_not_published(self):
        self.assertIsNone(chips.parse_twse_margin({"stat": "很抱歉，沒有符合條件的資料!"}))

    def test_tpex_columns(self):
        self.assertEqual(chips.parse_tpex_margin(_tpex_payload()),
                         [("6488", 400, 929, 6, 16501, 2, 11, 2, 489, 2)])
        self.assertIsNone(chips.parse_tpex_margin({"tables": [{"fields": [], "data": []}]}))


class TdccTests(unittest.TestCase):
    CSV = (
        "﻿資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n"
        "20261008,2330,11,10,100,1.00\n"
        "20261008,2330,12,5,200,2.50\n"
        "20261008,2330,15,2,900,80.00\n"
        "20261008,2330,17,100,1000,100.00\n"
        "20261008,9999XX,15,1,1,1.00\n"
    )

    def test_parse_and_store(self):
        rows = chips.parse_tdcc_csv(self.CSV)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0], ("2026-10-08", "2330", 11, 10, 100, 1.0))
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            history.upsert_daily_prices(db, [("2026-10-08", "2330", "TWSE", "台積電", 1.0, 0.0, 1, 1)])
            self.assertEqual(chips.fetch_tdcc_snapshot(db, text=self.CSV, log=lambda *_: None), "2026-10-08")
            self.assertEqual(chips.tdcc_dates(db), ["2026-10-08"])
            summary = chips.holder_summary(db, "2330")
            self.assertEqual(summary[0]["holders"], 100)
            self.assertAlmostEqual(summary[0]["big_pct"], 82.5)
            self.assertAlmostEqual(summary[0]["huge_pct"], 80.0)
            self.assertEqual(chips.holder_summary(db, "9999XX"), [])  # 非 daily_prices 代號不存
            # 同一週重複呼叫不重寫
            chips.fetch_tdcc_snapshot(db, text=self.CSV, log=lambda *_: None)
            self.assertEqual(len(chips.tdcc_dates(db)), 1)


class FakeClient:
    dates = ["20261002", "20260925"]

    def __init__(self):
        self.calls = []

    def query(self, ticker, ymd):
        self.calls.append((ticker, ymd))
        if ticker == "NONE":
            return []
        iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
        return [(iso, ticker, 16, 10, 100, 100.0)]


class TdccHistoryBackfillTests(unittest.TestCase):
    def test_parse_query_html(self):
        html = (
            '<tr><td align="center">15</td><td align="center">1,000,001以上</td>'
            '<td align="right">1,481</td><td align="right">21,966,711,289</td>'
            '<td align="right">84.70</td></tr>'
        )
        self.assertEqual(chips.parse_tdcc_query(html, "2026-09-18", "2330"),
                         [("2026-09-18", "2330", 15, 1481, 21966711289, 84.7)])

    def test_interrupt_and_resume(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            client, stop, seen = FakeClient(), threading.Event(), []

            def progress(done, total, label):
                seen.append((done, total))
                if done == 2:
                    stop.set()

            first = chips.backfill_tdcc_history(db, ["A", "B", "NONE"], stop, progress,
                                                delay_seconds=0, client=client)
            self.assertTrue(first["stopped"])
            self.assertEqual(first["done"], 2)
            self.assertEqual(first["total"], 6)
            resumed = FakeClient()
            second = chips.backfill_tdcc_history(db, ["A", "B", "NONE"], None, None,
                                                 delay_seconds=0, client=resumed)
            self.assertFalse(second["stopped"])
            self.assertEqual(second["done"], 6)
            self.assertEqual(len(resumed.calls), 4)  # 前兩筆不重查
            self.assertEqual(chips.pending_tdcc_pairs(db, ["A", "B", "NONE"], ["20261002", "20260925"]), [])
            self.assertEqual(len(chips.holder_summary(db, "A")), 2)

    def test_weeks_limits_dates(self):
        with tempfile.TemporaryDirectory() as d:
            client = FakeClient()
            r = chips.backfill_tdcc_history(Path(d) / "h.db", ["A"], None, None, delay_seconds=0,
                                            client=client, weeks=1)
            self.assertEqual((r["total"], client.calls), (1, [("A", "20261002")]))

    def test_holder_total_falls_back_to_level_16(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            conn = chips._connect(db)
            conn.execute("INSERT INTO tdcc_holders VALUES ('2026-01-02','X',16,500,10,100.0)")
            conn.commit()
            conn.close()
            self.assertEqual(chips.holder_summary(db, "X")[0]["holders"], 500)


class BigHolderChangeTests(unittest.TestCase):
    def test_delta_between_base_week_and_latest_week(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            conn = chips._connect(db)
            conn.executemany("INSERT INTO tdcc_holders VALUES (?,?,?,?,?,?)", [
                ("2026-09-25", "A", 12, 1, 1, 10.0), ("2026-09-25", "A", 15, 1, 1, 50.0),
                ("2026-10-08", "A", 12, 1, 1, 12.0), ("2026-10-08", "A", 15, 1, 1, 51.0),
                ("2026-10-08", "A", 5, 1, 1, 30.0),   # 小戶級距不算
                ("2026-10-08", "B", 15, 1, 1, 9.0),    # B 沒有基準週
            ])
            conn.commit()
            conn.close()
            result, dates = chips.big_holder_changes(db, ["A", "B"], "2026-10-05", "2026-10-13")
            self.assertEqual(dates, ("2026-09-25", "2026-10-08"))
            self.assertAlmostEqual(result["A"]["delta"], 3.0)
            self.assertNotIn("B", result)
            # 區間開始前沒有任何週資料 → 不顯示
            self.assertEqual(chips.big_holder_changes(db, ["A"], "2026-09-01", "2026-10-13"), ({}, None))


class GroupHolderChangeTests(unittest.TestCase):
    def test_share_weighted_delta_uses_same_members_both_weeks(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "h.db"
            conn = chips._connect(db)
            rows = []
            for date, a_big, b_big in (("2026-09-25", 40, 20), ("2026-10-08", 50, 20)):
                rows += [(date, "A", 12, 1, a_big, 0), (date, "A", 5, 1, 100 - a_big, 0),
                         (date, "B", 12, 1, b_big, 0), (date, "B", 5, 1, 100 - b_big, 0)]
            rows += [("2026-10-08", "C", 12, 1, 99, 0), ("2026-10-08", "C", 5, 1, 1, 0)]  # C 沒有基準週
            conn.executemany("INSERT INTO tdcc_holders VALUES (?,?,?,?,?,?)", rows)
            conn.commit()
            conn.close()
            result, dates = chips.group_big_holder_changes(
                db, {"G": {"A", "B", "C"}, "Empty": {"Z"}}, "2026-10-05", "2026-10-13")
            self.assertEqual(dates, ("2026-09-25", "2026-10-08"))
            self.assertAlmostEqual(result["G"]["base"], 30.0)    # (40+20)/200
            self.assertAlmostEqual(result["G"]["latest"], 35.0)  # (50+20)/200
            self.assertAlmostEqual(result["G"]["delta"], 5.0)
            self.assertEqual((result["G"]["covered"], result["G"]["total"]), (2, 3))
            self.assertNotIn("Empty", result)


if __name__ == "__main__":
    unittest.main()
