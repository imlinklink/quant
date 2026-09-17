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

    def test_lease_stays_valid_while_it_has_not_expired(self):
        """回归：判据是「租约到期时刻 > now」，不是「> now + 新租期」。

        后者等价于拿本次开始时刻与 now 比，会让**租约远未到期**的正常调用被判为崩溃遗留
        （实测：首次领取后仅过 1 秒就返回 abandoned，而租约是 300 秒）。后果是提前冻结
        ABSTAIN，并与仍在飞行的那个 worker 的有效结果冲突。
        """
        r1 = self._reviewer(at=T0)
        did = r1.prepare_review(opp(), self.pkt)
        self.assertEqual(r1.claim_attempt(did), 'claimed')
        for elapsed in (1, 60, 299):
            later = self._reviewer(at=T0 + timedelta(seconds=elapsed))
            self.assertEqual(later.claim_attempt(did), 'already_started',
                             f'{elapsed}s 后租约（300s）应仍有效')
        expired = self._reviewer(at=T0 + timedelta(seconds=301))
        self.assertEqual(expired.claim_attempt(did), 'abandoned')

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
        # 尝试状态机取值必须与模型结果词汇显式对齐：'OK' 不是终态值，落库时映射成
        # COMPLETED；原始词汇另存 model_status（否则重跑会把成功的尝试当崩溃遗留覆盖掉）
        self.assertEqual(attempt['status'], 'COMPLETED')
        self.assertEqual(attempt['model_status'], 'OK')
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


class AttemptStatusVocabularyTests(unittest.TestCase):
    """两套词汇的对齐是硬要求：错位的代价是「已付费的有效结果被当成从未终结而覆盖」。"""

    def test_model_status_maps_into_the_attempt_state_machine(self):
        from scripts.portfolio_shadow.entry_review import attempt_status_for
        from scripts.portfolio_shadow.schema import ATTEMPT_STATUSES, ATTEMPT_TERMINAL
        for model_status in ('OK', 'FAILED', 'TIMED_OUT', 'HISTORICAL_AS_OF',
                             'MODEL_KNOWLEDGE_CUTOFF', '', None):
            mapped = attempt_status_for(model_status)
            self.assertIn(mapped, ATTEMPT_STATUSES)
            self.assertIn(mapped, ATTEMPT_TERMINAL, f'{model_status} 落到了非终态')

    def test_store_refuses_an_unknown_attempt_status(self):
        tmp = tempfile.mkdtemp()
        store = ShadowStore(Path(tmp) / 'ledger.sqlite3', 'exp1')
        store.save_experiment(manifest())
        with self.assertRaises(ValueError) as ctx:
            store.put_job_run('d1', 1, 'OK')       # 模型侧词汇，不是状态机取值
        self.assertIn('UNKNOWN_ATTEMPT_STATUS', str(ctx.exception))

    def test_store_refuses_to_reopen_a_terminal_attempt(self):
        tmp = tempfile.mkdtemp()
        store = ShadowStore(Path(tmp) / 'ledger.sqlite3', 'exp1')
        store.save_experiment(manifest())
        store.put_job_run('d1', 1, 'COMPLETED', {'action': 'VETO'})
        with self.assertRaises(ValueError) as ctx:
            store.put_job_run('d1', 1, 'CALL_STARTED')
        self.assertIn('ATTEMPT_ALREADY_TERMINAL', str(ctx.exception))


