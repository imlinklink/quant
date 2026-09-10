"""DecisionEngine 测试（§12 / §20.2）：输入先持久化、fail-closed、权限裁剪、幂等。"""
import tempfile
import time
import unittest
from pathlib import Path

from mutifactor.llm.contracts.common import build_evidence_item
from mutifactor.llm.contracts.entry_v2 import build_entry_templates
from scripts.live_trading.decision_engine import DecisionEngine
from scripts.live_trading.decision_ledger.decision_run_store import build_context
from scripts.live_trading.decision_ledger.event_store import utc
from scripts.live_trading.position_registry import PositionRegistry

PLAN = {'plan_id': 'plan1', 'stock_code': 'US.AAPL',
        'risk': {'initial_stop': 90.0}, 'entry_constraints': {'price': 100.0}}


def _ev(eid, summary, subject='US.AAPL', cluster='c1'):
    return build_evidence_item(
        evidence_id=eid, subject_code=subject, kind='fundamental', source='internal:test',
        source_grade=2, summary=summary, observed_at=time.time() - 60, cluster_id=cluster)


def _valid_entry_raw():
    return {
        'status': 'complete', 'action': 'execute_now', 'template_id': 'plan1:standard',
        'confidence': 'high', 'reason_codes': ['OPTIONS_CONFIRM'],
        'facts': [{'text': '财报超预期，营收同比增长 20%', 'claim_type': 'fact',
                   'evidence_ids': ['e1']}],
        'inferences': [{'text': '基本面转强', 'claim_type': 'inference', 'evidence_ids': ['e1']}],
        'counterevidence': [{'text': '估值处于历史高位', 'claim_type': 'counterevidence',
                             'evidence_ids': ['e2']}],
        'missing_information': [],
        'selected_review_trigger_ids': [],
        'thesis_seed': {'summary': '营收超预期', 'evidence_ids': ['e1'],
                        'invalidation_condition_ids': []},
    }


class _FakeAdvisor:
    model = 'test-model'
    last_metadata = {}


class DecisionEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 'state.db', 'DRY-RUN')
        self.calls = []
        self.engine = DecisionEngine(
            self.registry, advisor=_FakeAdvisor(),
            call_model=lambda contract, packet: self._call(contract, packet))

    def _call(self, contract, packet):
        self.calls.append(packet)
        return self._raw

    def _context(self, subject_id='sig1'):
        versions = {'packet_schema': 'entry-v2', 'prompt': 'entry-v2',
                    'output_schema': 'entry-v2', 'feature': 'feature-v2',
                    'rule': 'rule-v2', 'permission': 'permission-v2', 'model_id': 'test-model'}
        model = {'provider': 'test', 'model_id': 'test-model', 'temperature': 0.0,
                 'timeout_seconds': 30}
        return build_context(role='entry', subject_type='signal', subject_id=subject_id,
                             account_scope='DRY-RUN', as_of=utc(), versions=versions,
                             model=model, market_session='regular')

    def _packet(self, subject_id='sig1'):
        templates = build_entry_templates(
            plan=PLAN, standard_quantity=100, entry_price=100.0, initial_stop=90.0,
            expires_at='2999-01-01T00:00:00+00:00')
        return {
            'context': self._context(subject_id),
            'signal': {'signal_id': subject_id, 'strategy': 'donchian', 'rule_score': 0.8,
                       'rule_reasons': ['x']},
            'selection_context': {},
            'plan': PLAN,
            'templates': templates,
            'evidence': [_ev('e1', '财报超预期，营收同比增长 20%'),
                         _ev('e2', '估值处于历史高位', cluster='c2')],
            'portfolio': {},
            'quality_gate': {'status': 'pass', 'allowed_uses': ['rank', 'entry']},
        }

    def test_input_persisted_before_model_and_shadow_effective(self):
        self._raw = _valid_entry_raw()
        result = self.engine.decide_entry(self._packet())
        self.assertEqual(result.status, 'validated')
        # shadow 权限：有效动作 = 规则基线
        self.assertEqual(result.effective_action, 'rule_baseline')
        self.assertEqual(result.model_action, 'execute_now')
        self.assertEqual(len(self.calls), 1)
        # 输入快照已落库
        snap = self.engine.store.get_snapshot('entry_input', result.input_snapshot_id, 1)
        self.assertIsNotNone(snap)
        # run 投影存在
        run = self.engine.store.get_run(result.decision_id)
        self.assertIsNotNone(run)
        self.assertEqual(run['status'], 'validated')

    def test_model_failure_fail_closed(self):
        self._raw = None
        packet = self._packet('sig2')
        result = self.engine.decide_entry(packet)
        self.assertEqual(result.status, 'failed')
        self.assertIsNone(result.effective_action)
        self.assertIsNone(result.validated_output)
        # 普通 decide 对正式失败结果幂等恢复；显式重试必须走独立 retry/rerun 接口。
        again = self.engine.decide_entry(packet)
        self.assertEqual(again.status, 'failed')
        self.assertEqual(again.decision_id, result.decision_id)
        self.assertEqual(len(self.calls), 1)

    def test_validation_failure_fail_closed(self):
        self._raw = {'status': 'complete', 'action': 'execute_now', 'template_id': 'nope',
                     'confidence': 'high', 'reason_codes': [], 'facts': [], 'inferences': [],
                     'counterevidence': [], 'missing_information': []}
        result = self.engine.decide_entry(self._packet('sig3'))
        self.assertEqual(result.status, 'failed')
        self.assertTrue(result.validation_errors)

    def test_idempotent_same_decision(self):
        self._raw = _valid_entry_raw()
        packet = self._packet('sig4')
        r1 = self.engine.decide_entry(packet)
        r2 = self.engine.decide_entry(packet)
        self.assertEqual(r1.decision_id, r2.decision_id)
        self.assertEqual(r1.status, r2.status)
        # 第二次不重新调用模型
        self.assertEqual(len(self.calls), 1)

    def test_constrained_action_effective(self):
        self._raw = _valid_entry_raw()
        engine = DecisionEngine(
            self.registry, advisor=_FakeAdvisor(),
            config={'llm_permissions': {'_default': 'shadow',
                                        'entry_review': 'constrained_action',
                                        'plan_template': 'constrained_action',
                                        'position_scale': 'constrained_action'}},
            call_model=lambda contract, packet: self._raw)
        result = engine.decide_entry(self._packet('sig5'))
        self.assertEqual(result.effective_action, 'execute_now')

    def test_plan_template_shadow_blocks_auto_execute(self):
        # 仅 entry_review 升级、plan_template/position_scale 保持 shadow → 整体 shadow
        self._raw = _valid_entry_raw()
        engine = DecisionEngine(
            self.registry, advisor=_FakeAdvisor(),
            config={'llm_permissions': {'_default': 'shadow',
                                        'entry_review': 'constrained_action'}},
            call_model=lambda contract, packet: self._raw)
        result = engine.decide_entry(self._packet('sig6'))
        self.assertEqual(result.effective_action, 'rule_baseline')

    def test_context_role_must_match_entrypoint(self):
        self._raw = _valid_entry_raw()
        packet = self._packet('sig-role')
        packet['context']['role'] = 'position'
        with self.assertRaisesRegex(ValueError, 'role 与调用入口不一致'):
            self.engine.decide_entry(packet)

    def test_context_scope_must_match_registry(self):
        self._raw = _valid_entry_raw()
        packet = self._packet('sig-scope')
        packet['context']['account_scope'] = 'OTHER'
        with self.assertRaisesRegex(ValueError, 'account_scope 与当前账户不一致'):
            self.engine.decide_entry(packet)

    def test_retry_after_failure_creates_new_attempt_and_recovers(self):
        self._raw = None
        packet = self._packet('sig-retry')
        r1 = self.engine.decide_entry(packet)
        self.assertEqual(r1.status, 'failed')
        # 显式重试：同输入 → 同 decision_id，但新 attempt
        self._raw = _valid_entry_raw()
        r2 = self.engine.retry_entry(packet)
        self.assertEqual(r2.status, 'validated')
        self.assertEqual(r2.decision_id, r1.decision_id)
        self.assertNotEqual(r2.attempt_id, r1.attempt_id)
        self.assertEqual(len(self.calls), 2)
        # 之后普通 decide 恢复最新（成功）结果，不再调用模型
        r3 = self.engine.decide_entry(packet)
        self.assertEqual(r3.status, 'validated')
        self.assertEqual(r3.effective_action, r2.effective_action)
        self.assertEqual(len(self.calls), 2)

    def test_retry_reuses_permission_snapshot(self):
        self._raw = None
        packet = self._packet('sig-retry-perm')
        r1 = self.engine.decide_entry(packet)
        self.assertEqual(r1.status, 'failed')
        snap1 = self.engine.guard.load_permission_snapshot(r1.decision_id)
        self.assertIsNotNone(snap1)
        # 重试沿用原权限快照，不重新生成带新时间的快照
        self._raw = _valid_entry_raw()
        self.engine.retry_entry(packet)
        snap2 = self.engine.guard.load_permission_snapshot(r1.decision_id)
        self.assertEqual(snap2['as_of'], snap1['as_of'])
        # validated_decision 快照按版本递增（version 2），最新版本可被恢复
        latest = self.engine.store.get_snapshot_latest('validated_decision', r1.decision_id)
        self.assertEqual(latest['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
