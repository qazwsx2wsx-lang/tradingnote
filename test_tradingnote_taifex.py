import csv
import io
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import tradingnote_taifex as taifex
from tradingnote_taifex import (
    LARGE_TRADERS_ALL_CONTRACTS_MONTH,
    build_ssf_map,
    fetch_daily_futures_report,
    fetch_large_traders_history_range,
    get_front_month_sessions,
    get_large_traders_for_product,
    group_large_traders_all,
    large_traders_code_for_product,
)


class FetchDailyFuturesReportTests(unittest.TestCase):
    def test_calls_http_get_json_with_configured_url(self):
        sentinel = [{"Contract": "TX"}]
        with patch.object(taifex, "http_get_json", return_value=sentinel) as mock:
            result = fetch_daily_futures_report()
        self.assertEqual(result, sentinel)
        mock.assert_called_once_with(taifex.TAIFEX_DAILY_FUTURES_URL)


class GetFrontMonthSessionsTests(unittest.TestCase):
    def test_picks_min_contract_month_and_normalizes_fields(self):
        rows = [
            {"Contract": "TX", "ContractMonth(Week)": "202611", "TradingSession": "一般",
             "Open": "1,234", "Volume": "100"},
            {"Contract": "TX", "ContractMonth(Week)": "202611", "TradingSession": "盤後",
             "Open": "1,230", "Volume": "50"},
            {"Contract": "TX", "ContractMonth(Week)": "202612", "TradingSession": "一般",
             "Open": "1,300", "Volume": "10"},
            {"Contract": "MTX", "ContractMonth(Week)": "202611", "TradingSession": "一般"},
        ]
        result = get_front_month_sessions("TX", rows)
        self.assertEqual(set(result), {"一般", "盤後"})
        self.assertEqual(result["一般"]["open"], 1234.0)
        self.assertEqual(result["一般"]["contract_month"], "202611")
        self.assertEqual(result["盤後"]["open"], 1230.0)

    def test_excludes_spread_and_weekly_rows(self):
        rows = [
            {"Contract": "TX", "ContractMonth(Week)": "199901/199902", "TradingSession": "一般"},
            {"Contract": "TX", "ContractMonth(Week)": "199901W1", "TradingSession": "一般"},
            {"Contract": "TX", "ContractMonth(Week)": "202703", "TradingSession": "一般"},
        ]
        result = get_front_month_sessions("TX", rows)
        self.assertEqual(result["一般"]["contract_month"], "202703")

    def test_year_boundary_month_comparison(self):
        rows = [
            {"Contract": "TX", "ContractMonth(Week)": "202701", "TradingSession": "一般"},
            {"Contract": "TX", "ContractMonth(Week)": "202612", "TradingSession": "一般"},
        ]
        result = get_front_month_sessions("TX", rows)
        self.assertEqual(result["一般"]["contract_month"], "202612")

    def test_no_match_returns_empty_dict(self):
        rows = [{"Contract": "MTX", "ContractMonth(Week)": "202611", "TradingSession": "一般"}]
        self.assertEqual(get_front_month_sessions("TX", rows), {})


class GetLargeTradersForProductTests(unittest.TestCase):
    def _row(self, month, trader_type, contract="TX"):
        return {
            "Contract": contract, "ContractName": "臺股期貨", "SettlementMonth": month,
            "TypeOfTraders": trader_type, "Top5Buy": "100", "Top5Sell": "50",
            "Top10Buy": "150", "Top10Sell": "80", "OIOfMarket": "1000", "Date": "2026/10/20",
        }

    def test_sorts_real_months_before_all_contracts_month(self):
        rows = [
            self._row("202701", "0"),
            self._row(LARGE_TRADERS_ALL_CONTRACTS_MONTH, "0"),
            self._row("202612", "0"),
            self._row("202612", "1"),
        ]
        result = get_large_traders_for_product("TX", rows)
        months = [r["settlement_month"] for r in result]
        self.assertEqual(months, ["202612", "202701", LARGE_TRADERS_ALL_CONTRACTS_MONTH])
        self.assertFalse(result[0]["is_all_contracts"])
        self.assertTrue(result[-1]["is_all_contracts"])
        self.assertEqual(set(result[0]["by_type"]), {"所有交易人", "特定法人"})

    def test_no_match_returns_empty_list(self):
        rows = [self._row("202612", "0", contract="MTX")]
        self.assertEqual(get_large_traders_for_product("TX", rows), [])


class GroupLargeTradersAllTests(unittest.TestCase):
    def test_groups_by_contract_matches_direct_call(self):
        rows = [
            {"Contract": "TX", "ContractName": "臺股期貨", "SettlementMonth": "202612",
             "TypeOfTraders": "0", "Top5Buy": "1", "Top5Sell": "1", "Top10Buy": "1",
             "Top10Sell": "1", "OIOfMarket": "1", "Date": "2026/10/20"},
            {"Contract": "MTX", "ContractName": "小型臺指期貨", "SettlementMonth": "202612",
             "TypeOfTraders": "0", "Top5Buy": "2", "Top5Sell": "2", "Top10Buy": "2",
             "Top10Sell": "2", "OIOfMarket": "2", "Date": "2026/10/20"},
        ]
        grouped = group_large_traders_all(rows)
        self.assertEqual(set(grouped), {"TX", "MTX"})
        self.assertEqual(grouped["TX"], get_large_traders_for_product("TX", rows))
        self.assertEqual(grouped["MTX"], get_large_traders_for_product("MTX", rows))


