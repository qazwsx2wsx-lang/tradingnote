import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tradingnote_history as history


def _rows(date, market, n, close=10.0):
    return [(date, f"{i:04d}", market, "x", close, 0.0, 100, 1000.0) for i in range(1, n + 1)]


class TpexGapTests(unittest.TestCase):
    def test_missing_dates_and_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "h.db"
            history.upsert_daily_prices(db, _rows("2026-10-12", "TWSE", 600))
            history.upsert_daily_prices(db, _rows("2026-10-13", "TWSE", 600) + _rows("2026-10-13", "TPEX", 600))
            history.upsert_daily_prices(db, _rows("2026-10-14", "TWSE", 600))
            self.assertEqual(history.missing_tpex_dates(db), ["2026-10-14", "2026-10-12"])
            record = {"ticker": "9999", "name": "t", "market": "TPEX", "close": 5.0,
                      "change_pct": 1.0, "volume": 1, "trading_value": 5.0}
            records = [dict(record, ticker=str(1000 + i)) for i in range(600)]
            with patch.object(history, "fetch_tpex_historical_day", side_effect=[records, None]):
                result = history.backfill_tpex_gaps(db, delay_seconds=0)
            self.assertEqual(result, {"filled": 1, "total": 2, "failed": 1})
            self.assertEqual(history.missing_tpex_dates(db), ["2026-10-12"])


class AnalysisValuesTests(unittest.TestCase):
    def period(self, dates):
        return SimpleNamespace(end_date=dates[-1], actual_dates=tuple(dates), trading_days=len(dates),
                               sufficient=True, prior_dates=tuple(dates[:-1]))

    def price(self, date, close):
        return SimpleNamespace(date=date, close=close, change=0.0, trading_value=1.0)

    def test_no_trade_day_in_middle_is_carried(self):
        dates = ["d1", "d2", "d3", "d4"]
        history_rows = [("d3", 11.0, 1.0, 1), ("d2", None, 0.0, 0), ("d1", 10.0, 1.0, 1)]  # d2 收盤價空白
        change, _, _, actual, sufficient = history.analysis_values(
            self.price("d4", 12.0), history_rows, self.period(dates), 2)
        self.assertEqual((actual, sufficient), (4, True))
        self.assertAlmostEqual(change, 20.0)

    def test_missing_baseline_day_is_still_insufficient(self):
        dates = ["d1", "d2", "d3"]
        history_rows = [("d2", 10.0, 1.0, 1)]  # 基準日 d1、d3 都沒資料
        change, _, _, actual, sufficient = history.analysis_values(
            self.price("d3", 12.0), history_rows, self.period(dates), 1)
        self.assertIsNone(change)
        self.assertFalse(sufficient)


if __name__ == "__main__":
    unittest.main()
