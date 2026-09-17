"""日报 + 配对绩效 + 冻结前瞻协议测试（PR6）。"""
import tempfile
import unittest
from pathlib import Path

from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.report import (daily_report, decision_trace, entry_metrics,
                                              paired_performance, render_markdown,
                                              render_trace)
from scripts.portfolio_shadow.schema import (LADDER_KEYS, Application, Manifest,
                                              Opportunity, to_micro)
from scripts.portfolio_shadow.store import SHADOW_SCHEMA_VERSION, ShadowStore


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
            as_of='2026-01-05T13:20:00+00:00', execution_applied=True, model_cost=100))
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


class KnowledgeCutoffFreezeTests(unittest.TestCase):
    """真实模型必须显式声明训练数据截止，否则不许 freeze —— 这是对 L−R 的一阶威胁。"""

    def test_real_model_requires_declared_knowledge_cutoff(self):
        m = manifest(llm_policy={'overlay': 'entry_veto', 'evidence_mode': 'strict',
                                 'use_real_model': True, 'evidence_window_days': 30, 'evidence_max_events': 50})
        errors = m.validate()
        self.assertTrue(any('knowledge_cutoff' in e for e in errors), errors)
        with self.assertRaises(ValueError):
            m.freeze('2026-01-02')

    def test_explicit_unknown_is_accepted_so_it_stays_visible(self):
        m = manifest(llm_policy={'overlay': 'entry_veto', 'evidence_mode': 'strict',
                                 'use_real_model': True, 'knowledge_cutoff': 'unknown',
                                 'evidence_window_days': 30, 'evidence_max_events': 50})
        self.assertEqual(m.validate(), [])

    def test_fixture_model_needs_no_cutoff(self):
        self.assertEqual(manifest().validate(), [])   # overlay=fixed_pass


