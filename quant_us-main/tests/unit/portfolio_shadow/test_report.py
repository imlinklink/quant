"""日报 + 配对绩效 + 冻结前瞻协议测试（PR6）。"""
import tempfile
import unittest
from pathlib import Path

from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.report import (daily_report, paired_performance,
                                              render_markdown)
from scripts.portfolio_shadow.schema import Manifest, Opportunity, to_micro
from scripts.portfolio_shadow.store import ShadowStore


def manifest(**overrides):
    d = dict(experiment_id='exp1', status='DRAFT', parent_strategy_id='B3', parent_version='1',
             parent_code_hash='abc', universe_id='u', universe_hash='uh',
             account_scopes=('SHADOW:exp1:R', 'SHADOW:exp1:L'), initial_cash=to_micro(100000),
             risk_policy={'single_position_risk_bp': 100, 'max_weight_bp': 2000,
                          'max_positions': 5},
             execution_policy={'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': 60},
             llm_policy={'overlay': 'fixed_pass'}, calendar_version='v1',
             evaluation_protocol={'main_metric': 'L_minus_R_return',
                                  'enrollment_window': '3-6 months',
                                  'review_date': '2026-12-31',
                                  'cost_allocation': 'L_pays_model_cost'})
    d.update(overrides)
    return Manifest(**d)


def opp(sid, sess):
    return Opportunity(experiment_id='exp1', security_id=sid, source_candidate_id=sid,
                       parent_version='1', signal_session='2026-01-01',
                       observed_at='2026-01-01T00:00:00+00:00', planned_execution_session=sess,
                       rank=1, entry_rule='b3', stop_reference={'atr14_micro': to_micro(2.0)},
                       exit_policy_id='H60', input_hash='h')


