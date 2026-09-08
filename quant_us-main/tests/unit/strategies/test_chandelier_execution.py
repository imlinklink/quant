"""保护线方向、开盘成交和信号日隔离的离线回归。"""
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from mutifactor.strategies.chandelier_backtest import ChandelierBacktester
from mutifactor.strategies.dual_chandelier import PositionExitState


class TestChandelierExecution(unittest.TestCase):
    def run_bars(self, direction, prices, missing_last_atr=False):
        df = pd.DataFrame([
            dict(date=pd.Timestamp('2026-03-02 09:30') + pd.Timedelta(minutes=15*i),
                 open=o, close=c, high=max(o, c), low=min(o, c))
            for i, (o, c) in enumerate(prices)
        ])
        bt = ChandelierBacktester(None, {'chandelier': {'atr_period': 1}})
        atr = pd.Series([2.0] * len(df))
        if missing_last_atr:
            atr.iloc[-1] = np.nan
        with patch.object(bt, '_fetch_minute_klines', return_value=df), \
             patch.object(bt, '_compute_atr_series', return_value=atr):
            return bt.run('US.TEST', '2026-03-01', direction=direction)

    def test_normal_open_after_trailing_does_not_exit(self):
        for direction, price in [('long', 110), ('short', 90)]:
            with self.subTest(direction=direction):
                result = self.run_bars(direction, [(100, price), (price, price)])
                self.assertEqual(result.exit_reason, 'HOLD')

    def test_gap_fills_at_open_even_without_atr(self):
        for direction, price, gap in [('long', 110, 102), ('short', 90, 98)]:
            for missing in [False, True]:
                with self.subTest(direction=direction, missing=missing):
                    result = self.run_bars(direction, [(100, price), (gap, gap)], missing)
                    self.assertEqual(result.exit_reason, 'STOP_LOSS')
                    self.assertEqual(result.exit_price, gap)

    def test_trailing_then_breakeven_never_loosens_stop(self):
        for direction, peak, retreat in [('long', 110, 105), ('short', 90, 95)]:
            with self.subTest(direction=direction):
                state = PositionExitState(100, direction=direction)
                state.recompute(2, peak)
                stop = state.stop_line
                state.recompute(2, retreat)
                self.assertEqual(state.stop_line, stop)

    def test_signal_day_intraday_bars_are_excluded(self):
        df = pd.DataFrame([
            dict(date=pd.Timestamp(date), open=price, close=price, high=price, low=price)
            for date, price in [('2026-03-02 09:30', 80), ('2026-03-03 09:30', 100)]
        ])
        bt = ChandelierBacktester(None, {'chandelier': {'atr_period': 1}})
        with patch.object(bt, '_fetch_minute_klines', return_value=df):
            result = bt.run('US.TEST', '2026-03-02')
        self.assertEqual(result.entry_price, 100)
        self.assertTrue(all(t.date() > pd.Timestamp('2026-03-02').date()
                            for t in result.time_history))

    def test_intrabar_stop_cannot_be_hidden_by_rebound(self):
        df=pd.DataFrame([dict(date=pd.Timestamp('2026-03-02 09:30'),open=100,high=102,low=90,close=101)])
        bt=ChandelierBacktester(None, {'chandelier':{'atr_period':1}})
        with patch.object(bt,'_fetch_minute_klines',return_value=df):
            result=bt.run('US.TEST','2026-03-01')
        self.assertEqual(result.exit_reason,'STOP_LOSS')
        self.assertEqual(result.exit_price,95)
