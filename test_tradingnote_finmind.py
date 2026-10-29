import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import tradingnote_finmind as finmind
from tradingnote_cache import TTLCache
from tradingnote_http import PriceFetchError


class ResetCacheTestCase(unittest.TestCase):
    """Resets finmind's module-level mutable caches/globals before each test."""

    def setUp(self):
        finmind._dataset_cache = TTLCache(finmind.DATASET_CACHE_TTL_SECONDS)
        finmind._call_timestamps = []


class FetchDatasetTests(ResetCacheTestCase):
    def test_success_builds_url_and_returns_data(self):
        mock = Mock(return_value={"msg": "success", "data": [{"date": "2026-10-01", "close": 100}]})
        with patch.object(finmind, "http_get_json", mock):
            result = finmind._fetch_dataset("TaiwanStockPER", "2330", "tok", lookback_days=10)
        self.assertEqual(result, [{"date": "2026-10-01", "close": 100}])
        url = mock.call_args[0][0]
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["dataset"], ["TaiwanStockPER"])
        self.assertEqual(query["data_id"], ["2330"])
        self.assertEqual(query["token"], ["tok"])
        self.assertIn("start_date", query)
        self.assertIn("end_date", query)

    def test_raises_on_non_success_msg(self):
        mock = Mock(return_value={"msg": "error!"})
        with patch.object(finmind, "http_get_json", mock):
            with self.assertRaises(PriceFetchError):
                finmind._fetch_dataset("TaiwanStockPER", "2330", None)

    def test_cache_hit_avoids_second_call(self):
        mock = Mock(return_value={"msg": "success", "data": []})
        with patch.object(finmind, "http_get_json", mock):
            finmind._fetch_dataset("TaiwanStockPER", "2330", None, lookback_days=10)
            finmind._fetch_dataset("TaiwanStockPER", "2330", None, lookback_days=10)
        self.assertEqual(mock.call_count, 1)

    def test_cache_miss_on_different_lookback(self):
        mock = Mock(return_value={"msg": "success", "data": []})
        with patch.object(finmind, "http_get_json", mock):
            finmind._fetch_dataset("TaiwanStockPER", "2330", None, lookback_days=10)
            finmind._fetch_dataset("TaiwanStockPER", "2330", None, lookback_days=20)
        self.assertEqual(mock.call_count, 2)


class CallCountTests(ResetCacheTestCase):
    def test_tracks_only_real_calls_not_cache_hits(self):
        mock = Mock(return_value={"msg": "success", "data": []})
        with patch.object(finmind, "http_get_json", mock):
            finmind._fetch_dataset("TaiwanStockPER", "2330", None, lookback_days=10)
            finmind._fetch_dataset("TaiwanStockPER", "2330", None, lookback_days=10)
        self.assertEqual(finmind.get_call_count(), 1)

    def test_prunes_expired_timestamps(self):
        finmind._call_timestamps = [time.time() - 4000]
        self.assertEqual(finmind.get_call_count(window_seconds=3600), 0)
        self.assertEqual(len(finmind._call_timestamps), 0)


class FetchValuationTests(ResetCacheTestCase):
    def test_tpex_bypasses_finmind(self):
        http_mock = Mock()
        with patch.object(finmind, "http_get_json", http_mock), \
             patch.object(finmind, "get_tpex_valuation", return_value={"sentinel": True}) as tpex_mock:
            result = finmind.fetch_valuation("5380", None, market="TPEX")
        self.assertEqual(result, {"sentinel": True})
        tpex_mock.assert_called_once_with("5380")
        http_mock.assert_not_called()

    def test_twse_maps_fields_and_picks_latest_row(self):
        rows = [
            {"date": "2026-10-01", "PER": 12.0, "PBR": 1.0, "dividend_yield": 2.0},
            {"date": "2026-10-02", "PER": 15.2, "PBR": 2.1, "dividend_yield": 3.3},
        ]
        with patch.object(finmind, "_fetch_dataset", return_value=rows):
            result = finmind.fetch_valuation("2330", None)
        self.assertEqual(
            result, {"date": "2026-10-02", "per": 15.2, "pbr": 2.1, "dividend_yield": 3.3}
        )

    def test_no_rows_returns_none(self):
        with patch.object(finmind, "_fetch_dataset", return_value=[]):
            self.assertIsNone(finmind.fetch_valuation("2330", None))


