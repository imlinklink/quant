"""P0/P1 修复的回归测试（对应 code review 的 8 个问题）。"""
import tempfile
import time
import unittest
import sqlite3
from pathlib import Path

from mutifactor.llm.contracts.common import build_evidence_item, utc as common_utc
from mutifactor.llm.contracts.entry_v2 import build_entry_templates, validate_entry_v2
from mutifactor.llm.contracts.position_v2 import (
    build_position_action_templates, validate_position_v2,
)
from mutifactor.llm.validators.action import applicable_permissions
from scripts.live_trading.decision_engine import DecisionEngine
from scripts.live_trading.decision_ledger.decision_run_store import (
    DecisionRunStore, build_context,
)
from scripts.live_trading.decision_ledger.event_store import digest, utc
from scripts.live_trading.decision_ledger.outcome_jobs import OutcomeSettlement
from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.project_decision_metrics import ProjectDecisionMetrics
from scripts.live_trading.replay_decision import ReplayEngine

PLAN = {'plan_id': 'plan1', 'stock_code': 'US.AAPL',
        'risk': {'initial_stop': 90.0}, 'entry_constraints': {'price': 100.0}}


def _ev(eid, summary, subject='US.AAPL', cluster='c1', kind='fundamental'):
    return build_evidence_item(evidence_id=eid, subject_code=subject, kind=kind,
                               source='internal:test', source_grade=2, summary=summary,
                               observed_at=time.time() - 60, cluster_id=cluster)


class _FakeAdvisor:
    model = 'test-model'
    last_metadata = {}


class FutureEvidenceTests(unittest.TestCase):
    """issue 2：三个角色都拒绝未来证据。"""

    def _entry_packet(self, evidence):
        return {'plan': PLAN,
                'templates': build_entry_templates(plan=PLAN, standard_quantity=100,
                                                   entry_price=100.0, initial_stop=90.0,
                                                   expires_at='2999-01-01T00:00:00+00:00'),
                'evidence': evidence,
                'quality_gate': {'status': 'pass', 'allowed_uses': ['entry']},
                'context': {'as_of': common_utc()}}

    def test_entry_rejects_future(self):
        fut = _ev('e1', '财报超预期，营收同比增长 20%')
        fut['effective_at'] = common_utc(time.time() + 100000)
        fut['observed_at'] = common_utc(time.time() + 100000)
        raw = {'status': 'complete', 'action': 'execute_now', 'template_id': 'plan1:standard',
               'confidence': 'high', 'reason_codes': [],
               'facts': [{'text': '财报超预期，营收同比增长 20%', 'claim_type': 'fact',
                          'evidence_ids': ['e1']}],
               'inferences': [], 'counterevidence': [], 'missing_information': [],
               'selected_review_trigger_ids': []}
        errs = validate_entry_v2(raw, self._entry_packet([fut]))
        self.assertTrue(any('未来证据' in e for e in errs))

    def test_position_rejects_future(self):
        fut = _ev('e1', '财报低于预期')
        fut['effective_at'] = common_utc(time.time() + 100000)
        fut['observed_at'] = common_utc(time.time() + 100000)
        packet = {'trade': {'trade_id': 't1', 'code': 'US.AAPL', 'direction': 'long',
                            'remaining_qty': 100.0},
                  'protection': {'active_stop': 90.0},
                  'new_evidence': [fut],
                  'allowed_actions': build_position_action_templates(
                      trade={'trade_id': 't1', 'code': 'US.AAPL', 'direction': 'long',
                             'remaining_qty': 100.0},
                      active_stop=90.0, expires_at='2999-01-01T00:00:00+00:00'),
                  'context': {'as_of': common_utc()}}
        raw = {'status': 'complete', 'thesis_state': 'WEAKENING', 'action': 'hold',
               'action_template_id': None, 'confidence': 'medium', 'reason_codes': [],
               'facts': [{'text': '财报低于预期', 'claim_type': 'fact', 'evidence_ids': ['e1']}],
               'inferences': [], 'counterevidence': [], 'missing_information': []}
        errs = validate_position_v2(raw, packet)
        self.assertTrue(any('未来证据' in e for e in errs))


