# -*- coding: utf-8 -*-
"""intraday_analyzer 修复回归：追涨配置读取段、跌后企稳死分支、追涨量价阈值。"""
import unittest

import pandas as pd

from scripts.live_trading.intraday_analyzer import IntradayAnalyzer


def _bars(closes, opens=None, lows=None, highs=None, volumes=None, n=20):
    opens = opens or closes
    lows = lows or [c - 0.1 for c in closes]
    highs = highs or [c + 0.1 for c in closes]
    volumes = volumes or [1000] * len(closes)
    return pd.DataFrame({
        'time_key': pd.date_range('2026-01-01', periods=len(closes), freq='5min'),
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes,
    })


class TestIntradayFixes(unittest.TestCase):
    def test_momentum_config_reads_analysis_momentum(self):
        a = IntradayAnalyzer({'analysis': {
            'momentum': {
                'rsi_strong': 80,
                'rsi_moderate': 70,
                'bb_breakout_pct': 95,
                'volume_surge_ratio': 2.5,
                'max_rally_pct': 0.05,
            },
        }})
        assert a.bo_rsi_strong == 80
        assert a.bo_rsi_moderate == 70
        assert a.bo_bb_breakout_pct == 95
        assert a.bo_volume_surge_ratio == 2.5
        assert a.bo_max_rally_pct == 0.05

    def test_momentum_config_missing_falls_back_to_defaults(self):
        a = IntradayAnalyzer({'analysis': {}})
        assert a.bo_rsi_strong == 65
        assert a.bo_volume_surge_ratio == 1.5

    def test_dip_stabilize_candle_scores(self):
        a = IntradayAnalyzer({'analysis': {}})
        # 前三根：10→9→8（连跌），最后一根 8→8.8 阳线收在前高之上 → 企稳 +1
        closes = [10.0, 9.0, 8.0, 8.8]
        opens = [10.0, 9.0, 8.0, 8.0]
        score, details = a._calc_candle(_bars(closes, opens=opens))
        assert '跌后企稳' in details, details
        assert score >= 1

    def test_momentum_volume_uses_momentum_threshold(self):
        # volume_ratio_threshold 故意调成 99，bo 阈值 1.0：
        # 若用错共享阈值则拿不到“上涨放量+2”，正确应拿到
        a = IntradayAnalyzer({'analysis': {
            'volume_ratio_threshold': 99.0,
            'momentum': {'volume_surge_ratio': 1.0},
        }})
        closes = [10.0] * 5 + [10.1, 10.2, 10.4, 10.5, 10.6]
        volumes = [1000] * 5 + [1100, 1200, 1300, 1500, 1600]
        bars = _bars(closes, volumes=volumes)
        result = a.analyze_momentum('HK.T', None, bars, 10.6)
        assert result['volume_score'] >= 2, result
