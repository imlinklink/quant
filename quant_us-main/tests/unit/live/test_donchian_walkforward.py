"""时间前进分析的离线验证（合成交易，不依赖 Futu）。

真实结论需在有 OpenD 的机器上跑 run_donchian_walkforward.py。
这里验证的是分析逻辑本身正确：折划分、IC、选参转移、前进组合口径。
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_donchian_walkforward import (
    _parse_cell, build_report, held_out_pool, spearman, walk_forward,
)


def synthetic_trades(years=range(2020, 2027), channels=(20, 55), mults=(1.5, 2.0),
                     per_cell_year=8, edge_cell=None, edge_bonus=300.0, seed=0):
    """构造逐笔明细：每个「格 × 年」有 per_cell_year 笔。

    edge_cell=(ch, mult) 时，给该格每笔加 edge_bonus，模拟「某参数真的更好」。
    """
    rng = np.random.default_rng(seed)
    rows = []
    for y in years:
        for ch in channels:
            for m in mults:
                for _ in range(per_cell_year):
                    pnl = float(rng.normal(0, 200))
                    if edge_cell and (ch, m) == edge_cell:
                        pnl += edge_bonus
                    rows.append({
                        'channel': ch, 'atr_mult': m,
                        'entry_date': pd.Timestamp(year=int(y), month=6, day=1),
                        'exit_date': pd.Timestamp(year=int(y), month=6, day=20),
                        'net_pnl_usd': pnl, 'open': False,
                        'stock': 'US.SYN', 'variant': f'dc{ch}_atr{m}',
                    })
    return pd.DataFrame(rows)


class SpearmanTests(unittest.TestCase):
    def test_perfect_and_inverse(self):
        a = [1, 2, 3, 4, 5]
        self.assertAlmostEqual(spearman(a, [2, 4, 6, 8, 10]), 1.0, places=6)
        self.assertAlmostEqual(spearman(a, [5, 4, 3, 2, 1]), -1.0, places=6)

    def test_degenerate_returns_nan(self):
        self.assertTrue(np.isnan(spearman([1, 1, 1], [1, 2, 3])))
        self.assertTrue(np.isnan(spearman([1, 2], [1, 2])))  # 样本不足


class ParseCellTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(_parse_cell('dc55/2.0'), (55, 2.0))
        self.assertEqual(_parse_cell('dc20/1.5'), (20, 1.5))


class WalkForwardTests(unittest.TestCase):
    def test_folds_use_earlier_years_as_train(self):
        trades = synthetic_trades()
        folds = walk_forward(trades)
        self.assertFalse(folds.empty)
        # 第一个可用的测试年应是 2021（2020 无训练期）
        self.assertEqual(int(folds['test_year'].iloc[0]), 2021)
        # 训练笔数应随年份递增（锚定扩窗）
        self.assertTrue(folds['train_trades'].is_monotonic_increasing)

    def test_real_edge_shows_positive_ic(self):
        # 让 dc55/2.0 在所有年份都更好 → 训练期表现应能预测测试期
        trades = synthetic_trades(edge_cell=(55, 2.0), edge_bonus=400.0, seed=1)
        folds = walk_forward(trades)
        ics = [f for f in folds['ic'] if np.isfinite(f)]
        self.assertTrue(ics)
        self.assertGreater(np.mean(ics), 0.5)
        # 最优格应常在样本外战胜中位
        self.assertGreaterEqual(int(folds['beat_median'].sum()), len(folds) - 1)

    def test_pure_noise_no_edge(self):
        # 无真实差异 → IC 不应稳定为正，也不应稳定战胜中位
        trades = synthetic_trades(seed=2)
        folds = walk_forward(trades)
        self.assertFalse(folds.empty)
        # 中位数附近：不强制断言方向，但至少要产出结果且 IC 有限
        self.assertTrue(all(np.isfinite(f) for f in folds['ic']))

    def test_min_sample_filters_short_years(self):
        trades = synthetic_trades(per_cell_year=1)  # 每格每年仅 1 笔 → 测试期样本不足
        folds = walk_forward(trades)
        self.assertTrue(folds.empty, '样本不足时不应产出折')


class HeldOutPoolTests(unittest.TestCase):
    def test_same_years_for_both_paths(self):
        trades = synthetic_trades(edge_cell=(55, 2.0), edge_bonus=400.0, seed=3)
        folds = walk_forward(trades)
        pool = held_out_pool(trades, folds)
        self.assertIn('walkforward', pool)
        self.assertIn('fixed_default', pool)
        # 两条路径都必须落在同一批测试年份内
        years = set(pool['years'])
        d = trades.copy()
        d['entry_year'] = d['entry_date'].dt.year
        self.assertTrue(years.issubset(set(d['entry_year'])))
        self.assertGreater(pool['walkforward']['trades'], 0)
        self.assertGreater(pool['fixed_default']['trades'], 0)

    def test_empty_folds_returns_empty(self):
        self.assertEqual(held_out_pool(synthetic_trades(), pd.DataFrame()), {})


class ReportTests(unittest.TestCase):
    def test_report_sections(self):
        trades = synthetic_trades(edge_cell=(55, 2.0), edge_bonus=400.0, seed=4)
        folds = walk_forward(trades)
        report = build_report(folds, held_out_pool(trades, folds))
        for kw in ('逐折结果', '汇总', '前进组合 vs 固定默认', '怎么读', 'IC'):
            self.assertIn(kw, report)

    def test_report_handles_no_folds(self):
        report = build_report(pd.DataFrame(), {})
        self.assertIn('没有足够的样本', report)


if __name__ == '__main__':
    unittest.main()