class DecisionIdBindsInputTests(unittest.TestCase):
    """issue 3：不同输入生成不同 decision_id。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 's.db', 'DRY-RUN')
        self.store = DecisionRunStore(self.registry)

    def _ctx(self, subject_id='sig1'):
        versions = {'packet_schema': 'entry-v2', 'prompt': 'entry-v2', 'output_schema': 'entry-v2',
                    'feature': 'feature-v2', 'rule': 'rule-v2', 'permission': 'permission-v2',
                    'model_id': 'm1'}
        model = {'provider': 'test', 'model_id': 'm1', 'temperature': 0.0, 'timeout_seconds': 30}
        return build_context(role='entry', subject_type='signal', subject_id=subject_id,
                             account_scope='DRY-RUN', as_of=utc(), versions=versions,
                             model=model, market_session='regular')

    def test_same_subject_different_evidence_different_id(self):
        ctx = self._ctx()
        p1 = {'context': ctx, 'evidence': [_ev('e1', 'a')]}
        p2 = {'context': dict(ctx), 'evidence': [_ev('e1', 'b')]}  # 证据内容不同
        sid1 = self.store.save_input_snapshot('entry', ctx['subject_id'], p1)
        sid2 = self.store.save_input_snapshot('entry', ctx['subject_id'], p2)
        self.assertNotEqual(sid1, sid2)
        from scripts.live_trading.decision_ledger.decision_run_store import finalize_decision_id
        d1 = finalize_decision_id(account_scope='DRY-RUN', role='entry', subject_id=ctx['subject_id'],
                                  input_snapshot_id=sid1, versions=ctx['versions'], model_id='m1')
        d2 = finalize_decision_id(account_scope='DRY-RUN', role='entry', subject_id=ctx['subject_id'],
                                  input_snapshot_id=sid2, versions=ctx['versions'], model_id='m1')
        self.assertNotEqual(d1, d2)


class SnapshotSymmetryTests(unittest.TestCase):
    """issue 4：save_snapshot/get_snapshot 用同一 key。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 's.db', 'DRY-RUN')
        self.store = DecisionRunStore(self.registry)

    def test_roundtrip_by_key(self):
        self.store.save_snapshot('validated_decision', 'd1', {'status': 'complete'})
        self.assertEqual(self.store.get_snapshot('validated_decision', 'd1'),
                         {'status': 'complete'})