class SsfMapTests(unittest.TestCase):
    def test_build_ssf_map_and_large_traders_code_for_product(self):
        ssf_rows = [
            {"Contract": "CDF", "StockCode": "2330", "StockName": "台積電",
             "UnderlyingStock": "台灣積體電路製造股份有限公司"},
        ]
        ssf_map = build_ssf_map(ssf_rows)
        self.assertEqual(ssf_map["CDF"]["stock_code"], "2330")
        self.assertEqual(large_traders_code_for_product("CDF", ssf_map), "CD")
        self.assertEqual(large_traders_code_for_product("TX", ssf_map), "TX")
        self.assertEqual(large_traders_code_for_product("BRF", ssf_map), "BRF")


class FetchLargeTradersHistoryRangeTests(unittest.TestCase):
    def _csv(self, rows):
        buf = io.StringIO()
        writer = csv.writer(buf)
        for row in rows:
            writer.writerow(row)
        return buf.getvalue()

    def test_parses_csv_and_skips_header_and_short_rows(self):
        text = self._csv([
            ["日期", "契約", "中文簡稱", "到期月份(週)", "身份", "前五大買方", "前五大賣方",
             "前十大買方", "前十大賣方", "全市場未沖銷契約數"],
            ["2026/10/20", "TX", "臺股期貨", "202612", "0", "100", "50", "150", "80", "10000"],
            ["2026/10/20", "TX", "臺股期貨", "999999", "0", "200", "100", "250", "130", "20000"],
            ["garbage"],
        ])
        with patch.object(taifex, "http_post_text", return_value=text):
            rows = fetch_large_traders_history_range(date(2026, 10, 20), date(2026, 10, 20))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ("2026-10-20", "TX", "202612", "0", "臺股期貨", 100, 50, 150, 80, 10000))
        self.assertEqual(rows[1], ("2026-10-20", "TX", "999999", "0", "臺股期貨", 200, 100, 250, 130, 20000))

    def test_rejects_html_error_page(self):
        html = "<html><body>查詢過久，請縮小查詢範圍</body></html>\n"
        with patch.object(taifex, "http_post_text", return_value=html):
            rows = fetch_large_traders_history_range(date(2026, 10, 20), date(2026, 10, 20))
        self.assertEqual(rows, [])


class DateChunksTests(unittest.TestCase):
    def test_splits_by_max_span_days(self):
        chunks = taifex._date_chunks(date(2026, 1, 1), date(2026, 3, 15), 60)
        self.assertEqual(chunks, [
            (date(2026, 1, 1), date(2026, 3, 1)),
            (date(2026, 3, 2), date(2026, 3, 15)),
        ])

    def test_inverted_range_returns_empty_list(self):
        chunks = taifex._date_chunks(date(2026, 3, 1), date(2026, 1, 1), 60)
        self.assertEqual(chunks, [])


class UpsertLargeTradersHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "taifex.db"

    def test_insert_or_replace_is_idempotent(self):
        row = ("2026-10-20", "TX", "202612", "0", "臺股期貨", 1, 1, 1, 1, 1000)
        taifex.upsert_large_traders_history(self.db, [row])
        taifex.upsert_large_traders_history(self.db, [row])
        series = taifex.get_large_traders_history_series(self.db, "TX", "202612", "0")
        self.assertEqual(len(series), 1)


class BackfillLargeTradersHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "taifex.db"

    def test_requests_forward_and_backward_gaps(self):
        today = date.today()
        existing = today - timedelta(days=10)
        taifex.upsert_large_traders_history(self.db, [
            (existing.isoformat(), "TX", "202612", "0", "臺股期貨", 1, 1, 1, 1, 1000),
        ])

        def fake_fetch(start, end):
            return [(end.isoformat(), "TX", "202612", "0", "臺股期貨", 1, 1, 1, 1, 1000)]

        with patch.object(taifex, "fetch_large_traders_history_range", side_effect=fake_fetch) as mock_fetch:
            result = taifex.backfill_large_traders_history(self.db, target_days=100, delay_seconds=0)

        self.assertGreaterEqual(mock_fetch.call_count, 2)
        self.assertGreaterEqual(result["chunks"], 2)
        self.assertGreater(result["rows"], 0)
        _min_d, max_d = taifex.get_large_traders_history_date_range(self.db)
        self.assertEqual(max_d, today.isoformat())

    def test_no_gap_when_already_covering_desired_range(self):
        today = date.today()
        taifex.upsert_large_traders_history(self.db, [
            ((today - timedelta(days=30)).isoformat(), "TX", "202612", "0", "臺股期貨", 1, 1, 1, 1, 1000),
            (today.isoformat(), "TX", "202612", "0", "臺股期貨", 1, 1, 1, 1, 1000),
        ])
        with patch.object(taifex, "fetch_large_traders_history_range") as mock_fetch:
            result = taifex.backfill_large_traders_history(self.db, target_days=5, delay_seconds=0)
        mock_fetch.assert_not_called()
        self.assertEqual(result, {"chunks": 0, "rows": 0})


if __name__ == "__main__":
    unittest.main()