class FetchMarginCostEstimateTests(ResetCacheTestCase):
    def _patch_datasets(self, margin_rows, price_rows):
        def side_effect(dataset, ticker, token, lookback_days=10):
            if dataset == "TaiwanStockMarginPurchaseShortSale":
                return margin_rows
            if dataset == "TaiwanStockPrice":
                return price_rows
            raise AssertionError(f"unexpected dataset {dataset}")
        return patch.object(finmind, "_fetch_dataset", side_effect=side_effect)

    def test_moving_weighted_average_cost(self):
        margin_rows = [
            {"date": "d1", "MarginPurchaseTodayBalance": 100},
            {"date": "d2", "MarginPurchaseTodayBalance": 150},
            {"date": "d3", "MarginPurchaseTodayBalance": 120},
        ]
        price_rows = [
            {"date": "d1", "close": 10},
            {"date": "d2", "close": 12},
            {"date": "d3", "close": 11},
        ]
        with self._patch_datasets(margin_rows, price_rows):
            result = finmind.fetch_margin_cost_estimate("2330", None)
        self.assertEqual(result["date"], "d3")
        self.assertEqual(result["balance"], 120)
        self.assertAlmostEqual(result["cost"], 1600 / 150)

    def test_balance_ending_at_zero_returns_none(self):
        margin_rows = [
            {"date": "d1", "MarginPurchaseTodayBalance": 100},
            {"date": "d2", "MarginPurchaseTodayBalance": 0},
        ]
        price_rows = [{"date": "d1", "close": 10}, {"date": "d2", "close": 9}]
        with self._patch_datasets(margin_rows, price_rows):
            self.assertIsNone(finmind.fetch_margin_cost_estimate("2330", None))

    def test_missing_price_on_increase_day_skips_cost_update(self):
        margin_rows = [
            {"date": "d1", "MarginPurchaseTodayBalance": 100},
            {"date": "d2", "MarginPurchaseTodayBalance": 150},
        ]
        price_rows = [{"date": "d1", "close": 10}]  # d2 has no matching price
        with self._patch_datasets(margin_rows, price_rows):
            result = finmind.fetch_margin_cost_estimate("2330", None)
        self.assertEqual(result, {"date": "d2", "cost": 10, "balance": 150})

    def test_no_margin_or_price_rows_returns_none(self):
        with self._patch_datasets([], []):
            self.assertIsNone(finmind.fetch_margin_cost_estimate("2330", None))


class InstitutionalInvestorsHistoryTests(ResetCacheTestCase):
    ROWS = [
        {"date": "d1", "name": "Foreign_Investor", "buy": 100, "sell": 40},
        {"date": "d1", "name": "Foreign_Dealer_Self", "buy": 10, "sell": 5},
        {"date": "d1", "name": "Investment_Trust", "buy": 20, "sell": 30},
        {"date": "d1", "name": "Dealer_self", "buy": 5, "sell": 2},
        {"date": "d1", "name": "Dealer_Hedging", "buy": 1, "sell": 1},
    ]

    def test_history_buckets_into_three_groups(self):
        with patch.object(finmind, "_fetch_dataset", return_value=self.ROWS):
            result = finmind.fetch_institutional_investors_history("2330", None)
        self.assertEqual(result["dates"], ["d1"])
        self.assertEqual(result["series"]["外資"], [65])
        self.assertEqual(result["series"]["投信"], [-10])
        self.assertEqual(result["series"]["自營商"], [3])

    def test_detailed_history_keeps_five_buckets_separate(self):
        with patch.object(finmind, "_fetch_dataset", return_value=self.ROWS):
            result = finmind.fetch_institutional_investors_detailed_history("2330", None)
        self.assertEqual(result["series"]["外資"], [60])
        self.assertEqual(result["series"]["外資自營商"], [5])
        self.assertEqual(result["series"]["投信"], [-10])
        self.assertEqual(result["series"]["自營商(自行)"], [3])
        self.assertEqual(result["series"]["自營商(避險)"], [0])

    def test_no_rows_returns_none(self):
        with patch.object(finmind, "_fetch_dataset", return_value=[]):
            self.assertIsNone(finmind.fetch_institutional_investors_history("2330", None))
            self.assertIsNone(finmind.fetch_institutional_investors_detailed_history("2330", None))


class SecuritiesLendingSummaryTests(ResetCacheTestCase):
    def test_weighted_average_fee_rate(self):
        rows = [
            {"date": "d1", "volume": 100, "fee_rate": 0.5},
            {"date": "d1", "volume": 300, "fee_rate": 0.9},
        ]
        with patch.object(finmind, "_fetch_dataset", return_value=rows):
            result = finmind.fetch_securities_lending_summary("2330", None)
        self.assertEqual(result["date"], "d1")
        self.assertEqual(result["volume"], 400)
        self.assertAlmostEqual(result["avg_fee_rate"], 0.8)

    def test_zero_volume_day_guards_division_by_zero(self):
        rows = [
            {"date": "d1", "volume": 100, "fee_rate": 0.5},
            {"date": "d2", "volume": 0, "fee_rate": 0.3},
        ]
        with patch.object(finmind, "_fetch_dataset", return_value=rows):
            result = finmind.fetch_securities_lending_summary("2330", None)
        self.assertEqual(result["date"], "d2")
        self.assertEqual(result["volume"], 0)
        self.assertIsNone(result["avg_fee_rate"])

    def test_no_rows_returns_none(self):
        with patch.object(finmind, "_fetch_dataset", return_value=[]):
            self.assertIsNone(finmind.fetch_securities_lending_summary("2330", None))