class OutcomeNoOverwriteTests(unittest.TestCase):
    """issue 5：selection 多股票 / entry 多模板不覆盖。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 's.db', 'DRY-RUN')
        self.settle = OutcomeSettlement(self.registry)

    def test_selection_two_codes_same_horizon(self):
        closes = [100, 101, 102, 103, 104, 105]
        self.settle.settle_selection('d1', 'A', closes)
        self.settle.settle_selection('d1', 'B', closes)
        with self.settle.events.transaction() as con:
            rows = con.execute(
                'SELECT subject_key FROM decision_outcomes_v2 WHERE account_scope=? AND decision_id=? AND horizon=?',
                (self.registry.namespace, 'd1', '1d')).fetchall()
        self.assertEqual(sorted(r[0] for r in rows), ['A', 'B'])

    def test_entry_templates_not_overwritten(self):
        templates = build_entry_templates(plan=PLAN, standard_quantity=100, entry_price=100.0,
                                          initial_stop=90.0,
                                          expires_at='2999-01-01T00:00:00+00:00')
        closes = [100, 102, 98, 95]
        self.settle.settle_entry('d2', 100.0, templates, closes)
        with self.settle.events.transaction() as con:
            rows = con.execute(
                'SELECT subject_key FROM decision_outcomes_v2 WHERE account_scope=? AND decision_id=?',
                (self.registry.namespace, 'd2')).fetchall()
        keys = {r[0] for r in rows}
        self.assertTrue({'standard', 'half_size', 'wait_for_confirmation', 'reject'} <= keys)

    def test_projection_v1_migration_preserves_existing_outcome(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'legacy.db'
        with sqlite3.connect(path) as con:
            con.executescript('''
                CREATE TABLE decision_projection_schema(version INTEGER PRIMARY KEY);
                INSERT INTO decision_projection_schema VALUES (1);
                CREATE TABLE decision_outcomes_v2 (
                    account_scope TEXT NOT NULL, decision_id TEXT NOT NULL,
                    horizon TEXT NOT NULL, label_as_of TEXT NOT NULL,
                    return_pct REAL, benchmark_return_pct REAL,
                    excess_return_pct REAL, mfe_pct REAL, mae_pct REAL,
                    realized_r REAL, data_quality TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(account_scope,decision_id,horizon));
                INSERT INTO decision_outcomes_v2 VALUES
                    ('DRY-RUN','legacy-d','1d','2026-01-02T00:00:00+00:00',
                     0.1,0.01,0.09,0.2,-0.05,1.0,'good','{"code":"A"}');
            ''')
        registry = PositionRegistry(path, 'DRY-RUN')
        store = DecisionRunStore(registry)
        with store.events.transaction() as con:
            row = con.execute(
                'SELECT subject_key,return_pct,mfe_pct,mae_pct,realized_r,body '
                'FROM decision_outcomes_v2 WHERE decision_id=?', ('legacy-d',)).fetchone()
        self.assertIsNotNone(row)
        self.assertTrue(row[0].startswith('legacy:'))
        self.assertEqual(row[1:], (0.1, 0.2, -0.05, 1.0, '{"code":"A"}'))


class MetricsQueryTests(unittest.TestCase):
    """issue 6：overview 不再报 a.role 列错误。"""

    def test_overview_runs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = PositionRegistry(Path(tmp.name) / 's.db', 'DRY-RUN')
        metrics = ProjectDecisionMetrics(registry)
        out = metrics.overview()  # 空库也应正常返回，不抛 OperationalError
        self.assertIn('by_role', out)


class SubPermissionGatingTests(unittest.TestCase):
    """issue 1：position 子权限独立 gate。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 's.db', 'DRY-RUN')

    def _position_packet(self, subject_id='t1'):
        templates = build_position_action_templates(
            trade={'trade_id': 't1', 'code': 'US.AAPL', 'direction': 'long',
                   'remaining_qty': 100.0},
            active_stop=90.0, expires_at='2999-01-01T00:00:00+00:00')
        versions = {'packet_schema': 'position-v2', 'prompt': 'position-v2',
                    'output_schema': 'position-v2', 'feature': 'feature-v2',
                    'rule': 'rule-v2', 'permission': 'permission-v2', 'model_id': 'm1'}
        model = {'provider': 'test', 'model_id': 'm1', 'temperature': 0.0, 'timeout_seconds': 30}
        ctx = build_context(role='position', subject_type='trade', subject_id=subject_id,
                            account_scope='DRY-RUN', as_of=utc(), versions=versions,
                            model=model, market_session='regular')
        return {'context': ctx,
                'trade': {'trade_id': 't1', 'code': 'US.AAPL', 'direction': 'long',
                          'remaining_qty': 100.0, 'entry_price': 100.0},
                'protection': {'active_stop': 90.0},
                'new_evidence': [_ev('e1', '财报低于预期'), _ev('e2', '估值已回落', cluster='c2')],
                'allowed_actions': templates,
                'quality_gate': {'status': 'pass', 'allowed_uses': ['position']}}

    @staticmethod
    def _exit_raw():
        return {'status': 'complete', 'thesis_state': 'INVALIDATED', 'action': 'exit',
                'action_template_id': 't1:exit', 'confidence': 'high',
                'reason_codes': ['THESIS_INVALIDATED'],
                'facts': [{'text': '财报低于预期', 'claim_type': 'fact', 'evidence_ids': ['e1']}],
                'inferences': [{'text': '基本面恶化', 'claim_type': 'inference', 'evidence_ids': ['e1']}],
                'counterevidence': [{'text': '估值已回落', 'claim_type': 'counterevidence',
                                     'evidence_ids': ['e2']}],
                'missing_information': [],
                'thesis_delta': {'added_evidence_ids': ['e1'], 'removed_evidence_ids': [],
                                 'summary': '恶化'}}

    def test_exit_requires_auto_exit_thesis(self):
        raw = self._exit_raw()
        engine = DecisionEngine(
            self.registry, advisor=_FakeAdvisor(),
            config={'llm_permissions': {'_default': 'shadow', 'exit_review': 'constrained_action'}},
            call_model=lambda c, p: raw)
        result = engine.decide_position(self._position_packet())
        # auto_exit_thesis 仍是 shadow → 整体最严格为 shadow → hold
        self.assertEqual(result.effective_action, 'hold')

    def test_exit_auto_with_both_permissions(self):
        raw = self._exit_raw()
        engine = DecisionEngine(
            self.registry, advisor=_FakeAdvisor(),
            config={'llm_permissions': {'_default': 'shadow', 'exit_review': 'constrained_action',
                                        'auto_exit_thesis': 'constrained_action'}},
            call_model=lambda c, p: raw)
        result = engine.decide_position(self._position_packet('t2'))
        self.assertEqual(result.effective_action, 'exit')


