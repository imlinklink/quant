"""增量候选生成器 smoke test（真实正确性由 verify_incremental 脚本对拍批处理）。"""
import unittest

import pandas as pd

from scripts.portfolio_shadow.candidate_adapter import (
    DEFAULT_FORWARD_HORIZON, IncrementalCandidateGenerator)


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


class IncrementalMaturityTests(unittest.TestCase):
    """未来行情成熟度：研究模式要求未来结果已成熟，前向模式不得据未来数据丢弃候选。"""

    def _gen(self, *, require_matured, blocked=None, n=40,
             forward_horizon=DEFAULT_FORWARD_HORIZON):
        sessions = pd.bdate_range('2026-01-02', periods=n)
        prices = pd.DataFrame({
            'security_id': ['SEC-A'] * len(sessions), 'session': sessions,
            'raw_open': 100.0, 'raw_high': 101.0, 'raw_low': 99.0, 'raw_close': 100.5,
            'volume': 1000.0, 'asof_atr': 2.0, 'scale_to_next': 1.0})
        market = pd.DataFrame({'session': sessions, 'asof_close': 400.0, 'asof_ma200': 390.0})
        gen = IncrementalCandidateGenerator(
            prices, market, pd.DataFrame(), None, blocked or {}, sessions,
            experiment_id='x', parent_version='1', require_matured=require_matured,
            forward_horizon=forward_horizon)
        # 绕开月度动量管线，只测本次改动的成熟度门；信号直接桩化为命中
        gen._signal_for = lambda sid, sess: 'BREAKOUT'
        return gen, sessions

    def _seed(self, gen, decision_session):
        gen.pending['SEC-A-pending'] = {'security_id': 'SEC-A',
                                        'decision_session': decision_session, 'rank': 1}

    def test_default_forward_horizon_matches_batch_entries(self):
        from scripts.medium_term.p2_selection_check import HORIZONS
        self.assertEqual(DEFAULT_FORWARD_HORIZON, max(HORIZONS))

    def test_matured_mode_drops_candidate_lacking_forward_bars(self):
        gen, sessions = self._gen(require_matured=True)
        self._seed(gen, sessions[5])
        self.assertEqual(gen.opportunities_for(sessions[7]), [])
        self.assertEqual(gen.pending, {})

    def test_forward_mode_keeps_candidate_lacking_forward_bars(self):
        gen, sessions = self._gen(require_matured=False)
        self._seed(gen, sessions[5])
        out = gen.opportunities_for(sessions[7])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].planned_execution_session, str(sessions[8].date()))
        self.assertEqual(out[0].signal_session, str(sessions[7].date()))
        self.assertEqual(gen.pending, {})

    def test_forward_window_blocked_date_only_gates_in_matured_mode(self):
        # forward_horizon=5 使前向 bar 充足、只让行动覆盖门成为唯一变量
        sessions = pd.bdate_range('2026-01-02', periods=40)
        blocked = {'SEC-A': (sessions[10],)}
        matured, s1 = self._gen(require_matured=True, blocked=blocked, forward_horizon=5)
        self._seed(matured, s1[5])
        self.assertEqual(matured.opportunities_for(s1[7]), [])
        forward, s2 = self._gen(require_matured=False, blocked=blocked, forward_horizon=5)
        self._seed(forward, s2[5])
        self.assertEqual(len(forward.opportunities_for(s2[7])), 1)


class MissingAtrTests(unittest.TestCase):
    """ATR 是定止损的输入。缺失必须**按数据缺口丢弃候选**，而不是崩在 `to_micro`。

    原先 `_atr_micro` 直接把值交给 `to_micro`，而 `int(Decimal('NaN'))` 抛
    `ValueError: cannot convert NaN to integer` —— warmup 期的一根空 ATR 就能让整条
    生成链崩掉，读起来像代码缺陷，实际是数据缺口。`_atr_micro` 的 docstring 与
    `experiments._run` 的 `ATR_MISSING` 观察都假定它返回 `None`，这个假定此前没有测试。
    """

    def _gen(self, atr):
        sessions = pd.bdate_range('2026-01-02', periods=40)
        prices = pd.DataFrame({
            'security_id': ['SEC-A'] * len(sessions), 'session': sessions,
            'raw_open': 100.0, 'raw_high': 101.0, 'raw_low': 99.0, 'raw_close': 100.5,
            'volume': 1000.0, 'asof_atr': atr, 'scale_to_next': 1.0})
        market = pd.DataFrame({'session': sessions, 'asof_close': 400.0, 'asof_ma200': 390.0})
        gen = IncrementalCandidateGenerator(
            prices, market, pd.DataFrame(), None, {}, sessions,
            experiment_id='x', parent_version='1', require_matured=False)
        # 绕开月度动量管线与成熟度门，让"ATR 是否可用"成为唯一变量
        gen._signal_for = lambda sid, sess: 'BREAKOUT'
        gen.pending['SEC-A-pending'] = {'security_id': 'SEC-A',
                                        'decision_session': sessions[5], 'rank': 1}
        return gen, sessions

    def test_missing_atr_drops_the_candidate_instead_of_crashing(self):
        gen, sessions = self._gen(float('nan'))
        self.assertEqual(gen.opportunities_for(sessions[7]), [])
        self.assertEqual(gen.pending, {})          # 缺 ATR ⇒ 无法定止损 ⇒ 丢弃

    def test_nonpositive_atr_is_not_a_valid_risk_distance(self):
        """0 与负值同样不是可用的风险距离 —— 由 `_atr_micro` 直接钉死，不靠调用方自觉。"""
        gen, sessions = self._gen(0.0)
        self.assertIsNone(gen._atr_micro('SEC-A', sessions[7]))
        self.assertIsNone(self._gen(-1.0)[0]._atr_micro('SEC-A', sessions[7]))
        self.assertEqual(self._gen(2.0)[0]._atr_micro('SEC-A', sessions[7]), 2_000_000)

    def test_valid_atr_still_produces_the_opportunity(self):
        """反证：上面的丢弃不是"永远丢弃"—— 有效 ATR 照样产出机会。"""
        gen, sessions = self._gen(2.0)
        out = gen.opportunities_for(sessions[7])
        self.assertEqual(len(out), 1)
        self.assertEqual(gen.pending, {})


if __name__ == '__main__':
    unittest.main()
