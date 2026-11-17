"""daily_prices 的最高／最低價：寫入、不被沒有高低價的來源洗掉、舊表補欄位、回補。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tradingnote_history as history


def _rows(date, market, n, high_low=True):
    extra = (10.5, 9.5) if high_low else ()
    base = 1000 if market == "TWSE" else 5000  # 兩市場代號不能重複（主鍵是 date, ticker）
    return [(date, str(base + i), market, "x", 10.0, 0.0, 100, 1000.0) + extra for i in range(n)]


def _high_low(db, date, ticker):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT high, low FROM daily_prices WHERE date = ? AND ticker = ?",
                            (date, ticker)).fetchone()
    finally:
        conn.close()


class HighLowStorageTests(unittest.TestCase):
    def test_eight_column_upsert_keeps_existing_high_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "h.db"
            history.upsert_daily_prices(db, [("2026-10-29", "2330", "TWSE", "台積電", 100.0, 1.0, 10, 1e3, 101.0, 98.0)])
            history.upsert_daily_prices(db, [("2026-10-29", "2330", "TWSE", "台積電", 100.5, 1.5, 12, 2e3)])
            self.assertEqual(_high_low(db, "2026-10-29", "2330"), (101.0, 98.0))
            conn = sqlite3.connect(db)
            close, volume = conn.execute("SELECT close, volume FROM daily_prices").fetchone()
            conn.close()
            self.assertEqual((close, volume), (100.5, 12))
            # 新資料有高低價就覆寫
            history.upsert_daily_prices(db, [("2026-10-29", "2330", "TWSE", "台積電", 100.0, 1.0, 10, 1e3, 102.0, 97.0)])
            self.assertEqual(_high_low(db, "2026-10-29", "2330"), (102.0, 97.0))

    def test_old_table_gets_high_low_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "h.db"
            conn = sqlite3.connect(db)
            conn.execute("""CREATE TABLE daily_prices (date TEXT NOT NULL, ticker TEXT NOT NULL, market TEXT,
                name TEXT, close REAL, change_pct REAL, volume INTEGER, trading_value REAL,
                PRIMARY KEY (date, ticker))""")
            conn.execute("INSERT INTO daily_prices VALUES ('2026-10-28','2330','TWSE','t',1,0,1,1)")
            conn.commit()
            conn.close()
            history.upsert_daily_prices(db, [("2026-10-29", "2330", "TWSE", "t", 1.0, 0.0, 1, 1.0, 1.2, 0.8)])
            self.assertEqual(_high_low(db, "2026-10-28", "2330"), (None, None))
            self.assertEqual(_high_low(db, "2026-10-29", "2330"), (1.2, 0.8))

    def test_parse_tpex_daily_quotes_reads_high_low_by_header(self):
        fields = ["代號", "名稱", "收盤", "漲跌", "開盤", "最高", "最低", "均價", "成交股數", "成交金額(元)"]
        payload = {"stat": "ok", "tables": [{"fields": fields, "data": [
            ["6488", "環球晶", "110.00", "10.00 ", "101.00", "111.00", "100.50", "108.00", "1,000", "110,000"],
        ]}]}
        record = history.parse_tpex_daily_quotes(payload)[0]
        self.assertEqual((record["high"], record["low"]), (111.0, 100.5))


class BackfillHighLowTests(unittest.TestCase):
    def test_refetches_only_days_missing_high_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "h.db"
            history.upsert_daily_prices(db, _rows("2026-10-27", "TWSE", 600, high_low=False)
                                        + _rows("2026-10-27", "TPEX", 600))
            history.upsert_daily_prices(db, _rows("2026-10-28", "TWSE", 600)
                                        + _rows("2026-10-28", "TPEX", 600, high_low=False))
            history.upsert_daily_prices(db, _rows("2026-10-29", "TWSE", 600) + _rows("2026-10-29", "TPEX", 600))
            self.assertEqual(history.missing_high_low(db, 10), [("2026-10-28", "TPEX"), ("2026-10-27", "TWSE")])
            self.assertEqual(history.missing_high_low(db, 1), [])

            def records(market):
                base = 1000 if market == "TWSE" else 5000
                return [{"ticker": str(base + i), "name": "x", "market": market, "close": 10.0, "change_pct": 0.0,
                         "volume": 100, "trading_value": 1000.0, "high": 11.0, "low": 9.0} for i in range(600)]

            with patch.object(history, "fetch_tpex_historical_day", return_value=records("TPEX")) as tpex, \
                    patch.object(history, "fetch_twse_historical_day", return_value=records("TWSE")) as twse:
                result = history.backfill_high_low(db, 10, delay_seconds=0)
            self.assertEqual(result, {"filled": 2, "total": 2, "failed": 0})
            tpex.assert_called_once_with("20261028")
            twse.assert_called_once_with("20261027")
            self.assertEqual(history.missing_high_low(db, 10), [])
            self.assertEqual(_high_low(db, "2026-10-27", "1000"), (11.0, 9.0))


if __name__ == "__main__":
    unittest.main()