class ShortSaleBalanceTests(ResetCacheTestCase):
    def test_change_from_previous_balance(self):
        rows = [{"date": "d1", "SBLShortSalesCurrentDayBalance": 1000,
                  "SBLShortSalesPreviousDayBalance": 800}]
        with patch.object(finmind, "_fetch_dataset", return_value=rows):
            result = finmind.fetch_short_sale_balance("2330", None)
        self.assertEqual(result["balance"], 1000)
        self.assertEqual(result["change"], 200)

    def test_missing_previous_balance_defaults_to_zero(self):
        rows = [{"date": "d1", "SBLShortSalesCurrentDayBalance": 1000}]
        with patch.object(finmind, "_fetch_dataset", return_value=rows):
            result = finmind.fetch_short_sale_balance("2330", None)
        self.assertEqual(result["change"], 1000)

    def test_none_current_balance_returns_none(self):
        rows = [{"date": "d1", "SBLShortSalesCurrentDayBalance": None}]
        with patch.object(finmind, "_fetch_dataset", return_value=rows):
            self.assertIsNone(finmind.fetch_short_sale_balance("2330", None))

    def test_no_rows_returns_none(self):
        with patch.object(finmind, "_fetch_dataset", return_value=[]):
            self.assertIsNone(finmind.fetch_short_sale_balance("2330", None))


class FetchStockPriceHistoryTests(ResetCacheTestCase):
    def test_change_pct_computed_from_spread(self):
        rows = [{"date": "d1", "close": 11, "spread": 1, "Trading_Volume": 100,
                 "Trading_money": 1100}]
        with patch.object(finmind, "_fetch_dataset", return_value=rows):
            result = finmind.fetch_stock_price_history("2330", None, 10)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["change_pct"], 10.0)

    def test_none_spread_gives_none_change_pct(self):
        rows = [{"date": "d1", "close": 11, "spread": None}]
        with patch.object(finmind, "_fetch_dataset", return_value=rows):
            result = finmind.fetch_stock_price_history("2330", None, 10)
        self.assertIsNone(result[0]["change_pct"])

    def test_none_close_row_is_skipped(self):
        rows = [{"date": "d1", "close": None, "spread": None},
                {"date": "d2", "close": 11, "spread": 1}]
        with patch.object(finmind, "_fetch_dataset", return_value=rows):
            result = finmind.fetch_stock_price_history("2330", None, 10)
        self.assertEqual([r["date"] for r in result], ["d2"])


class BackfillTpexHistoryViaFinmindTests(ResetCacheTestCase):
    DIRECTORY = {
        "5380": {"name": "A", "industry": "x", "market": "TPEX"},
        "5381": {"name": "B", "industry": "x", "market": "TPEX"},
    }

    def test_quota_exhaustion_stops_early(self):
        call_count_mock = Mock(side_effect=[0, finmind.FINMIND_HOURLY_LIMIT])
        fetch_mock = Mock(return_value=[
            {"date": "2026-10-01", "close": 10, "change_pct": 1.0, "volume": 100, "trading_value": 1000}
        ])
        upsert_mock = Mock()
        with patch.object(finmind, "get_industry_directory", return_value=self.DIRECTORY), \
             patch.object(finmind, "get_ticker_history_day_counts", return_value={}), \
             patch.object(finmind, "get_call_count", call_count_mock), \
             patch.object(finmind, "fetch_stock_price_history", fetch_mock), \
             patch.object(finmind, "upsert_daily_prices", upsert_mock):
            result = finmind.backfill_tpex_history_via_finmind(
                "unused.db", "tok", target_days=120, delay_seconds=0
            )
        self.assertEqual(result, {"done": 1, "total": 2, "newly_fetched": 1,
                                    "stopped_reason": "quota_exhausted"})
        fetch_mock.assert_called_once_with("5380", "tok", unittest.mock.ANY)
        upsert_mock.assert_called_once()

    def test_skips_tickers_already_sufficient(self):
        fetch_mock = Mock()
        with patch.object(finmind, "get_industry_directory", return_value=self.DIRECTORY), \
             patch.object(finmind, "get_ticker_history_day_counts",
                           return_value={"5380": 200, "5381": 200}), \
             patch.object(finmind, "get_call_count", return_value=0), \
             patch.object(finmind, "fetch_stock_price_history", fetch_mock), \
             patch.object(finmind, "upsert_daily_prices", Mock()):
            result = finmind.backfill_tpex_history_via_finmind(
                "unused.db", "tok", target_days=120, delay_seconds=0
            )
        fetch_mock.assert_not_called()
        self.assertEqual(result["done"], 2)
        self.assertEqual(result["newly_fetched"], 0)
        self.assertIsNone(result["stopped_reason"])

    def test_no_tpex_tickers_returns_all_zero(self):
        with patch.object(finmind, "get_industry_directory", return_value={}):
            result = finmind.backfill_tpex_history_via_finmind("unused.db", "tok")
        self.assertEqual(result, {"done": 0, "total": 0, "newly_fetched": 0, "stopped_reason": None})


if __name__ == "__main__":
    unittest.main()
