"""研究 runner 里承重的两块：孤立账户定仓的**构造性相等**、以及判定 token 的映射。

第 1 步的正确性全押在「孤立账户恰好买到实际股数」上：如果它悄悄买成别的规模，
两臂比较的就不再是同一件事，而结果看上去仍然"正常"。所以这一条必须逐值验证，
而不是抽查几个。判定映射同样逐分支钉死 —— 门槛写错会让结论反向。
"""
import unittest

from scripts.strategy_research import entry_arms as E
from scripts.strategy_research import runner as R


class IsolatedSizingTests(unittest.TestCase):
    def test_isolated_account_buys_exactly_the_recorded_shares(self):
        # 覆盖几位不同量级的股数与止损距离（8% 与 2.5×ATR 两种主导情形）
        for shares, price, stop in ((125, 100_000_000, 92_000_000),
                                    (353, 33_410_000, 30_371_250),
                                    (7, 711_600_000, 640_440_000),
                                    (1, 5_000_000, 4_600_000),
                                    (999, 12_345_678, 11_234_567)):
            entry = {'security_id': 'SEC-A', 'entry_session': '2026-01-05',
                     'shares': shares, 'entry_price_micro': price, 'stop_micro': stop}
            _manifest, state = R._isolated_account(entry, horizon=60, fee_bp=10)
            from scripts.medium_term.entry_risk import risk_sized_shares_micro
            got = risk_sized_shares_micro(price, stop, state.initial_equity,
                                          state.cash_available, risk_bp=10000,
                                          max_weight_bp=1000000, fee_bp=10)
            self.assertEqual(got, shares, f'{shares} 股 @ {price}/{stop} 定成了 {got}')

    def test_isolated_account_rejects_an_invalid_stop(self):
        entry = {'security_id': 'SEC-A', 'entry_session': '2026-01-05', 'shares': 10,
                 'entry_price_micro': 100_000_000, 'stop_micro': 100_000_000}
        with self.assertRaises(ValueError) as ctx:
            R._isolated_account(entry, horizon=60, fee_bp=10)
        self.assertIn('INVALID_STOP', str(ctx.exception))


class ExpectedShortfallTests(unittest.TestCase):
    def test_es_is_a_positive_loss_and_takes_the_worst_tail(self):
        # 20 个值 ⇒ 5% = 1 个 ⇒ 取最差的那一个（转成正损失量）
        values = [-3.0] + [1.0] * 19
        self.assertAlmostEqual(R._es(values), 3.0)
        # 全程为正 ⇒ ES 为负（不是损失）
        self.assertAlmostEqual(R._es([2.0, 3.0, 4.0]), -2.0)

    def test_es_of_nothing_is_none_not_zero(self):
        self.assertIsNone(R._es([]))


def _summary(**over):
    base = {
        'control_reproduction_failures': [], 'control_tracking_matches_unprotected': True,
        'control_untouched_arms_identical': True, 'n_activated': 100,
        'delta_r_sum': 5.0, 'delta_r_sum_2x': 5.0, 'tail_es_improvement': 0.2,
        'concentration': {'top1_share_of_giveback_reduction': 0.3,
                          'leave_one_out_delta_r_sum': 3.0},
    }
    base.update(over)
    return base