class ReportDisclosureTests(unittest.TestCase):
    """报告必须披露账本版本与模型知识截止 —— 否则读报告的人无法判断数字在什么前提下成立。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.m = manifest(llm_policy={'overlay': 'entry_veto', 'evidence_mode': 'strict',
                                      'use_real_model': True,
                                      'knowledge_cutoff': '2025-06-01T00:00:00+00:00',
                                      'evidence_window_days': 30, 'evidence_max_events': 50}
                          ).freeze('2026-01-02')
        self.store.save_experiment(self.m)

    def test_report_discloses_schema_version_and_cutoff(self):
        rep = daily_report(self.store, self.m)
        self.assertEqual(rep['schema_version'], SHADOW_SCHEMA_VERSION)
        self.assertEqual(rep['llm_policy']['knowledge_cutoff'], '2025-06-01T00:00:00+00:00')
        text = render_markdown(rep, paired_performance(self.store, self.m))
        self.assertIn('模型知识截止=2025-06-01T00:00:00+00:00', text)
        self.assertIn(f'schema=v{SHADOW_SCHEMA_VERSION}', text)


class UnknownPolicyKeyTests(unittest.TestCase):
    """拼错的键必须被指出来：它会被 dict.get 静默忽略，让安全门无声失效。"""

    def test_ladder_keys_match_risk_policy_defaults(self):
        from scripts.portfolio_shadow.risk_policy import _DEFAULTS
        self.assertEqual(set(LADDER_KEYS), set(_DEFAULTS))

    def test_misspelled_llm_policy_key_is_named(self):
        # 拼错的 knowledge_cutoff 原先只会让 freeze 门报「缺失」，操作者按提示补上
        # 另一个拼写 —— 真正的错字反而看不见
        m = manifest(llm_policy={'overlay': 'entry_veto', 'use_real_model': True,
                                 'knowledge_cutof': '2025-06-01'})
        errors = m.validate()
        self.assertTrue(any('knowledge_cutof' in e for e in errors), errors)
        self.assertTrue(any('llm_policy' in e for e in errors), errors)

    def test_unknown_keys_in_each_policy_dict_are_flagged(self):
        cases = {
            'risk_policy': {'single_position_risk_bp': 100, 'max_weight_bp': 2000,
                            'max_positions': 5, 'typo_here': 1},
            'execution_policy': {'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': 60,
                                 'max_wait_sesson': 20},
            'evaluation_protocol': {'main_metric': 'L_minus_R_return',
                                    'enrollment_window': '3-6 months',
                                    'review_date': '2026-12-31',
                                    'cost_allocation': 'L_pays_model_cost', 'extra': 1},
        }
        for key, value in cases.items():
            with self.subTest(key):
                errors = manifest(**{key: value}).validate()
                self.assertTrue(any(f'{key} 含未知键' in e for e in errors), errors)
                with self.assertRaises(ValueError):
                    manifest(**{key: value}).freeze('2026-01-02')

    def test_unknown_ladder_key_is_flagged(self):
        m = manifest(risk_policy={'single_position_risk_bp': 100, 'max_weight_bp': 2000,
                                  'max_positions': 5,
                                  'drawdown_ladder': {'reduced': 0.1, 'tpyo': 0.2}})
        errors = m.validate()
        self.assertTrue(any('drawdown_ladder' in e and 'tpyo' in e for e in errors), errors)

    def test_known_keys_still_pass(self):
        self.assertEqual(manifest().validate(), [])


class EvidenceModeFreezeTests(unittest.TestCase):
    """entry_veto 必须声明证据等级：诊断级证据上的 VETO 结论适用范围完全不同。"""

    def test_entry_veto_requires_declared_evidence_mode(self):
        m = manifest(llm_policy={'overlay': 'entry_veto'})
        errors = m.validate()
        self.assertTrue(any('evidence_mode' in e for e in errors), errors)
        with self.assertRaises(ValueError):
            m.freeze('2026-01-02')

    def test_invalid_evidence_mode_is_rejected(self):
        m = manifest(llm_policy={'overlay': 'entry_veto', 'evidence_mode': 'verified'})
        self.assertTrue(any('evidence_mode' in e for e in m.validate()))

    def test_fixed_pass_needs_no_evidence_mode(self):
        self.assertEqual(manifest().validate(), [])

    def test_report_renders_the_declared_evidence_mode(self):
        m = manifest(llm_policy={'overlay': 'entry_veto',
                                 'evidence_mode': 'diagnostic',
                                 'evidence_window_days': 30,
                                 'evidence_max_events': 50}).freeze('2026-01-02')
        tmp = tempfile.mkdtemp()
        store = ShadowStore(Path(tmp) / 'ledger.sqlite3', 'exp1')
        store.save_experiment(m)
        text = render_markdown(daily_report(store, m), paired_performance(store, m))
        self.assertIn('证据等级=diagnostic', text)


class EntryMetricsTests(unittest.TestCase):
    """设计 §8 三项指标：分母为 0 时返回 None，不把「没有对象可评」混成「评了全放行」。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.m = manifest().freeze('2026-01-02')
        self.store.save_experiment(self.m)
        self.l = 'SHADOW:exp1:L'

    def _opportunity(self, sid='SEC-A'):
        o = opp(sid, '2026-01-06')
        self.store.put_opportunity(o)
        return o.opportunity_id()

    def _packet(self, oid, mode='strict'):
        self.store.put_packet(oid, {'packet_id': f'pkt_{oid}', 'as_of': '2026-01-05T23:00:00Z',
                                    'rule_plan': {'entry_rule': 'b3'},
                                    'market_context': {'price': 100},
                                    'data_quality': {'level': 'OK'}, 'events': [],
                                    'evidence': {'evidence_mode': mode}})

    def _application(self, oid, action, *, decision_id='d1', model_id=None,
                     status='COMPLETED', gated=False, execution_applied=False):
        self.store.put_application(Application(
            scope=self.l, opportunity_id=oid, action=action, reason_code='R', decision_id=decision_id,
            as_of='2026-01-05T23:00:00Z', decision_frozen=True,
            execution_applied=execution_applied))
        if decision_id:
            self.store.put_job_run(decision_id, 1, status, {'model_id': model_id,
                                                            'gated': gated})
    def test_no_opportunities_is_not_a_failure(self):
        m = entry_metrics(self.store, self.m)
        self.assertIsNone(m['real_review_coverage'])
        self.assertIsNone(m['plan_change_rate'])
        self.assertEqual(m['eligible_for_review'], 0)

    def test_fixture_review_does_not_count_as_real_coverage(self):
        """夹具评审绝不能冒充「已接入真实模型」。"""
        oid = self._opportunity()
        self._packet(oid)
        self._application(oid, 'PASS', model_id='fixture')
        m = entry_metrics(self.store, self.m)
        self.assertEqual(m['eligible_for_review'], 1)
        self.assertEqual(m['real_model_reviews'], 0)
        self.assertEqual(m['real_review_coverage'], 0.0)
        self.assertTrue(m['note'])

    def test_real_model_attempt_counts_as_coverage(self):
        oid = self._opportunity()
        self._packet(oid)
        self._application(oid, 'PASS', model_id='deepseek-chat')
        m = entry_metrics(self.store, self.m)
        self.assertEqual(m['real_model_reviews'], 1)
        self.assertEqual(m['real_review_coverage'], 1.0)
        self.assertEqual(m['note'], '')

    def test_gated_call_is_not_a_real_review(self):
        """质量门短路根本没发起调用，不能算评审。"""
        oid = self._opportunity()
        self._packet(oid)
        self._application(oid, 'ABSTAIN', model_id='deepseek-chat', gated=True)
        self.assertEqual(entry_metrics(self.store, self.m)['real_model_reviews'], 0)

    def test_plan_change_rate_counts_veto(self):
        for sid, action in (('SEC-A', 'VETO'), ('SEC-B', 'PASS')):
            oid = self._opportunity(sid)
            self._packet(oid)
            self._application(oid, action, decision_id=f'd{sid}', model_id='deepseek-chat')
        m = entry_metrics(self.store, self.m)
        self.assertEqual(m['plan_change_count'], 1)
        self.assertEqual(m['plan_change_rate'], 0.5)

    def test_trackable_completion_counts_executed_and_missed_terminal(self):
        oid_a = self._opportunity('SEC-A')
        self._packet(oid_a)
        self._application(oid_a, 'PASS', decision_id='da', model_id='deepseek-chat',
                          execution_applied=True)
        oid_b = self._opportunity('SEC-B')
        self._packet(oid_b)
        self._application(oid_b, 'PASS', decision_id='db', model_id='deepseek-chat')
        self.store.set_opportunity_terminal(oid_b, 'MISSED_EXECUTION', '2026-01-06')
        m = entry_metrics(self.store, self.m)
        self.assertEqual(m['approved_applications'], 2)
        self.assertEqual(m['execution_completed'], 1)
        self.assertEqual(m['execution_failed_terminal'], 1)
        self.assertEqual(m['trackable_completion_rate'], 1.0)


