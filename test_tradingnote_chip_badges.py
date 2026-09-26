import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest

from tradingnote_technical import calculate_indicators
from tradingnote_gui import _stock_trend_badges, _chip_momentum_badges


def _flat_rows(n=100, close=100, volume=1000):
    return [
        dict(date=f"{i:04}", close=close, max=close, min=close, Trading_Volume=volume)
        for i in range(n)
    ]


class PriceVolumeDivergenceTests(unittest.TestCase):
    """Feature A：_stock_trend_badges 新增的第 4 個「量價背離」標籤。"""

    def test_no_divergence_on_flat_series(self):
        technical = calculate_indicators(_flat_rows())
        badges = _stock_trend_badges(technical)
        texts = [text for text, _tone in badges]
        self.assertFalse(any(t.startswith("量價背離") for t in texts))

    def test_bullish_trend_with_shrinking_volume_flags_weakening(self):
        rows = _flat_rows()
        rows[-1] = dict(date=rows[-1]["date"], close=110, max=110, min=110, Trading_Volume=100)
        technical = calculate_indicators(rows)
        badges = _stock_trend_badges(technical)
        self.assertIn(("量價背離：轉弱", "negative"), badges)

    def test_bearish_trend_with_surging_volume_flags_possible_bottom(self):
        rows = _flat_rows()
        rows[-1] = dict(date=rows[-1]["date"], close=90, max=90, min=90, Trading_Volume=5000)
        technical = calculate_indicators(rows)
        badges = _stock_trend_badges(technical)
        self.assertIn(("量價背離：留意止跌", "info"), badges)

    def test_empty_technical_returns_no_badges(self):
        self.assertEqual(_stock_trend_badges(None), [])
        self.assertEqual(_stock_trend_badges({}), [])


class ChipMomentumBadgeTests(unittest.TestCase):
    """Feature B：_chip_momentum_badges。"""

    def test_missing_data_returns_empty(self):
        self.assertEqual(_chip_momentum_badges(None, None), [])

    def test_synchronized_buying(self):
        history = {
            "dates": ["2026-09-01", "2026-09-02"],
            "series": {"外資": [100, 200], "投信": [10, 20], "自營商": [5, 15]},
        }
        badges = _chip_momentum_badges(history, None)
        self.assertIn(("籌碼方向：三大法人同步買超", "positive"), badges)

    def test_synchronized_selling(self):
        history = {
            "dates": ["2026-09-01", "2026-09-02"],
            "series": {"外資": [-100, -200], "投信": [-10, -20], "自營商": [-5, -15]},
        }
        badges = _chip_momentum_badges(history, None)
        self.assertIn(("籌碼方向：三大法人同步賣超", "negative"), badges)

    def test_mixed_direction_is_divergent(self):
        history = {
            "dates": ["2026-09-01", "2026-09-02"],
            "series": {"外資": [100, 200], "投信": [10, -20], "自營商": [5, 15]},
        }
        badges = _chip_momentum_badges(history, None)
        self.assertIn(("籌碼方向：三大法人分歧", "neutral"), badges)

    def test_buy_streak_at_least_three_days(self):
        history = {
            "dates": ["09-01", "09-02", "09-03", "09-04"],
            "series": {
                "外資": [100, 100, 100, 100],
                "投信": [10, 10, 10, 10],
                "自營商": [5, 5, 5, 5],
            },
        }
        badges = _chip_momentum_badges(history, None)
        self.assertIn(("籌碼動能：連買 4 日", "positive"), badges)

    def test_streak_below_threshold_is_not_reported(self):
        history = {
            "dates": ["09-01", "09-02", "09-03"],
            "series": {"外資": [-50, 100, 100], "投信": [-5, 10, 10], "自營商": [-1, 5, 5]},
        }
        badges = _chip_momentum_badges(history, None)
        texts = [text for text, _tone in badges]
        self.assertFalse(any(t.startswith("籌碼動能") for t in texts))

    def test_margin_balance_increase(self):
        margin_history = {
            "dates": [f"09-{i:02}" for i in range(1, 13)],
            "series": {"融資餘額": [1000] * 10 + [1200, 1200], "融券餘額": [0] * 12},
        }
        badges = _chip_momentum_badges(None, margin_history)
        self.assertIn(("融資動向：10 日變化 +20.0%", "info"), badges)

    def test_margin_balance_flat_is_not_reported(self):
        margin_history = {
            "dates": [f"09-{i:02}" for i in range(1, 13)],
            "series": {"融資餘額": [1000] * 12, "融券餘額": [0] * 12},
        }
        badges = _chip_momentum_badges(None, margin_history)
        texts = [text for text, _tone in badges]
        self.assertFalse(any(t.startswith("融資動向") for t in texts))

    def test_margin_history_shorter_than_window_is_skipped(self):
        margin_history = {
            "dates": ["09-01", "09-02"],
            "series": {"融資餘額": [1000, 1100], "融券餘額": [0, 0]},
        }
        badges = _chip_momentum_badges(None, margin_history)
        texts = [text for text, _tone in badges]
        self.assertFalse(any(t.startswith("融資動向") for t in texts))


if __name__ == "__main__":
    unittest.main()
