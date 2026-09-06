# -*- coding: utf-8 -*-
"""UnifiedExitStrategy 兜底路径回归：K线<30/只有当日高低/无数据时不再 AttributeError。"""
import unittest

import pandas as pd

from mutifactor.strategies.unified_exit_strategy import UnifiedExitStrategy


def _df(n: int) -> pd.DataFrame:
    return pd.DataFrame({
        'time_key': pd.date_range('2026-01-01', periods=n, freq='D'),
        'open': [10.0] * n,
        'high': [10.5] * n,
        'low': [9.5] * n,
        'close': [10.0] * n,
        'volume': [1000] * n,
    })


POS = {'stock_code': 'HK.T', 'quantity': 100,
       'cost_price': 10.0, 'highest_price': 10.0}


class TestUnifiedFallback(unittest.TestCase):
    def _strategy(self, mode):
        return UnifiedExitStrategy({'risk': {'early_hard_stop_pct': 0.08}}, mode=mode)

    def test_backtest_kline_short_uses_simple(self):
        out = self._strategy('backtest').check_exit(
            dict(POS), 10.2, kline_df=_df(10), current_date='2026-02-01'
        )
        assert isinstance(out, tuple) and len(out) == 5
        assert out[0] in (True, False)

    def test_backtest_no_kline_uses_simple(self):
        out = self._strategy('backtest').check_exit(
            dict(POS), 9.0, kline_df=None, current_date='2026-02-01'
        )
        assert isinstance(out, tuple) and len(out) == 5

    def test_live_intraday_only_uses_simple(self):
        out = self._strategy('live').check_exit(
            dict(POS), 10.1, today_high=10.6, today_low=9.9
        )
        assert isinstance(out, tuple) and len(out) == 5

    def test_live_no_data_uses_simple(self):
        out = self._strategy('live').check_exit(dict(POS), 10.1)
        assert isinstance(out, tuple) and len(out) == 5