class ReplayValidationTests(unittest.TestCase):
    """issue 7：replay validate 重算哈希 + 真校验。"""

    def test_missing_snapshot_not_validated(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = PositionRegistry(Path(tmp.name) / 's.db', 'DRY-RUN')
        store = DecisionRunStore(registry)
        # 写一条 input_snapshot_id 指向不存在快照的 run
        store.save_run({'decision_id': 'decision_x', 'role': 'entry', 'subject_type': 'signal',
                        'subject_id': 'sig', 'as_of': utc(), 'status': 'validated',
                        'input_snapshot_id': 'does_not_exist',
                        'prompt_version': 'entry-v2', 'output_schema_version': 'entry-v2',
                        'feature_version': 'f', 'rule_version': 'r', 'permission_version': 'p',
                        'provider': 'test', 'model_id': 'm', 'created_at': utc(),
                        'effective_action': 'rule_baseline'})
        engine = ReplayEngine(registry=registry, store=store)
        report = engine.validate('decision_x')
        self.assertFalse(report['input_hash_match'])
        self.assertFalse(report['validated'])


class CriticalEventHardFailTests(unittest.TestCase):
    """issue 8：关键审计事件写失败抛异常。"""

    def test_decision_requested_failure_raises(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = PositionRegistry(Path(tmp.name) / 's.db', 'DRY-RUN')

        class _Boom:
            model = 'm'
            last_metadata = {}
            def record(self, *a, **k):
                raise RuntimeError('disk full')

        class _BoomStore:
            def __init__(self, r):
                self.events = _Boom()
                self.scope = r.namespace
            def get_run(self, *a): return None
            def save_input_snapshot(self, *a, **k): return 'sid'

        from mutifactor.llm.contracts.entry_v2 import build_entry_templates
        engine = DecisionEngine(registry, advisor=_FakeAdvisor(), store=_BoomStore(registry),
                                call_model=lambda c, p: None)
        versions = {'packet_schema': 'entry-v2', 'prompt': 'entry-v2', 'output_schema': 'entry-v2',
                    'feature': 'f', 'rule': 'r', 'permission': 'p', 'model_id': 'm'}
        model = {'provider': 'test', 'model_id': 'm', 'temperature': 0.0, 'timeout_seconds': 30}
        ctx = build_context(role='entry', subject_type='signal', subject_id='sig',
                            account_scope='DRY-RUN', as_of=utc(), versions=versions,
                            model=model, market_session='regular')
        packet = {'context': ctx, 'signal': {}, 'plan': PLAN,
                  'templates': build_entry_templates(plan=PLAN, standard_quantity=100,
                                                     entry_price=100.0, initial_stop=90.0,
                                                     expires_at='2999-01-01T00:00:00+00:00'),
                  'evidence': [], 'quality_gate': {'status': 'pass', 'allowed_uses': ['entry']}}
        with self.assertRaises(RuntimeError):
            engine.decide_entry(packet)


if __name__ == '__main__':
    unittest.main()