class DecisionTraceTests(unittest.TestCase):
    """设计 §8：每次决策都要能展示规则原计划、证据、模型动作、最终动作、执行结果、费用。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.m = manifest().freeze('2026-01-02')
        self.store.save_experiment(self.m)

    def test_trace_covers_the_whole_chain(self):
        o = opp('SEC-A', '2026-01-06')
        oid = o.opportunity_id()
        self.store.put_opportunity(o)
        self.store.put_packet(oid, {
            'packet_id': 'pkt1', 'as_of': '2026-01-05T23:00:00Z',
            'rule_plan': {'entry_rule': 'b3', 'parent_version': '1',
                          'rule_reason_codes': ['pullback']},
            'market_context': {'price': 100, 'price_unit': 'micro_usd'},
            'data_quality': {'level': 'OK'},
            'events': [{'summary': '公司下调指引', 'source_url': 'https://example.com/8k'}],
            'evidence': {'evidence_mode': 'strict'}})
        self.store.put_application(Application(
            scope='SHADOW:exp1:L', opportunity_id=oid, action='ABSTAIN',
            reason_code='INVALID_OUTPUT', decision_id='d1', as_of='2026-01-05T23:00:00Z',
            decision_frozen=True, raw_action='VETO', model_cost=77, cost_uncertain=True))
        self.store.put_job_run('d1', 1, 'COMPLETED', {
            'model_id': 'deepseek-chat', 'validation_errors': ['REASON_NOT_ALLOWED'],
            'action': 'ABSTAIN'})
        trace = decision_trace(self.store, oid, self.m.account_scopes)
        self.assertEqual(trace['rule_plan']['rule_reason_codes'], ['pullback'])
        self.assertEqual(trace['evidence_mode'], 'strict')
        self.assertEqual(trace['events'][0]['source_url'], 'https://example.com/8k')
        acct = trace['accounts']['SHADOW:exp1:L']
        self.assertEqual(acct['raw_action'], 'VETO')
        self.assertEqual(acct['degraded_from'], 'VETO')     # 被校验降级
        self.assertEqual(acct['attempt_status'], 'COMPLETED')
        self.assertEqual(acct['attempt_errors'], ['REASON_NOT_ALLOWED'])
        self.assertTrue(acct['cost_uncertain'])
        text = render_trace(trace)
        self.assertIn('规则原计划', text)
        self.assertIn('VETO', text)
        self.assertIn('成本未知，待补记', text)
