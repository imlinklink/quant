import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from scripts.live_trading.entry_timing import evaluate_entry_timing


class EntryTimingTests(unittest.TestCase):
    def test_only_completed_breakout_triggers(self):
        times = pd.date_range('2026-09-10 09:30', periods=7, freq='15min',
                              tz='America/New_York')
        b = pd.DataFrame({'time_key': times, 'open': [100]*7, 'high': [101]*6+[103],
                          'low': [99]*7, 'close': [100]*6+[102.5], 'volume': [1]*7})
        setup = {'trigger_price': 101, 'max_chase_price': 104,
                 'invalidation_price': 95}
        now = datetime(2026, 9, 10, 11, 15, tzinfo=ZoneInfo('America/New_York'))
        self.assertTrue(evaluate_entry_timing(setup, b, now)['triggered'])
        earlier = datetime(2026, 9, 10, 11, 0, tzinfo=ZoneInfo('America/New_York'))
        self.assertFalse(evaluate_entry_timing(setup, b, earlier)['triggered'])


if __name__ == '__main__':
    unittest.main()
