import unittest
from unittest.mock import patch, call

from tradingnote_core import PriceInfo
from tradingnote_http import PriceFetchError
import tradingnote_institutional as institutional
from tradingnote_institutional import (
    InstitutionalTickerFlow,
    aggregate_institutional_by_group,
    fetch_tpex_institutional,
    fetch_twse_institutional,
)


def _twse_row(ticker="2330", name="台積電", foreign_a="100", foreign_b="50",
              trust="30", dealer="20", extra_len=19):
    row = [ticker, name, "0", "0", foreign_a, "0", "0", foreign_b, "0", "0",
           trust, dealer]
    while len(row) < extra_len:
        row.append("0")
    return row


class FetchTwseInstitutionalTests(unittest.TestCase):
    def test_parses_rows_and_sums_foreign_components(self):
        payload = {"stat": "OK", "date": "20261020", "data": [_twse_row()]}
        with patch.object(institutional, "http_get_json", return_value=payload):
            result = fetch_twse_institutional()
        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual(row.ticker, "2330")
        self.assertEqual(row.name, "台積電")
        self.assertEqual(row.date, "2026-10-20")
        self.assertEqual(row.market, "TWSE")
        self.assertEqual(row.foreign_net, 150)
        self.assertEqual(row.trust_net, 30)
        self.assertEqual(row.dealer_net, 20)

    def test_skips_short_rows_and_blank_ticker_rows(self):
        short_row = ["2330", "台積電"]
        blank_ticker_row = _twse_row(ticker="")
        payload = {
            "stat": "OK",
            "date": "20261020",
            "data": [short_row, blank_ticker_row, _twse_row(ticker="2317")],
        }
        with patch.object(institutional, "http_get_json", return_value=payload):
            result = fetch_twse_institutional()
        self.assertEqual([row.ticker for row in result], ["2317"])

    def test_raises_on_bad_stat(self):
        payload = {"stat": "很抱歉，沒有符合條件的資料!"}
        with patch.object(institutional, "http_get_json", return_value=payload):
            with self.assertRaises(PriceFetchError):
                fetch_twse_institutional()


class FetchTpexInstitutionalTests(unittest.TestCase):
    def _row(self, foreign_key, ticker="5380"):
        return {
            "SecuritiesCompanyCode": ticker,
            "CompanyName": "測試上櫃",
            "Date": "1151020",
            foreign_key: "40",
            "SecuritiesInvestmentTrustCompanies-Difference": "15",
            "Dealers-Difference": "-5",
        }

    def test_parses_roc_date_and_primary_foreign_key(self):
        rows = [self._row("ForeignInvestorsInclude MainlandAreaInvestors-Difference")]
        with patch.object(institutional, "http_get_json", return_value=rows):
            result = fetch_tpex_institutional()
        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual(row.date, "2026-10-20")
        self.assertEqual(row.market, "TPEX")
        self.assertEqual(row.foreign_net, 40)
        self.assertEqual(row.trust_net, 15)
        self.assertEqual(row.dealer_net, -5)

    def test_parses_fallback_foreign_key_variant(self):
        rows = [self._row(
            "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference"
        )]
        with patch.object(institutional, "http_get_json", return_value=rows):
            result = fetch_tpex_institutional()
        self.assertEqual(result[0].foreign_net, 40)

    def test_blank_ticker_rows_skipped(self):
        rows = [self._row("Dealers-Difference", ticker="")]
        with patch.object(institutional, "http_get_json", return_value=rows):
            result = fetch_tpex_institutional()
        self.assertEqual(result, [])