class FreezeProtocolTests(unittest.TestCase):
    def test_freeze_requires_evaluation_protocol(self):
        m = manifest(evaluation_protocol={})
        errors = m.validate()
        self.assertTrue(any('evaluation_protocol.main_metric' in e for e in errors))
        with self.assertRaises(ValueError):
            m.freeze('2026-01-02')

    def test_freeze_passes_with_full_protocol(self):
        self.assertEqual(manifest().validate(), [])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.store.save_experiment(manifest().freeze('2026-01-02'))
        m = manifest().freeze('2026-01-02')
        bars = {'SEC-A': {'open': to_micro(100), 'high': to_micro(101),
                          'low': to_micro(99), 'close': to_micro(100.5)}}
        # R 入场，L 因 VETO 不入场
        r = step(new_account_state('SHADOW:exp1:R', m.initial_cash), session='2026-01-05',
                 bars=bars, corporate_actions=[], intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        self.store.save_state('SHADOW:exp1:R', r.state, r.nav, r.events)
        l = step(new_account_state('SHADOW:exp1:L', m.initial_cash), session='2026-01-05',
                 bars=bars, corporate_actions=[], intents=[], manifest=m, model_cost=100)
        self.store.save_state('SHADOW:exp1:L', l.state, l.nav, l.events)
        from scripts.portfolio_shadow.schema import Application
        self.store.put_application(Application(
            scope='SHADOW:exp1:L', opportunity_id=opp('SEC-A', '2026-01-05').opportunity_id(),
            action='VETO', reason_code='MATERIAL_COMPANY_EVENT_RISK', decision_id='',
            as_of='2026-01-05T13:20:00+00:00', applied=True, model_cost=100))
        self.store.put_opportunity(opp('SEC-A', '2026-01-05'))

    def test_daily_report_counts_scope_differences(self):
        r = daily_report(self.store, manifest())
        r_acct = r['accounts']['SHADOW:exp1:R']
        l_acct = r['accounts']['SHADOW:exp1:L']
        self.assertEqual(len(r_acct['positions']), 1)
        self.assertEqual(len(l_acct['positions']), 0)
        self.assertGreater(r_acct['equity'], l_acct['equity'])
        self.assertAlmostEqual(l_acct['model_cost'], 100 / 1e6)  # 100 微美元
        self.assertEqual(r['funnel']['total'], 1)
        self.assertEqual(r['applications']['by_scope_action']['L:VETO'], 1)

    def test_paired_performance_l_minus_r(self):
        p = paired_performance(self.store, manifest())
        self.assertEqual(p['common_sessions'], 1)
        self.assertLess(p['L_full_cost_return'], p['R_full_cost_return'])
        self.assertEqual(p['L_VETO'], 1)
        self.assertGreater(p['L_model_cost'], 0)
        self.assertEqual(p['R_model_cost'], 0)

    def test_store_read_methods_roundtrip(self):
        self.assertEqual(len(self.store.daily_nav('SHADOW:exp1:R')), 1)
        self.assertEqual(len(self.store.applications('SHADOW:exp1:L')), 1)


class DrawdownTests(unittest.TestCase):
    def test_max_drawdown_includes_initial_capital(self):
        from scripts.portfolio_shadow.report import _max_drawdown
        # 初始 10 万，首日净值 9 万 → MDD 应为 -10%（不是 0）
        self.assertAlmostEqual(_max_drawdown([{'equity': 90000}], 'equity', initial=100000), -0.10)
        # 净值回到 11 万 → 无回撤
        self.assertAlmostEqual(_max_drawdown([{'equity': 110000}], 'equity', initial=100000), 0.0)


if __name__ == '__main__':
    unittest.main()


class CostStatusReportTests(unittest.TestCase):
    """成本不可知时报告必须标 PROVISIONAL，收益/MDD 只能表述为净值上界。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.m = manifest().freeze('2026-01-02')
        self.store.save_experiment(self.m)
        bars = {'SEC-A': {'open': to_micro(100), 'high': to_micro(101),
                          'low': to_micro(99), 'close': to_micro(100.5)}}
        for scope in self.m.account_scopes:
            res = step(new_account_state(scope, self.m.initial_cash), session='2026-01-05',
                       bars=bars, corporate_actions=[], intents=[opp('SEC-A', '2026-01-05')],
                       manifest=self.m, model_cost_uncertain=('a1',))
            self.store.save_state(scope, res.state, res.nav, res.events)

    def test_daily_report_marks_cost_provisional(self):
        acct = daily_report(self.store, self.m)['accounts']['SHADOW:exp1:L']
        self.assertEqual(acct['cost_status'], 'PROVISIONAL')
        self.assertEqual(acct['model_cost_uncertain_count'], 1)

    def test_paired_performance_flags_upper_bound(self):
        paired = paired_performance(self.store, self.m)
        self.assertEqual(paired['L_cost_status'], 'PROVISIONAL')
        self.assertTrue(paired['full_cost_is_upper_bound'])

    def test_render_marks_provisional_with_bound_wording(self):
        text = render_markdown(daily_report(self.store, self.m),
                               paired_performance(self.store, self.m))
        self.assertIn('暂定', text)
        self.assertIn('待扣非负费用后的净值上界', text)

    def test_render_tolerates_no_common_sessions(self):
        """paired_performance 无公共 session 时走早退分支，缺 L_minus_R_return 等键。"""
        tmp = tempfile.mkdtemp()
        store = ShadowStore(Path(tmp) / 'ledger.sqlite3', 'exp2')
        m = manifest(experiment_id='exp2',
                     account_scopes=('SHADOW:exp2:R', 'SHADOW:exp2:L')).freeze('2026-01-02')
        store.save_experiment(m)
        # 只有 R 有状态 → 无公共 session
        bars = {'SEC-A': {'open': to_micro(100), 'high': to_micro(101),
                          'low': to_micro(99), 'close': to_micro(100.5)}}
        res = step(new_account_state('SHADOW:exp2:R', m.initial_cash), session='2026-01-05',
                   bars=bars, corporate_actions=[], intents=[], manifest=m)
        store.save_state('SHADOW:exp2:R', res.state, res.nav, res.events)
        paired = paired_performance(store, m)
        self.assertEqual(paired['common_sessions'], 0)
        text = render_markdown(daily_report(store, m), paired)  # 不得抛 TypeError
        self.assertIn('L−R 全成本收益差=n/a', text)
