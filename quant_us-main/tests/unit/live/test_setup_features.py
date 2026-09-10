import unittest

import numpy as np
import pandas as pd

from scripts.live_trading.setup_features import completed_daily_bars, compute_setup_features


def bars(n=280, start='2025-01-01'):
    dates = pd.bdate_range(start, periods=n, tz='UTC')
    close = np.linspace(100, 150, n)
    return pd.DataFrame({'date': dates, 'open': close, 'high': close + 1,
                         'low': close - 1, 'close': close,
                         'volume': np.full(n, 1_000_000.0)})


class SetupFeatureTests(unittest.TestCase):
    def test_unclosed_daily_bar_is_excluded(self):
        d = bars(3)
        as_of = d.date.iloc[-1].normalize() + pd.Timedelta(hours=20)
        out = completed_daily_bars(d, as_of)
        self.assertEqual(len(out), 2)

    def test_summer_close_uses_new_york_dst(self):
        d = bars(3, start='2026-07-01')
        as_of = d.date.iloc[-1].normalize() + pd.Timedelta(hours=20, minutes=1)
        self.assertEqual(len(completed_daily_bars(d, as_of)), 3)

    def test_fail_closed_when_warmup_missing(self):
        out = compute_setup_features(bars(50), None, None, '2026-12-31T23:00:00Z')
        self.assertEqual(out['quality']['status'], 'fail')

    def test_feature_prefix_does_not_change_with_future_rows(self):
        d = bars()
        cutoff = d.date.iloc[259].normalize() + pd.Timedelta(hours=22)
        a = compute_setup_features(d, None, None, cutoff, {'min_daily_bars': 250})
        b = compute_setup_features(d.iloc[:260], None, None, cutoff,
                                   {'min_daily_bars': 250})
        self.assertEqual(a['features'], b['features'])
        self.assertEqual(a['structure'], b['structure'])


if __name__ == '__main__':
    unittest.main()