class RocCompactToIsoTests(unittest.TestCase):
    def test_valid_seven_digit_roc_date(self):
        self.assertEqual(institutional._roc_compact_to_iso("1151020"), "2026-10-20")

    def test_wrong_length_returns_empty_string(self):
        self.assertEqual(institutional._roc_compact_to_iso("115102"), "")
        self.assertEqual(institutional._roc_compact_to_iso("11510200"), "")

    def test_non_digit_returns_empty_string(self):
        self.assertEqual(institutional._roc_compact_to_iso("abcdefg"), "")

    def test_none_returns_empty_string(self):
        self.assertEqual(institutional._roc_compact_to_iso(None), "")


class HttpGetJsonRetryTests(unittest.TestCase):
    def test_retries_once_then_succeeds(self):
        mock = unittest.mock.Mock(side_effect=[PriceFetchError("boom"), {"ok": True}])
        with patch.object(institutional, "http_get_json", mock):
            result = institutional._http_get_json_retry("http://example/url")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock.call_count, 2)
        self.assertEqual(
            mock.call_args_list,
            [call("http://example/url", timeout=20), call("http://example/url", timeout=30)],
        )

    def test_raises_after_second_failure(self):
        mock = unittest.mock.Mock(side_effect=[PriceFetchError("boom"), PriceFetchError("boom2")])
        with patch.object(institutional, "http_get_json", mock):
            with self.assertRaises(PriceFetchError):
                institutional._http_get_json_retry("http://example/url")
        self.assertEqual(mock.call_count, 2)


class AggregateInstitutionalByGroupTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "1111": PriceInfo("1111", "A", None, None, None, 100, 1, 1000,
                               "2026-10-20", "TWSE", 10000),
            "2222": PriceInfo("2222", "B", None, None, None, 50, 1, 1000,
                               "2026-10-20", "TWSE", 10000),
        }

    def test_sums_multiple_tickers_in_one_group(self):
        rows = [
            InstitutionalTickerFlow("1111", "A", "2026-10-20", "TWSE", 10, 5, -2),
            InstitutionalTickerFlow("2222", "B", "2026-10-20", "TWSE", 20, -3, 1),
        ]
        result = aggregate_institutional_by_group(rows, {"測試": ["1111", "2222"]}, self.snapshot)
        self.assertEqual(len(result), 1)
        group = result[0]
        self.assertEqual(group.covered_stocks, 2)
        self.assertAlmostEqual(group.foreign_value, 10 * 100 + 20 * 50)
        self.assertAlmostEqual(group.trust_value, 5 * 100 + -3 * 50)
        self.assertAlmostEqual(group.dealer_value, -2 * 100 + 1 * 50)
        self.assertAlmostEqual(
            group.total_value, group.foreign_value + group.trust_value + group.dealer_value
        )

    def test_partial_coverage_only_counts_matching_member(self):
        rows = [
            InstitutionalTickerFlow("1111", "A", "2026-10-20", "TWSE", 10, 5, -2),
            InstitutionalTickerFlow("2222", "B", "2026-10-19", "TWSE", 20, -3, 1),  # mismatched date
        ]
        result = aggregate_institutional_by_group(rows, {"測試": ["1111", "2222"]}, self.snapshot)
        self.assertEqual(len(result), 1)
        group = result[0]
        self.assertEqual(group.covered_stocks, 1)
        self.assertAlmostEqual(group.foreign_value, 10 * 100)

    def test_member_missing_from_snapshot_excluded_without_error(self):
        rows = [
            InstitutionalTickerFlow("1111", "A", "2026-10-20", "TWSE", 10, 5, -2),
            InstitutionalTickerFlow("3333", "C", "2026-10-20", "TWSE", 99, 99, 99),
        ]
        result = aggregate_institutional_by_group(rows, {"測試": ["1111", "3333"]}, self.snapshot)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].covered_stocks, 1)

    def test_group_with_no_covered_members_excluded_from_results(self):
        rows = [
            InstitutionalTickerFlow("2222", "B", "2026-10-19", "TWSE", 20, -3, 1),  # mismatched date
        ]
        result = aggregate_institutional_by_group(rows, {"測試": ["2222"]}, self.snapshot)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
