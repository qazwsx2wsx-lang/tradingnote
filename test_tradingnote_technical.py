import unittest
from tradingnote_technical import (
    TechnicalPriceBar,
    calculate_indicators,
    normalize_price_rows,
    _calculate_kd,
    _calculate_macd,
    _calculate_rsi,
    _ema,
    _simple_moving_average,
)

CLOSES = [10, 10.5, 11, 10.8, 11.2, 11.5, 11.3, 11.8, 12, 11.7, 12.2, 12.5, 12.3, 12.8, 13]


class TechnicalTests(unittest.TestCase):
    def test_flat_prices_and_warmup(self):
        rows = [dict(date=f"{i:04}", close=100, max=100, min=100, Trading_Volume=1000) for i in range(100)]
        data = calculate_indicators(rows)
        charts = data["charts"]
        self.assertEqual(len(charts), 24)
        self.assertEqual(data["rsi"][-1], 50)
        self.assertEqual(charts["boll"]["series"]["上軌"][-1], 100)
        self.assertEqual(charts["atr"]["series"]["ATR"][-1], 0)
        self.assertEqual(charts["mfi"]["series"]["MFI"][-1], 50)
        self.assertEqual(charts["volume_ratio"]["series"]["量比"][-1], 1)
        self.assertIsNone(charts["ma"]["series"]["MA120"][-1])

    def test_missing_ohlc_preserves_close_indicators(self):
        data = calculate_indicators([dict(date=f"{i:04}", close=i+1) for i in range(40)])
        self.assertEqual(data["charts"]["ma"]["series"]["MA5"][-1], 38)
        self.assertTrue(all(x is None for x in data["charts"]["atr"]["series"]["ATR"]))
        for spec in data["charts"].values():
            for series in spec["series"].values():
                self.assertEqual(len(series), 40)

    def test_invalid_and_empty(self):
        self.assertIsNone(calculate_indicators([]))
        self.assertEqual(normalize_price_rows([dict(date="x",close=float("nan"))]), [])

    def test_sma_matches_plain_average(self):
        period = 5
        expected = [None] * (period - 1) + [
            sum(CLOSES[i - period + 1 : i + 1]) / period
            for i in range(period - 1, len(CLOSES))
        ]
        actual = _simple_moving_average(CLOSES, period)
        self.assertEqual(len(actual), len(CLOSES))
        for a, e in zip(actual, expected):
            if e is None:
                self.assertIsNone(a)
            else:
                self.assertAlmostEqual(a, e)

    def test_ema_matches_seed_plus_recursion(self):
        period = 5
        alpha = 2 / (period + 1)
        expected = [None] * (period - 1)
        expected.append(sum(CLOSES[:period]) / period)
        for i in range(period, len(CLOSES)):
            expected.append(CLOSES[i] * alpha + expected[-1] * (1 - alpha))
        actual = _ema(CLOSES, period)
        self.assertEqual(len(actual), len(CLOSES))
        for a, e in zip(actual, expected):
            if e is None:
                self.assertIsNone(a)
            else:
                self.assertAlmostEqual(a, e)

    def test_wilder_rsi_known_values(self):
        period = 5
        gains = [max(CLOSES[i] - CLOSES[i - 1], 0.0) for i in range(1, len(CLOSES))]
        losses = [max(CLOSES[i - 1] - CLOSES[i], 0.0) for i in range(1, len(CLOSES))]
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        def rsi_value(ag, al):
            if ag == al == 0:
                return 50.0
            if al == 0:
                return 100.0
            rs = ag / al
            return 100 - 100 / (1 + rs)

        expected = [None] * period
        expected.append(rsi_value(avg_gain, avg_loss))
        for offset in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[offset]) / period
            avg_loss = (avg_loss * (period - 1) + losses[offset]) / period
            expected.append(rsi_value(avg_gain, avg_loss))

        actual = _calculate_rsi(CLOSES, period)
        self.assertEqual(len(actual), len(CLOSES))
        # Check the seed index and a later index that exercises the recursive update.
        self.assertAlmostEqual(actual[period], expected[period])
        self.assertAlmostEqual(actual[-1], expected[-1])
        for a, e in zip(actual, expected):
            if e is None:
                self.assertIsNone(a)
            else:
                self.assertAlmostEqual(a, e)

    def test_macd_matches_fast_slow_ema_difference(self):
        fast, slow, signal = 3, 6, 3
        result = _calculate_macd(CLOSES, fast, slow, signal)
        fast_ema = _ema(CLOSES, fast)
        slow_ema = _ema(CLOSES, slow)
        last = len(CLOSES) - 1
        self.assertIsNotNone(result["macd"][last])
        self.assertAlmostEqual(result["macd"][last], fast_ema[last] - slow_ema[last])
        self.assertIsNotNone(result["signal"][last])
        self.assertAlmostEqual(
            result["histogram"][last], result["macd"][last] - result["signal"][last]
        )

    def test_kd_seed_and_first_recursion_step(self):
        period = 3
        bars = [
            TechnicalPriceBar(
                date=f"{i:04}", open=None, high=c + 0.3, low=c - 0.3,
                close=c, volume=100, trading_value=None,
            )
            for i, c in enumerate(CLOSES)
        ]
        result = _calculate_kd(bars, period=period)
        seed = period - 1

        window0 = bars[0 : period]
        highest0 = max(b.high for b in window0)
        lowest0 = min(b.low for b in window0)
        rsv_seed = (bars[seed].close - lowest0) / (highest0 - lowest0) * 100
        k_seed = 50.0 * 2 / 3 + rsv_seed / 3
        d_seed = 50.0 * 2 / 3 + k_seed / 3
        self.assertAlmostEqual(result["rsv"][seed], rsv_seed)
        self.assertAlmostEqual(result["k"][seed], k_seed)
        self.assertAlmostEqual(result["d"][seed], d_seed)

        window1 = bars[1 : period + 1]
        highest1 = max(b.high for b in window1)
        lowest1 = min(b.low for b in window1)
        rsv_next = (bars[seed + 1].close - lowest1) / (highest1 - lowest1) * 100
        k_next = k_seed * 2 / 3 + rsv_next / 3
        d_next = d_seed * 2 / 3 + k_next / 3
        self.assertAlmostEqual(result["rsv"][seed + 1], rsv_next)
        self.assertAlmostEqual(result["k"][seed + 1], k_next)
        self.assertAlmostEqual(result["d"][seed + 1], d_next)

    def test_rsv_flat_window_returns_fifty(self):
        bars = [
            TechnicalPriceBar(
                date=f"{i:04}", open=None, high=10.0, low=10.0,
                close=10.0, volume=100, trading_value=None,
            )
            for i in range(5)
        ]
        result = _calculate_kd(bars, period=3)
        for value in result["rsv"][2:]:
            self.assertEqual(value, 50.0)


if __name__ == "__main__":
    unittest.main()
