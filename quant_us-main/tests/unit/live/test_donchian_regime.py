"""市场状态（regime）过滤实验的离线验证（合成数据，不依赖 Futu）。

重点验证两件容易出错的事：
  1. **闸门无前视**——改动未来数据不得改变过去某日的闸门取值；
  2. **闸门真的拦得住**——全 False 时零交易，全 True 时与无过滤基线一致。
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_donchian_backtest import add_indicators, run_variant
from scripts.run_donchian_regime import (
    add_stock_ma, base_vdef, build_market_gate, build_report, configs, make_gate,
    run_experiment,
)


def synthetic_ohlc(n=400, seed=7):
    rng = np.random.default_rng(seed)
    trend = np.linspace(100.0, 200.0, n)
    cycle = 12.0 * np.sin(np.linspace(0, 18 * np.pi, n))
    close = trend + cycle + rng.normal(0, 0.8, n)
    return pd.DataFrame({
        'date': pd.date_range('2020-07-01', periods=n, freq='B'),
        'open': close - rng.normal(0, 0.5, n),
        'high': close + 1.5, 'low': close - 1.5, 'close': close,
        'volume': rng.uniform(8e5, 3e6, n),
    })


def synthetic_bench(n=400, seed=11):
    """基准指数：前 60 天在均线下方，之后上行到均线上方。"""
    rng = np.random.default_rng(seed)
    down = np.linspace(120, 100, 60)
    up = np.linspace(100, 160, n - 60)
    close = np.concatenate([down, up]) + rng.normal(0, 0.3, n)
    return pd.DataFrame({'date': pd.date_range('2020-07-01', periods=n, freq='B'),
                         'close': close})


class NoLookaheadTests(unittest.TestCase):
    def test_future_change_does_not_alter_past_gate(self):
        bench = synthetic_bench()
        full = build_market_gate(bench, 50, bench['date'])
        cutoff = bench['date'].iloc[200]
        trunc = build_market_gate(bench[bench['date'] <= cutoff], 50, bench['date'])
        # 截断点之前的所有日期，两次计算的闸门取值必须完全一致
        for ts, v in trunc.items():
            if ts <= cutoff:
                self.assertEqual(full[ts], v, f'{ts} 的闸门取值受未来数据影响')

    def test_gate_reflects_price_vs_ma(self):
        bench = synthetic_bench()
        gate = build_market_gate(bench, 50, bench['date'])
        d = bench.copy()
        d['ma'] = d['close'].rolling(50).mean()
        for _, r in d.dropna().iloc[::37].iterrows():
            ts = pd.Timestamp(r['date']).normalize()
            self.assertEqual(gate[ts], bool(r['close'] > r['ma']))

    def test_missing_benchmark_date_carries_forward(self):
        bench = synthetic_bench()
        # 人为挖掉中间一天
        gap_ts = bench['date'].iloc[150]
        holed = bench[bench['date'] != gap_ts]
        extra = pd.Timestamp(gap_ts)
        gate = build_market_gate(holed, 50, list(bench['date']) + [extra])
        self.assertIn(extra, gate)  # 缺失日被填充而非 KeyError


class GateBlockingTests(unittest.TestCase):
    def setUp(self):
        self.ind = add_stock_ma(add_indicators(synthetic_ohlc()), [50, 200])
        self.vdef = base_vdef()

    def test_always_false_blocks_all(self):
        trades = run_variant(self.ind, self.vdef, entry_allowed=lambda r, a, i: False)
        self.assertEqual(trades, [])

    def test_always_true_matches_no_gate(self):
        a = run_variant(self.ind, self.vdef, entry_allowed=lambda r, a, i: True)
        b = run_variant(self.ind, self.vdef)
        self.assertEqual([t['exit_price'] for t in a], [t['exit_price'] for t in b])

    def test_no_gate_is_default(self):
        # 不传 entry_allowed 时行为不变（向后兼容）
        self.assertEqual([t['entry_price'] for t in run_variant(self.ind, self.vdef)],
                         [t['entry_price'] for t in run_variant(self.ind, self.vdef, None)])

    def test_stock_gate_uses_ma_column(self):
        gate = make_gate('stock', 200, None)
        row_on = pd.Series({'close': 120.0, 'ma200': 100.0})
        row_off = pd.Series({'close': 90.0, 'ma200': 100.0})
        self.assertTrue(gate(row_on, None, 0))
        self.assertFalse(gate(row_off, None, 0))

    def test_market_gate_unknown_date_is_off(self):
        gate = make_gate('market', 50, {})
        self.assertFalse(gate(pd.Series({'date': pd.Timestamp('2021-01-04')}), None, 0))


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.ind_data = {'US.SYN': add_stock_ma(add_indicators(synthetic_ohlc()), [50, 200])}
        self.bench = synthetic_bench()

    def test_all_configs_run(self):
        cfgs = configs([50, 200])
        trades = run_experiment(self.ind_data, self.bench, cfgs)
        self.assertFalse(trades.empty)
        self.assertEqual(set(trades['config']),
                         {'baseline', 'marketMA50', 'marketMA200', 'stockMA50', 'stockMA200'})

    def test_restrictive_gate_reduces_trades(self):
        cfgs = configs([200])
        trades = run_experiment(self.ind_data, self.bench, cfgs)
        n_base = (trades['config'] == 'baseline').sum()
        n_gated = (trades['config'] == 'marketMA200').sum()
        self.assertLessEqual(n_gated, n_base)

    def test_report_sections(self):
        cfgs = configs([50, 200])
        trades = run_experiment(self.ind_data, self.bench, cfgs)
        report = build_report(trades, cfgs)
        for kw in ('全期对比', '分年期望', '相对基线的差异', '怎么读'):
            self.assertIn(kw, report)

    def test_report_handles_empty(self):
        self.assertIn('没有产生任何交易', build_report(pd.DataFrame(), configs([50])))


if __name__ == '__main__':
    unittest.main()
