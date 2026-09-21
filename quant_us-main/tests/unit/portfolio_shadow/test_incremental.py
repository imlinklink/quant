"""增量候选生成器 smoke test（真实正确性由 verify_incremental 脚本对拍批处理）。"""
import unittest

import numpy as np
import pandas as pd

from scripts.medium_term.stock_cross_section import generate_monthly_candidates
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


class MembersMaskParityTests(unittest.TestCase):
    """时点宇宙掩码在**两条路径**上必须一致：批处理 vs 影子增量。

    掩码一开始只加到了批量那条上（`generate_monthly_candidates`），而前向跑的是增量
    生成器 —— 不钉住就会让"前向的 B 臂"与"回测的 B 臂"在宇宙口径上悄悄分叉，
    而两个 B 臂只允许差"是不是前向"。

    **第一版是空转的**：夹具没给 `quality` ⇒ 增量路径一个候选都不选 ⇒ 两边都是空集，
    把掩码从增量路径摘掉测试照样通过。所以这里先断言**夹具真的选出东西**，再比两条路径。
    """

    NAMES = ('A', 'B', 'C')

    def _fixture(self):
        sessions = pd.bdate_range('2025-01-01', periods=330)
        frames = []
        for sid, slope in (('A', .5), ('B', .3), ('C', .1)):
            close = 100 + np.arange(len(sessions)) * slope
            frames.append(pd.DataFrame({
                'security_id': sid, 'session': sessions, 'raw_open': close,
                'raw_high': close * 1.01, 'raw_low': close * .99, 'raw_close': close,
                'volume': 1e6, 'asof_atr': 2., 'scale_to_next': 1.,
                'asof_close': close, 'asof_ma200': 100.}))
        prices = pd.concat(frames, ignore_index=True)
        market = pd.DataFrame({'session': sessions, 'asof_close': 101., 'asof_ma200': 100.})
        # **有列无行**：两条路径都走 PIT 复权分支（该函数会读 action_type/ex_date，
        # 给一个连列都没有的空帧会 KeyError）
        actions = pd.DataFrame(columns=['security_id', 'ex_date', 'action_type',
                                        'ratio', 'cash_amount'])
        # 质量区间必须给 —— 否则增量路径把候选整批丢掉，测试就成了空转
        quality = pd.DataFrame([{'security_id': s, 'quality_status': 'verified',
                                 'from_session': str(sessions[0].date()),
                                 'to_session': str(sessions[-1].date())} for s in self.NAMES])
        return prices, market, sessions, actions, quality

    def _mask(self, sessions, excluded):
        return pd.DataFrame([{'session': s, 'security_id': sid, 'eligible': sid != excluded}
                             for s in sessions for sid in self.NAMES])

    def test_batch_and_incremental_agree_under_the_same_mask(self):
        prices, market, sessions, actions, quality = self._fixture()
        mask = self._mask(sessions, excluded='A')          # 排掉动量最强的 A
        view = prices[['security_id', 'session', 'raw_open', 'raw_high', 'raw_low',
                       'raw_close', 'volume']].rename(columns={
            'raw_open': 'open', 'raw_high': 'high', 'raw_low': 'low', 'raw_close': 'close'})
        batch = generate_monthly_candidates(view, market, top_n=2,
                                            actions=actions, members=mask)
        # 取**倒数第二个**轮次：最后一轮没有 T+1（`next_session` 返回 None），
        # 那一轮的 `selected` 会被整批置 False（既有测试同样取 [-2]）
        decision = sorted(batch.decision_session.unique())[-2]
        brow = batch[batch.decision_session.eq(decision)].set_index('security_id')
        # 先证明夹具真选出了东西 —— 否则下面的比较是空集比空集
        self.assertTrue(brow.selected.astype(bool).any(), '夹具没选出任何候选，测试会空转')
        gen = IncrementalCandidateGenerator(
            prices, market, quality, actions, {}, sessions,
            experiment_id='x', parent_version='1', top_n=2, members=mask)
        gen._generate_monthly(decision)
        # pending 的键是 `{sid}-{轮次日}`；取回 security_id 再比
        incremental = sorted({cid.rsplit('-', 3)[0] for cid in gen.pending})
        self.assertTrue(incremental, '增量路径没选出任何候选，测试会空转')
        # 掩码生效：A 不合格且带原因，留在截面里当审计分母
        self.assertFalse(bool(brow.loc['A', 'eligible']))
        self.assertEqual(brow.loc['A', 'reject_reason'], 'OUTSIDE_PIT_UNIVERSE')
        # 排名**只在合格宇宙内**：被排掉的 A 不得占用名次
        self.assertTrue(pd.isna(brow.loc['A', 'rank']))
        # 两条路径对同一轮次必须给出同一组入选者
        self.assertEqual(incremental, sorted(brow.index[brow.selected.astype(bool)]))


if __name__ == '__main__':
    unittest.main()
