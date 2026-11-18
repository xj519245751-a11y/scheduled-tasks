import unittest

import numpy as np
import pandas as pd

from mmain import calculate_macd_hist, streak_at, trend_tag


class TestMacdV2Core(unittest.TestCase):
    def test_streak_at_counts_current_same_sign_run(self):
        values = np.array([1.0, 2.0, -1.0, -2.0, -3.0])
        self.assertEqual(streak_at(values, 4), (3, -1))

    def test_streak_at_handles_zero(self):
        values = np.array([1.0, 0.0, -1.0])
        self.assertEqual(streak_at(values, 1), (0, 0))

    def test_trend_tags(self):
        self.assertEqual(trend_tag(1, 0.05), "📈上涨延续")
        self.assertEqual(trend_tag(1, -0.05), "↩️下跌中反弹")
        self.assertEqual(trend_tag(-1, -0.05), "📉下跌延续")
        self.assertEqual(trend_tag(-1, 0.05), "↩️上涨中回调")
        self.assertEqual(trend_tag(1, 0.005), "➖横盘")

    def test_macd_hist_returns_finite_series(self):
        close = pd.Series(np.linspace(100, 130, 100))
        hist = calculate_macd_hist(close)
        self.assertEqual(len(hist), len(close))
        self.assertTrue(np.isfinite(hist.to_numpy()).all())


if __name__ == "__main__":
    unittest.main()
