# -*- coding: utf-8 -*-
"""阶段低点日线评分（v1）回归：RSI 拐头给确认分、止损距离否决。"""
import unittest

import pandas as pd

from scripts.live_trading.intraday_analyzer import IntradayAnalyzer


def _daily(closes, lows=None, highs=None):
    n = len(closes)
    lows = lows or [c * 0.995 for c in closes]
    highs = highs or [c * 1.005 for c in closes]
    return pd.DataFrame({
        'date': pd.date_range('2025-01-01', periods=n, freq='B'),
        'open': closes,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': [1_000_000.0] * n,
    })


def _decline_then_stabilize():
    closes = [100 * (0.985 ** i) for i in range(24)]
    # 最后两根企稳回升
    closes += [closes[-1] * 1.005, closes[-1] * 1.012]
    return closes


class TestBottomFishDaily(unittest.TestCase):
    def setUp(self):
        self.ana = IntradayAnalyzer({
            'analysis': {'bottom_fish_daily': {}},
        })

    def test_stabilize_with_rsi_turn_passes(self):
        df = _daily(_decline_then_stabilize())
        price = float(df['close'].iloc[-1])
        res = self.ana.analyze_bottom_fish_daily(df, price)
        assert res['ok'] is True, res['details']
        assert res['rsi_turn'] is True
        assert res['score'] >= 3

    def test_stop_distance_veto(self):
        df = _daily(_decline_then_stabilize())
        # 价格已远离参考低点 8%+ → 止损空间过大，即使形态成立也放弃
        ref_low = float(df['low'].tail(20).min())
        price = ref_low * 1.09
        res = self.ana.analyze_bottom_fish_daily(df, price)
        assert res['ok'] is False
        assert '止损距离' in res['details']

    def test_insufficient_bars(self):
        df = _daily([100, 99, 98])
        res = self.ana.analyze_bottom_fish_daily(df, 98.0)
        assert res['ok'] is False
        assert '数据不足' in res['details']
