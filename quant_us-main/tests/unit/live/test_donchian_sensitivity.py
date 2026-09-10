"""唐奇安参数敏感性扫描的离线验证（合成 OHLC，不依赖 Futu）。

目的：证明扫描管线正确——通道列按需生成、stop_mult 真正生效、网格产出完整。
真实行情下的结论需在有 OpenD 的机器上跑 run_donchian_sensitivity.py。
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_donchian_backtest import add_indicators, run_variant
from scripts.run_donchian_sensitivity import build_report, make_vdef, sweep, _surface


def synthetic_ohlc(n=400, seed=7):
    """构造带周期回撤的上行序列，保证能触发唐奇安突破。"""
    rng = np.random.default_rng(seed)
    trend = np.linspace(100.0, 200.0, n)
    cycle = 12.0 * np.sin(np.linspace(0, 18 * np.pi, n))
    close = trend + cycle + rng.normal(0, 0.8, n)
    high = close + 1.5
    low = close - 1.5
    open_ = close - rng.normal(0, 0.5, n)
    return pd.DataFrame({
        'date': pd.date_range('2020-07-01', periods=n, freq='B'),
        'open': open_, 'high': high, 'low': low, 'close': close,
        'volume': rng.uniform(8e5, 3e6, n),
    })


class IndicatorChannelTests(unittest.TestCase):
    def test_requested_channels_are_created(self):
        d = add_indicators(synthetic_ohlc(), channels=(20, 40, 70))
        for n in (20, 40, 55, 70):
            self.assertIn(f'hh{n}', d.columns, f'缺 hh{n}')
        # 向后兼容：ll10 / ll20 仍在
        self.assertIn('ll10', d.columns)
        self.assertIn('ll20', d.columns)

    def test_default_call_unchanged(self):
        d = add_indicators(synthetic_ohlc())
        self.assertIn('hh20', d.columns)
        self.assertIn('hh55', d.columns)


class StopMultTests(unittest.TestCase):
    def test_stop_mult_changes_outcome(self):
        d = add_indicators(synthetic_ohlc(), channels=(20,))
        tight = run_variant(d, make_vdef(20, 1.0))
        wide = run_variant(d, make_vdef(20, 4.0))
        self.assertTrue(tight, '紧止损应产生交易')
        self.assertTrue(wide, '宽止损应产生交易')
        # 更宽的 ATR 止损 → 止损更晚触发，出场价/盈亏应与紧止损不同
        self.assertNotEqual([t['exit_price'] for t in tight],
                            [t['exit_price'] for t in wide])

    def test_default_stop_mult_is_two(self):
        # 不传 stop_mult 时应等价于 2.0（模块常量），保持向后兼容
        d = add_indicators(synthetic_ohlc(), channels=(20,))
        a = run_variant(d, {**make_vdef(20, 2.0)})
        b = run_variant(d, {'key': 'legacy', 'label': 'legacy', 'entry_n': 20,
                            'vol_ratio': None, 'ma_filter': None, 'exit': 'chandelier'})
        self.assertEqual([t['exit_price'] for t in a], [t['exit_price'] for t in b])


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.ind = {'US.SYN': add_indicators(synthetic_ohlc(), channels=(20, 55))}

    def test_grid_shape_and_columns(self):
        res = sweep(self.ind, [20, 55], [1.5, 2.0])
        self.assertEqual(len(res), 4)  # 2 通道 × 2 倍数
        for col in ('channel', 'atr_mult', 'trades', 'expectancy',
                    'profit_factor', 'max_dd', 'pass_rate'):
            self.assertIn(col, res.columns)
        # 每格都应有交易（合成序列足够长）
        self.assertTrue((res['trades'] > 0).all())

    def test_report_renders_all_sections(self):
        res = sweep(self.ind, [20, 55], [1.5, 2.0])
        report = build_report(res, [20, 55], [1.5, 2.0])
        self.assertIn('年度通过率曲面', report)
        self.assertIn('期望/笔 曲面', report)
        self.assertIn('默认参数 vs 全网最优', report)
        self.assertIn('dc55', report)

    def test_surface_pivot(self):
        res = sweep(self.ind, [20, 55], [1.5, 2.0])
        lines = _surface(res, 'trades', lambda v: f'{int(v)}')
        # 表头 + 2 行
        self.assertEqual(len(lines), 4)
        self.assertIn('通道', lines[0])


if __name__ == '__main__':
    unittest.main()