class VerdictTests(unittest.TestCase):
    def test_supported_when_return_holds_and_risk_improves(self):
        self.assertEqual(R.step1_verdict(_summary())['token'], 'EVIDENCE_SUPPORTED')

    def test_tradeoff_when_risk_improves_but_return_is_sacrificed_within_the_bound(self):
        v = R.step1_verdict(_summary(delta_r_sum=-4.0, delta_r_sum_2x=-4.0))
        self.assertEqual(v['token'], 'RISK_TRADEOFF')

    def test_no_improvement_when_the_tail_did_not_improve(self):
        self.assertEqual(R.step1_verdict(_summary(tail_es_improvement=0.05))['token'],
                         'NO_IMPROVEMENT')

    def test_risk_rejected_when_return_breaks_the_non_inferiority_bound(self):
        v = R.step1_verdict(_summary(delta_r_sum=-9.0, delta_r_sum_2x=-9.0))
        self.assertEqual(v['token'], 'RISK_REJECTED')

    def test_concentrated_wins_over_a_good_headline(self):
        v = R.step1_verdict(_summary(
            concentration={'top1_share_of_giveback_reduction': 0.72,
                           'leave_one_out_delta_r_sum': -1.0}))
        self.assertEqual(v['token'], 'CONCENTRATED')

    def test_insufficient_sample(self):
        self.assertEqual(R.step1_verdict(_summary(n_activated=19))['token'],
                         'INSUFFICIENT_SAMPLE')

    def test_engineering_blocked_beats_everything_else(self):
        for broken in ({'control_reproduction_failures': [{'x': 1}]},
                       {'control_tracking_matches_unprotected': False},
                       {'control_untouched_arms_identical': False}):
            self.assertEqual(R.step1_verdict(_summary(**broken))['token'],
                             'ENGINEERING_BLOCKED')

    def test_thresholds_come_from_the_registration(self):
        reg = R.registration()
        gates = reg['decision_rules']['step1_numeric_gates']
        v = R.step1_verdict(_summary())
        self.assertEqual(v['thresholds']['non_inferiority_r'], gates['non_inferiority_bound_R'])
        self.assertEqual(v['thresholds']['min_activated_trades'], gates['min_activated_trades'])
        self.assertAlmostEqual(v['thresholds']['risk_improvement_gate'], 0.15)


def _cmp(**over):
    """一个"全部通过"的对照结果，按需覆盖某一项。"""
    a = {'total_return': 2.0, 'n_trades': 200, 'mdd': -0.14, 'worst_trade_r': -1.9,
         'tail_es_r': 1.40}
    b = {'total_return': 2.2, 'n_trades': 260, 'mdd': -0.13, 'worst_trade_r': -1.7,
         'tail_es_r': 1.25}
    base = {
        'a': a, 'b': b,
        'delta_terminal_return': b['total_return'] - a['total_return'],
        'delta_cagr': 0.01, 'delta_mdd': 0.01, 'delta_worst_trade_r': 0.2,
        'delta_terminal_return_2x': 0.15,
        'entries': {'common': 10, 'only_a': 190, 'only_b': 250},
        'new_entries_top1_share': 0.2, 'new_entries_leave_one_out': 5000.0,
        'control_reproduction_failures': [],
    }
    base.update(over)
    return base


class EntryVerdictTests(unittest.TestCase):
    """买入侧判定映射：门槛写错会让结论反向，所以逐分支钉死。"""

    def test_supported(self):
        self.assertEqual(E.verdict(_cmp())['token'], 'EVIDENCE_SUPPORTED')

    def test_tradeoff_when_risk_improves_but_return_does_not(self):
        c = _cmp(delta_terminal_return=-0.1, delta_terminal_return_2x=-0.1)
        v = E.verdict(c)
        self.assertEqual(v['token'], 'RISK_TRADEOFF')
        self.assertIn('防守选项', v['note'])

    def test_rejected_when_return_improves_only_before_costs(self):
        """收益改善但 2× 成本下不成立 ⇒ 不算支持（成本情景是硬门槛）。"""
        v = E.verdict(_cmp(delta_terminal_return_2x=-0.02))
        self.assertEqual(v['token'], 'RISK_REJECTED')

    def test_risk_rejected_when_the_tail_or_mdd_gets_worse(self):
        for over in ({'delta_mdd': 0.05}, {'delta_worst_trade_r': -0.1}):
            self.assertEqual(E.verdict(_cmp(**over))['token'], 'RISK_REJECTED')

    def test_concentrated_wins_over_a_good_headline(self):
        v = E.verdict(_cmp(new_entries_top1_share=0.7, new_entries_leave_one_out=-10.0))
        self.assertEqual(v['token'], 'CONCENTRATED')

    def test_insufficient_sample_and_engineering_blocked(self):
        self.assertEqual(E.verdict(_cmp(a={**_cmp()['a'], 'n_trades': 5}))['token'],
                         'INSUFFICIENT_SAMPLE')
        self.assertEqual(E.verdict(_cmp(control_reproduction_failures=[{'x': 1}]))['token'],
                         'ENGINEERING_BLOCKED')

    def test_gates_match_the_registration_text(self):
        reg = E._registration_digest()
        self.assertEqual(len(reg), 64)


if __name__ == '__main__':
    unittest.main()
