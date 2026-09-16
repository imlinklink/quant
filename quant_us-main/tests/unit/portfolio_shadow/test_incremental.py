"""增量候选生成器 smoke test（真实正确性由 verify_incremental 脚本对拍批处理）。"""
import unittest

import pandas as pd

from scripts.portfolio_shadow.candidate_adapter import IncrementalCandidateGenerator


class IncrementalGeneratorSmokeTests(unittest.TestCase):
    def _gen(self):
        sessions = pd.bdate_range('2026-01-02', periods=30)
        prices = pd.DataFrame({
            'security_id': ['SEC-A'] * len(sessions),
            'session': sessions,
            'raw_open': 100.0, 'raw_high': 101.0, 'raw_low': 99.0, 'raw_close': 100.5,
            'volume': 1000.0, 'asof_atr': 2.0, 'scale_to_next': 1.0,
        })
        market = pd.DataFrame({'session': sessions, 'asof_close': 400.0,
                               'asof_ma200': 390.0})
        return IncrementalCandidateGenerator(
            prices, market, pd.DataFrame(), None, {}, sessions,
            experiment_id='x', parent_version='1')

    def test_non_month_end_returns_empty(self):
        gen = self._gen()
        # 非月末、无 pending → 空
        self.assertEqual(gen.opportunities_for(pd.Timestamp('2026-01-05')), [])

    def test_month_end_does_not_crash(self):
        gen = self._gen()
        # 月末日（2026-01-30）：动量回看不足 → 无候选，但不崩
        out = gen.opportunities_for(pd.Timestamp('2026-01-30'))
        self.assertEqual(out, [])


if __name__ == '__main__':
    unittest.main()
