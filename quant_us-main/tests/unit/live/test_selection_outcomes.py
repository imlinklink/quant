"""固定窗口结果（阶段 G3）回归：收益/MFE/MAE、rank IC、Top-N 超额、完整汇总。"""
import unittest

import numpy as np
import pandas as pd

from scripts.live_trading.decision_ledger.selection_outcomes import (
    compute_window_outcomes, evaluate_selection, rank_ic, topn_excess,
)


def _bars(codes_slope, as_of='2026-09-04'):
    """构造 bars：每个 code 20 个交易日，价格按 slope 线性上涨。as_of 落在第 8 根后。"""
    rows = []
    dates = pd.date_range('2026-08-24', periods=20, freq='B', tz='UTC')
    for code, slope in codes_slope.items():
        for i, d in enumerate(dates):
            price = 100.0 * (1.0 + slope * i)
            rows.append({'code': code, 'date': d, 'open': price, 'high': price * 1.02,
                         'low': price * 0.98, 'close': price})
    return pd.DataFrame(rows)


class SelectionOutcomesContracts(unittest.TestCase):
    def test_compute_window_outcomes(self):
        bars = _bars({'US.A': 0.01, 'US.B': 0.005})
        outcomes = compute_window_outcomes(bars, ['US.A', 'US.B'],
                                           as_of='2026-09-04', horizons=(1, 5))
        for code in ('US.A', 'US.B'):
            r5 = outcomes[code][5]
            self.assertTrue(r5['complete'])
            self.assertGreater(r5['return'], 0)
            self.assertGreaterEqual(r5['mfe'], r5['return'])
            self.assertLessEqual(r5['mae'], r5['return'])
            # 斜率更高的 A 收益更高
        self.assertGreater(outcomes['US.A'][5]['return'], outcomes['US.B'][5]['return'])

    def test_window_outcomes_incomplete_when_no_data(self):
        bars = _bars({'US.A': 0.01})
        # as_of 太晚，未来不足 10 会话
        outcomes = compute_window_outcomes(bars, ['US.A'],
                                           as_of='2026-09-30', horizons=(10,))
        self.assertFalse(outcomes['US.A'][10]['complete'])

    def test_intraday_as_of_does_not_use_unclosed_today_bar(self):
        # P1-3：盘中 as_of 时，当日尚未收盘的 K 不得作为决策基准。
        # 构造：09-04 当天有一根「暴涨收盘」bar，但 as_of=09-04 12:00（盘中）→ 基准应为 09-03，而非 9999。
        rows = []
        dates = pd.date_range('2026-08-24', periods=20, freq='B', tz='UTC')  # 到 09-18 附近
        # 强制把最后一根放到 09-04，并给它离谱收盘 9999
        for i, d in enumerate(dates):
            if d.normalize() == pd.Timestamp('2026-09-04', tz='UTC'):
                continue
            px = 100.0 + i
            rows.append({'code': 'US.A', 'date': d, 'open': px, 'high': px + 1,
                         'low': px - 1, 'close': px})
        rows.append({'code': 'US.A', 'date': pd.Timestamp('2026-09-04', tz='UTC'),
                     'open': 120, 'high': 9999, 'low': 119, 'close': 9999})
        bars = pd.DataFrame(rows).sort_values('date')
        # 盘中：09-04 12:00 还没到 09-04 22:00 收盘标记 → 当日 bar 不可用
        out = compute_window_outcomes(bars, ['US.A'], as_of='2026-09-04T12:00:00+00:00',
                                      horizons=(1,))
        self.assertTrue(out['US.A'][1]['complete'])
        # 基准收益应从 09-03 收盘算，绝不会出现把 9999 当 base 的 -98% 假收益
        self.assertGreater(out['US.A'][1]['return'], -0.5)
        # 收盘后（09-04 23:00）当日 bar 已收盘 → base=9999，1 日后收益可负
        out2 = compute_window_outcomes(bars, ['US.A'], as_of='2026-09-04T23:00:00+00:00',
                                       horizons=(1,))
        self.assertLess(out2['US.A'][1]['return'], -0.5) if out2['US.A'][1]['complete'] else None

    def test_rank_ic_perfect_and_reversed(self):
        ranked = ['US.A', 'US.B', 'US.C']
        good = {'US.A': {1: {'complete': True, 'return': 0.3}},
                'US.B': {1: {'complete': True, 'return': 0.2}},
                'US.C': {1: {'complete': True, 'return': 0.1}}}
        self.assertAlmostEqual(rank_ic(ranked, good, 1), 1.0, places=6)

        bad = {'US.A': {1: {'complete': True, 'return': 0.1}},
               'US.B': {1: {'complete': True, 'return': 0.2}},
               'US.C': {1: {'complete': True, 'return': 0.3}}}
        self.assertAlmostEqual(rank_ic(ranked, bad, 1), -1.0, places=6)

    def test_rank_ic_ignores_incomplete(self):
        ranked = ['US.A', 'US.B']
        outcomes = {'US.A': {1: {'complete': True, 'return': 0.3}},
                    'US.B': {1: {'complete': False}}}
        self.assertIsNone(rank_ic(ranked, outcomes, 1))  # 只剩一个有效样本

    def test_topn_excess(self):
        universe = ['US.A', 'US.B', 'US.C', 'US.D']
        outcomes = {c: {1: {'complete': True, 'return': r}} for c, r in
                    zip(universe, [0.3, 0.25, 0.05, 0.0])}
        excess = topn_excess(['US.A', 'US.B'], universe, outcomes, 1)
        self.assertGreater(excess, 0)

    def test_evaluate_selection(self):
        batch = {'research_batch_id': 'b1', 'as_of': '2026-09-04',
                 'universe': ['US.A', 'US.B', 'US.C'],
                 'candidates': [
                     {'code': 'US.A', 'rank': 1, 'missing_information': []},
                     {'code': 'US.B', 'rank': 2, 'missing_information': ['缺财报']},
                     {'code': 'US.C', 'rank': 3, 'missing_information': []},
                 ]}
        bars = _bars({'US.A': 0.01, 'US.B': 0.005, 'US.C': 0.003})
        report = evaluate_selection(batch, bars, horizons=(1, 5))
        self.assertEqual(report['universe_size'], 3)
        self.assertEqual(report['ranked_count'], 3)
        self.assertEqual(report['missing_information_rate'], round(1 / 3, 4))
        for h in (1, 5):
            self.assertIn(h, report['horizons'])
            self.assertIsNotNone(report['horizons'][h]['rank_ic'])


if __name__ == '__main__':
    unittest.main()
