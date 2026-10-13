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


if __name__ == "__main__":
    unittest.main()
