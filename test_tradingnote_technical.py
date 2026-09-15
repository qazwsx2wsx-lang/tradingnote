import unittest
from tradingnote_technical import calculate_indicators, normalize_price_rows


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


if __name__ == "__main__":
    unittest.main()
