"""入场评审编排测试（设计 §7）：原子领取、动作冻结、崩溃恢复、迟到不改判。"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.portfolio_shadow.entry_review import EntryReviewer, decision_id_for
from scripts.portfolio_shadow.evidence import build_entry_packet
from scripts.portfolio_shadow.llm_overlay import FakeModel
from scripts.portfolio_shadow.schema import Manifest, Opportunity, to_micro
from scripts.portfolio_shadow.store import ShadowStore

T0 = datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc)
DEADLINE = '2026-01-06T14:20:00+00:00'
AS_OF = '2026-01-05T21:00:00+00:00'
EVENT = {'evidence_id': 'ev_1', 'security_id': 'SEC-A', 'event_type': 'filing',
         'summary': '公司下调全年指引', 'excerpt': '原文摘录', 'source_url': 'file://x',
         'published_at': '2026-01-04T12:00:00+00:00',
         'observed_at': '2026-01-04T13:00:00+00:00', 'content_hash': 'a' * 64}


def manifest():
    return Manifest(
        experiment_id='exp1', status='DRAFT', parent_strategy_id='B3', parent_version='1',
        parent_code_hash='abc', universe_id='u', universe_hash='uh',
        account_scopes=('SHADOW:exp1:R', 'SHADOW:exp1:L'), initial_cash=to_micro(100000),
        risk_policy={'single_position_risk_bp': 100, 'max_weight_bp': 2000, 'max_positions': 5},
        execution_policy={'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': 3},
        llm_policy={'overlay': 'entry_veto', 'evidence_mode': 'strict',
                    'evidence_window_days': 30, 'evidence_max_events': 50},
        calendar_version='v1',
        evaluation_protocol={'main_metric': 'L_minus_R_return', 'enrollment_window': '3-6 months',
                             'review_date': '2026-12-31', 'cost_allocation': 'L_pays_model_cost'}
    ).freeze('2026-01-02')


def opp(sid='SEC-A'):
    return Opportunity(experiment_id='exp1', security_id=sid, source_candidate_id=sid,
                       parent_version='1', signal_session='2026-01-05',
                       observed_at=AS_OF, planned_execution_session='2026-01-06', rank=1,
                       entry_rule='b3', stop_reference={'atr14_micro': to_micro(2.0)},
                       exit_policy_id='H60', input_hash='h', terminal='READY')


def packet(events=(EVENT,)):
    return build_entry_packet(opp(), {'price': to_micro(100), 'observed_at': AS_OF},
                              list(events), {}, AS_OF,
                              evidence={'evidence_mode': 'strict'})


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.store.save_experiment(manifest())
        self.scope = 'SHADOW:exp1:L'
        self.pkt = packet()

    def _reviewer(self, at=T0, model=None, **kw):
        return EntryReviewer(self.store, scope=self.scope, model_id='m',
                             model_factory=lambda: model or FakeModel(action='PASS',
                                                                      cost_micro=0),
                             now=(lambda: at) if callable(at) else (lambda: at), **kw)

    def test_only_one_worker_claims_the_same_decision(self):
        """设计验收案例：同一 decision 仅一个调用领取成功。"""
        r = self._reviewer()
        did = r.prepare_review(opp(), self.pkt)
        self.assertEqual(r.claim_attempt(did), 'claimed')
        self.assertEqual(r.claim_attempt(did), 'already_started')  # 租约仍有效

    def test_expired_lease_is_abandoned_not_reclaimed(self):
        """租约过期 ≠ 可以重发：那是崩溃遗留，必须判 UNKNOWN 而不是静默重来。"""
        r1 = self._reviewer(at=T0)
        did = r1.prepare_review(opp(), self.pkt)
        self.assertEqual(r1.claim_attempt(did), 'claimed')
        r2 = self._reviewer(at=T0 + timedelta(seconds=600))
        self.assertEqual(r2.claim_attempt(did), 'abandoned')

    def test_prepare_does_not_destroy_the_crash_evidence(self):
        """回归：登记若用 REPLACE 会把 CALL_STARTED 和租约一起抹掉，接管者会误判为首次调用。"""
        r1 = self._reviewer(at=T0)
        did = r1.prepare_review(opp(), self.pkt)
        r1.claim_attempt(did)
        r1.prepare_review(opp(), self.pkt)          # 再次登记
        r2 = self._reviewer(at=T0 + timedelta(seconds=600))
        self.assertEqual(r2.claim_attempt(did), 'abandoned')

    def test_terminal_attempt_is_finalized(self):
        r = self._reviewer()
        did = r.prepare_review(opp(), self.pkt)
        r.claim_attempt(did)
        self.store.put_job_run(did, 1, 'COMPLETED', {'action': 'PASS'})
        self.assertEqual(r.claim_attempt(did), 'finalized')


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.store.save_experiment(manifest())
        self.scope = 'SHADOW:exp1:L'
        self.pkt = packet()

    def _reviewer(self, model):
        return EntryReviewer(self.store, scope=self.scope, model_id='m',
                             model_factory=lambda: model, now=lambda: T0)

    def test_attempt_record_keeps_raw_output_and_validation_errors(self):
        """设计 §7：不能只保存 packet_hash 及最终 action。"""
        model = FakeModel(action='VETO', reason_code='NOT_AN_ALLOWED_REASON',
                          evidence_ids=[], cost_micro=123)
        outcome = self._reviewer(model).review(opp(), self.pkt, DEADLINE)
        attempt = self.store.job_run(outcome.decision_id)
        self.assertEqual(attempt['status'], 'OK')
        self.assertIn('REASON_NOT_ALLOWED', attempt['validation_errors'])
        self.assertTrue(attempt['raw_output'])
        self.assertEqual(attempt['model_id'], 'm')
        self.assertTrue(attempt['request_id'])
        self.assertEqual(attempt['started_at'], T0.isoformat())
        # 非法 VETO 被降级为 ABSTAIN，但原输出完整留痕
        self.assertEqual(outcome.decision.action, 'ABSTAIN')
        self.assertEqual(outcome.decision.raw_action, 'VETO')

    def test_frozen_action_is_reused_not_recomputed(self):
        model = FakeModel(action='VETO', reason_code='MATERIAL_COMPANY_EVENT_RISK',
                          evidence_ids=['ev_1'], cost_micro=50)
        r = self._reviewer(model)
        first = r.review(opp(), self.pkt, DEADLINE)
        second = r.review(opp(), self.pkt, DEADLINE)
        self.assertEqual(first.decision.action, 'VETO')
        self.assertEqual(second.decision.action, 'VETO')   # 未重算
        self.assertEqual(second.note, 'reused_frozen')

    def test_decision_id_binds_packet_model_prompt_and_schema(self):
        base = decision_id_for('exp1', self.scope, 'oid', 'pkt', 'm')
        for changed in (decision_id_for('exp1', self.scope, 'oid', 'pkt2', 'm'),
                        decision_id_for('exp1', self.scope, 'oid', 'pkt', 'm2'),
                        decision_id_for('exp1', self.scope, 'oid', 'pkt', 'm',
                                        prompt_version='entry-veto-v2'),
                        decision_id_for('exp1', self.scope, 'oid', 'pkt', 'm',
                                        schema_version='other')):
            self.assertNotEqual(base, changed)

    def test_frozen_action_is_not_execution(self):
        """设计 §7：动作冻结 ≠ 成交。"""
        model = FakeModel(action='PASS', cost_micro=0)
        self._reviewer(model).review(opp(), self.pkt, DEADLINE)
        app = self.store.application(self.scope, opp().opportunity_id())
        self.assertTrue(app['decision_frozen'])
        self.assertFalse(app['execution_applied'])
        self.store.mark_execution_applied(self.scope, opp().opportunity_id(), '2026-01-06')
        self.assertTrue(self.store.application(self.scope, opp().opportunity_id(),
                                               )['execution_applied'])