class HistoricalDebugTests(unittest.TestCase):
    """设计 §9：历史提示词调试若需调用，另标 historical_debug，**不进入正式 R/L 表现**。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.store.save_experiment(manifest())
        self.scope = 'SHADOW:exp1:L'
        self.pkt = packet()

    def _reviewer(self, model):
        return EntryReviewer(self.store, scope=self.scope, model_id='historical_debug',
                             model_factory=lambda: model, now=lambda: T0, debug=True)

    def test_debug_call_writes_no_application(self):
        """一旦落成账目，过去的执行日就相当于用今天生成的结果补填前瞻记录。"""
        model = FakeModel(action='VETO', reason_code='MATERIAL_COMPANY_EVENT_RISK',
                          evidence_ids=['ev_1'], cost_micro=42)
        outcome = self._reviewer(model).review(opp(), self.pkt, DEADLINE)
        self.assertFalse(outcome.frozen)
        self.assertEqual(outcome.note, 'historical_debug')
        self.assertIsNone(self.store.application(self.scope, opp().opportunity_id()))

    def test_debug_attempt_is_marked_and_keeps_the_full_reply(self):
        # 与 RealModel 一致：成本不可知时返回 cost_micro=None，而不是把某个数标成不确定
        model = FakeModel(action='PASS', cost_micro=None, cost_uncertain=True)
        outcome = self._reviewer(model).review(opp(), self.pkt, DEADLINE)
        attempt = self.store.job_run(outcome.decision_id)
        self.assertTrue(attempt['historical_debug'])
        self.assertEqual(attempt['status'], 'COMPLETED')
        self.assertTrue(attempt['raw_output'])
        self.assertIsNone(attempt['cost_micro'])       # 成本未知，不是 0
        self.assertTrue(attempt['cost_uncertain'])

    def test_debug_rerun_reuses_the_recorded_attempt(self):
        model = FakeModel(action='PASS', cost_micro=1)
        r = self._reviewer(model)
        first = r.review(opp(), self.pkt, DEADLINE)
        second = r.review(opp(), self.pkt, DEADLINE)
        self.assertEqual(first.decision_id, second.decision_id)
        self.assertEqual(second.note, 'reused_debug_attempt')   # 不再花钱


class RealModelHistoricalGateTests(unittest.TestCase):
    """`allow_historical` 只服务于 debug 路径，默认必须继续拦住历史调用。"""

    def _result(self, allow):
        from scripts.portfolio_shadow.llm_overlay import RealModel
        from unittest.mock import Mock
        advisor = Mock()
        advisor.chat.return_value = None
        advisor.last_metadata = {}
        model = RealModel(advisor, now=lambda: datetime(2026, 9, 17, tzinfo=timezone.utc),
                          allow_historical=allow)
        packet = self.pkt
        return model.call(packet, '2026-01-06T14:20:00+00:00')

    def setUp(self):
        self.pkt = packet()

    def test_default_refuses_a_historical_deadline(self):
        self.assertEqual(self._result(False)['status'], 'HISTORICAL_AS_OF')

    def test_allow_historical_lets_the_debug_path_through(self):
        self.assertNotEqual(self._result(True)['status'], 'HISTORICAL_AS_OF')


class FixtureVetoTests(unittest.TestCase):
    """设计 §11 验收案例「确定性 VETO fixture：R买入、L不买入」。"""

    def test_fixture_veto_cites_packet_evidence(self):
        """VETO 不引用包内证据会被验证器降级成 ABSTAIN —— fixture 也必须走同一条规则。"""
        from scripts.portfolio_shadow.llm_overlay import validate_model_output
        p = packet()
        model = FakeModel(action='VETO', reason_code='MATERIAL_COMPANY_EVENT_RISK',
                          evidence_from_packet=True, cost_micro=0)
        result = model.call(p, DEADLINE)
        self.assertEqual(result['output']['evidence_ids'],
                         [p['events'][0]['evidence_id']])
        ok, errors = validate_model_output(result['output'], p)
        self.assertTrue(ok, errors)

    def test_veto_without_evidence_is_downgraded(self):
        p = packet()
        model = FakeModel(action='VETO', reason_code='MATERIAL_COMPANY_EVENT_RISK',
                          evidence_ids=[], cost_micro=0)
        from scripts.portfolio_shadow.llm_overlay import validate_model_output
        ok, errors = validate_model_output(model.call(p, DEADLINE)['output'], p)
        self.assertFalse(ok)
        self.assertIn('VETO_NO_EVIDENCE', errors)
