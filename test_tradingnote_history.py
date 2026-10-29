import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import tradingnote_history as history
from tradingnote_core import PriceInfo
from tradingnote_history import _cross_sectional_percentile


class CrossSectionalPercentileTests(unittest.TestCase):
    def test_no_ties_matches_rank_formula(self):
        values = [10, 20, 30, 40, 50]
        self.assertEqual(_cross_sectional_percentile(10, values), 0.0)
        self.assertEqual(_cross_sectional_percentile(50, values), 100.0)
        self.assertEqual(_cross_sectional_percentile(30, values), 50.0)

    def test_ties_use_average_rank(self):
        values = [10, 20, 20, 20, 50]
        self.assertAlmostEqual(_cross_sectional_percentile(20, values), 50.0)

    def test_single_value_returns_fifty(self):
        self.assertEqual(_cross_sectional_percentile(42, [42]), 50.0)

    def test_none_value_or_empty_values_returns_none(self):
        self.assertIsNone(_cross_sectional_percentile(None, [1, 2, 3]))
        self.assertIsNone(_cross_sectional_percentile(5, []))


def _price(ticker, name, close, volume, trading_date, trading_value, change=None):
    return PriceInfo(ticker, name, None, None, None, close, change, volume,
                      trading_date, "TWSE", trading_value)


class ComputeGroupFlowTests(unittest.TestCase):
    """多成分股、含歷史不足成員、含群組外股票的資金流向聚合。"""

    D0, D1, D2, D3, D4, D5 = (
        "2026-09-25", "2026-09-26", "2026-09-27", "2026-09-28", "2026-09-29", "2026-09-30",
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "history.db"

        rows = []
        # 1111：完整 6 天（可算出變化率與量比）。
        for d, close, volume in zip(
            (self.D0, self.D1, self.D2, self.D3, self.D4, self.D5),
            (95, 100, 101, 102, 103, 110),
            (1000, 1000, 1000, 1000, 1000, 1500),
        ):
            rows.append((d, "1111", "TWSE", "A", close, 0.0, volume, 100000))
        # 2222：只有最後兩天（歷史不足，無法算出變化率／量比)。
        rows.append((self.D4, "2222", "TWSE", "B", 50, 0.0, 500, 20000))
        rows.append((self.D5, "2222", "TWSE", "B", 55, 0.0, 500, 20000))
        # 3333：完整 6 天，跌幅－5%、量比 0.5。
        for d, close, volume in zip(
            (self.D0, self.D1, self.D2, self.D3, self.D4, self.D5),
            (200, 200, 200, 200, 200, 190),
            (2000, 2000, 2000, 2000, 2000, 1000),
        ):
            rows.append((d, "3333", "TWSE", "C", close, 0.0, volume, 300000))
        # 4444：只有截止日當天（完全沒有歷史）。
        rows.append((self.D5, "4444", "TWSE", "D", 70, 0.0, 800, 50000))
        history.upsert_daily_prices(self.db, rows)

        self.snapshot = {
            "1111": _price("1111", "A", 110, 1500, self.D5, 100000),
            "2222": _price("2222", "B", 55, 500, self.D5, 20000),
            "3333": _price("3333", "C", 190, 1000, self.D5, 300000),
            "4444": _price("4444", "D", 70, 800, self.D5, 50000),
        }
        self.universe = {"1111", "2222", "3333", "4444"}

    def test_weighted_change_and_capital_share_with_outside_denominator(self):
        groups = {"G1": {"1111", "2222"}}
        results = history.compute_group_flow(
            self.db, self.snapshot, groups, avg_days=5, market_universe=self.universe
        )
        self.assertEqual(len(results), 1)
        g1 = results[0]
        self.assertEqual(g1.stock_count, 2)
        # 只有 1111 足夠歷史能算出變化率；2222 歷史不足被排除在加權平均外。
        self.assertAlmostEqual(g1.avg_change_pct, 10.0)
        total_market_value = 100000 + 20000 + 300000 + 50000
        self.assertAlmostEqual(g1.capital_share_pct, 120000 / total_market_value * 100)
        # actual_days 取群組內最小值：1111 足夠(5)，2222 只有 2 天。
        self.assertEqual(g1.actual_days, 2)
        self.assertEqual(g1.required_days, 5)

    def test_group_with_only_insufficient_member_has_no_avg_change(self):
        groups = {"G1": {"2222"}}
        results = history.compute_group_flow(
            self.db, self.snapshot, groups, avg_days=5, market_universe=self.universe
        )
        self.assertEqual(len(results), 1)
        g1 = results[0]
        self.assertIsNone(g1.avg_change_pct)
        self.assertEqual(g1.stock_count, 1)
        self.assertLess(g1.actual_days, g1.required_days)

    def test_volume_ratio_from_baseline_average(self):
        groups = {"G1": {"1111"}, "G2": {"3333"}}
        results = history.compute_group_flow(
            self.db, self.snapshot, groups, avg_days=5, market_universe=self.universe
        )
        by_group = {r.industry: r for r in results}
        self.assertAlmostEqual(by_group["G1"].volume_ratio, 1500 / 1000)
        self.assertAlmostEqual(by_group["G2"].volume_ratio, 1000 / 2000)

    def test_composite_score_blends_percentiles_and_propagates_none(self):
        groups = {"G1": {"1111", "2222"}, "G2": {"3333"}, "G3": {"4444"}}
        results = history.compute_group_flow(
            self.db, self.snapshot, groups, avg_days=5, market_universe=self.universe
        )
        by_group = {r.industry: r for r in results}

        popularity_values = [r.capital_share_pct for r in results]
        for r in results:
            self.assertAlmostEqual(
                r.popularity_score,
                _cross_sectional_percentile(r.capital_share_pct, popularity_values),
            )

        # G3（4444）完全沒有歷史：變化率與量比都是 None，依規則 composite_score 也必須是 None，
        # 即便 popularity_score（只需今日成交金額）仍然算得出來。
        g3 = by_group["G3"]
        self.assertIsNone(g3.avg_change_pct)
        self.assertIsNone(g3.volume_ratio)
        self.assertIsNotNone(g3.popularity_score)
        self.assertIsNone(g3.composite_score)

        g1, g2 = by_group["G1"], by_group["G2"]
        self.assertIsNotNone(g1.composite_score)
        self.assertIsNotNone(g2.composite_score)


class ComputeIndustryFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "history.db"

    def test_uses_industry_map_as_groups_and_market_universe(self):
        snapshot = {
            "1111": _price("1111", "A", 110, 100, "2026-10-20", 100000, change=10),
            "2222": _price("2222", "B", 55, 50, "2026-10-20", 50000, change=5),
            "3333": _price("3333", "C", 20, 10, "2026-10-20", 20000, change=-1),
        }
        industry_map = {"1111": "電子零組件業", "2222": "電子零組件業", "3333": "金融保險"}
        with patch.object(history, "get_industry_map", return_value=industry_map):
            results = history.compute_industry_flow(self.db, snapshot, avg_days=1)
        by_industry = {r.industry: r for r in results}
        self.assertEqual(set(by_industry), {"電子零組件業", "金融保險"})
        self.assertEqual(by_industry["電子零組件業"].stock_count, 2)
        self.assertEqual(by_industry["金融保險"].stock_count, 1)
        total = 100000 + 50000 + 20000
        self.assertAlmostEqual(
            by_industry["電子零組件業"].capital_share_pct, (100000 + 50000) / total * 100
        )


class FetchTwseHistoricalDayTests(unittest.TestCase):
    def _payload(self, rows):
        return {
            "stat": "OK",
            "tables": [{"fields": ["證券代號", "證券名稱"], "data": rows}],
        }

    def test_decodes_negative_sign_and_computes_change_pct(self):
        row = ["2330", "台積電", "1000000", "5000", "500000000", "100", "105", "99", "103",
               '<p style="color:green">-</p>', "2"]
        payload = self._payload([row])
        with patch.object(history, "http_get_json", return_value=payload):
            records = history.fetch_twse_historical_day("20261020")
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["close"], 103.0)
        self.assertAlmostEqual(record["change"], -2.0)
        self.assertAlmostEqual(record["change_pct"], -2.0 / 105 * 100)
        self.assertEqual(record["volume"], 1000000)
        self.assertEqual(record["trading_value"], 500000000.0)

    def test_non_ok_stat_returns_none(self):
        with patch.object(history, "http_get_json", return_value={"stat": "BAD"}):
            self.assertIsNone(history.fetch_twse_historical_day("20261020"))

    def test_price_fetch_error_returns_none(self):
        from tradingnote_http import PriceFetchError
        with patch.object(history, "http_get_json", side_effect=PriceFetchError("boom")):
            self.assertIsNone(history.fetch_twse_historical_day("20261020"))


class BackfillTwseHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "history.db"

    def test_skips_existing_dates_and_only_fetches_missing_ones(self):
        yesterday = date.today() - timedelta(days=1)
        existing = [yesterday - timedelta(days=i) for i in range(3)]
        history.upsert_daily_prices(self.db, [
            (d.isoformat(), "1111", "TWSE", "A", 100, 0.0, 100, 1000) for d in existing
        ])
        fetch_mock = Mock(return_value=[
            {"ticker": "1111", "name": "A", "market": "TWSE", "close": 100,
             "change_pct": 0.0, "volume": 100, "trading_value": 1000}
        ])
        with patch.object(history, "fetch_twse_historical_day", fetch_mock):
            done = history.backfill_twse_history(
                self.db, target_days=5, lookback_cap_days=10, delay_seconds=0
            )
        self.assertEqual(done, 5)
        self.assertEqual(fetch_mock.call_count, 2)

    def test_rerun_is_idempotent_no_duplicate_fetches(self):
        fetch_mock = Mock(return_value=[
            {"ticker": "1111", "name": "A", "market": "TWSE", "close": 100,
             "change_pct": 0.0, "volume": 100, "trading_value": 1000}
        ])
        with patch.object(history, "fetch_twse_historical_day", fetch_mock):
            history.backfill_twse_history(
                self.db, target_days=3, lookback_cap_days=10, delay_seconds=0
            )
            fetch_mock.reset_mock()
            history.backfill_twse_history(
                self.db, target_days=3, lookback_cap_days=10, delay_seconds=0
            )
        fetch_mock.assert_not_called()

    def test_stops_at_lookback_cap_when_all_holidays(self):
        fetch_mock = Mock(return_value=None)
        with patch.object(history, "fetch_twse_historical_day", fetch_mock):
            done = history.backfill_twse_history(
                self.db, target_days=5, lookback_cap_days=10, delay_seconds=0
            )
        self.assertEqual(done, 0)
        self.assertEqual(fetch_mock.call_count, 10)


if __name__ == "__main__":
    unittest.main()
