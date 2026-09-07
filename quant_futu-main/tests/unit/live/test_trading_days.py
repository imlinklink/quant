# -*- coding: utf-8 -*-
"""富途 request_trading_days 返回结构解析（新版 list / 旧版 DataFrame）。"""
import unittest
import threading
import time

import pandas as pd

from scripts.live_trading.live_manager_base import (
    _extract_trading_day_set,
    _sleep_interruptible,
)


class TestExtractTradingDaySet(unittest.TestCase):
    def test_new_sdk_list_of_dicts(self):
        data = [
            {'time': '2026-09-01 00:00:00', 'trade_date_type': 'WHOLE'},
            {'time': '2026-09-02', 'trade_date_type': 'WHOLE'},
            {'time': '2026-09-03', 'trade_date_type': 'HALF'},
        ]
        days = _extract_trading_day_set(data)
        self.assertEqual(days, {'2026-09-01', '2026-09-02', '2026-09-03'})

    def test_legacy_dataframe(self):
        df = pd.DataFrame({
            'time_point': ['2026-09-01', '2026-09-02'],
        })
        days = _extract_trading_day_set(df)
        self.assertEqual(days, {'2026-09-01', '2026-09-02'})

    def test_empty_or_invalid_returns_none(self):
        self.assertIsNone(_extract_trading_day_set(None))
        self.assertIsNone(_extract_trading_day_set([]))
        self.assertIsNone(_extract_trading_day_set([{'foo': 'x'}]))
        self.assertIsNone(_extract_trading_day_set(pd.DataFrame()))


class TestInterruptibleSleep(unittest.TestCase):
    def test_stop_event_breaks_long_sleep(self):
        ev = threading.Event()
        start = time.monotonic()

        def set_event():
            ev.set()

        threading.Timer(0.1, set_event).start()
        _sleep_interruptible(ev, 60)
        self.assertLess(time.monotonic() - start, 2.0)


if __name__ == '__main__':
    unittest.main()
