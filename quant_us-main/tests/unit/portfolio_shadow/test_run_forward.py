"""前向运行关键路径：#1 成交时序（计划日入队、到期认领）、#3 证据质量门、#4 未知成本。

这些是 review 指出的「75 项测试通过但没覆盖的关键路径」——CLI 之前完全没有测试。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import pandas as pd

from scripts.evidence.evidence_store import normalize_evidence
from scripts.portfolio_shadow.candidate_adapter import intents_for_session
from scripts.portfolio_shadow.cli import drop_from_schedule, run_entry_overlay
from scripts.portfolio_shadow.evidence import entry_decision_cutoff, entry_response_deadline
from scripts.portfolio_shadow.llm_overlay import FakeModel, SCHEMA_VERSION
from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.schema import Manifest, Opportunity, to_micro
from scripts.portfolio_shadow.store import ShadowStore

SIGNAL = pd.Timestamp('2026-01-05')
EXEC = pd.Timestamp('2026-01-06')


def manifest():
    return Manifest(
        experiment_id='exp1', status='DRAFT', parent_strategy_id='B3', parent_version='1',
        parent_code_hash='abc', universe_id='u', universe_hash='uh',
        account_scopes=('SHADOW:exp1:R', 'SHADOW:exp1:L'), initial_cash=to_micro(100000),
        risk_policy={'single_position_risk_bp': 100, 'max_weight_bp': 2000, 'max_positions': 5},
        execution_policy={'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': 3},
        llm_policy={'overlay': 'entry_veto'}, calendar_version='v1',
        evaluation_protocol={'main_metric': 'L_minus_R_return', 'enrollment_window': '3-6 months',
                             'review_date': '2026-12-31', 'cost_allocation': 'L_pays_model_cost'}
    ).freeze('2026-01-02')


def opp(sid='SEC-A', planned='2026-01-06'):
    return Opportunity(experiment_id='exp1', security_id=sid, source_candidate_id=sid,
                       parent_version='1', signal_session='2026-01-05',
                       observed_at='2026-01-05T21:00:00+00:00',
                       planned_execution_session=planned, rank=1, entry_rule='b3',
                       stop_reference={'atr14_micro': to_micro(2.0)}, exit_policy_id='H60',
                       input_hash='h', terminal='READY')


def evidence_records(sid='SEC-A'):
    """符合 evidence_store schema 的一条已核实证据（含正文列）。"""
    row = {'security_id': sid, 'symbol_as_published': sid, 'kind': 'filing',
           'source_id': 'src1', 'source_record_id': 'rec1',
           'source_url_or_archive_path': 'file://x',
           'event_at': '2026-01-04T12:00:00Z', 'published_at': '2026-01-04T12:00:00Z',
           'observed_at': '2026-01-04T13:00:00Z', 'ingested_at': '2026-01-04T14:00:00Z',
           'version_id': 'v1', 'supersedes_id': None, 'content_hash': 'a' * 64,
           'summary_hash': None, 'quality_status': 'verified',
           'availability_proof': 'archive', 'license_tag': 'research-use-only',
           'summary': '公司下调全年指引'}
    frame = normalize_evidence(pd.DataFrame([row]))
    frame['summary'] = ['公司下调全年指引']  # normalize 剥掉正文，这里按行回挂
    return frame


class OverlayForwardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.store.save_experiment(manifest())
        self.scope = 'SHADOW:exp1:L'
        self.quotes = {'SEC-A': {'price': to_micro(100), 'observed_at': '2026-01-04T21:00:00Z'}}

    def _run(self, model, records=None, notes=None):
        return run_entry_overlay(self.store, self.scope, notes or [opp()], session=SIGNAL,
                                 exec_session=EXEC, quotes=self.quotes, records=records,
                                 real_model=model)

    def test_no_evidence_abstains_free_and_keeps_intent(self):
        model = Mock()
        kept, cost, uncertain = self._run(model)
        self.assertEqual([o.opportunity_id() for o in kept], [opp().opportunity_id()])
        self.assertEqual((cost, uncertain), (0, []))
        model.call.assert_not_called()          # 无证据不发起调用 ⇒ 无费用
        app = self.store.application(self.scope, opp().opportunity_id())
        self.assertEqual(app['action'], 'ABSTAIN')
        self.assertEqual(app['reason_code'], 'INSUFFICIENT_EVIDENCE')

    def test_missing_quote_blocks_and_drops_intent(self):
        model = Mock()
        kept, cost, _ = run_entry_overlay(self.store, self.scope, [opp()], session=SIGNAL,
                                          exec_session=EXEC, quotes={}, records=None,
                                          real_model=model)
        self.assertEqual(kept, [])              # BLOCK ⇒ 剔除，不能照常成交
        self.assertEqual(cost, 0)
        model.call.assert_not_called()
        app = self.store.application(self.scope, opp().opportunity_id())
        self.assertEqual(app['action'], 'BLOCK')
        self.assertEqual(app['reason_code'], 'DATA_BLOCKED_QUOTE')

    def test_decision_carries_signal_day_cutoff_not_exec_day(self):
        self._run(Mock())
        app = self.store.application(self.scope, opp().opportunity_id())
        self.assertEqual(app['as_of'], entry_decision_cutoff(SIGNAL))
        self.assertNotEqual(app['as_of'], entry_decision_cutoff(EXEC))
        self.assertLess(app['as_of'], entry_response_deadline(EXEC))

    def test_veto_with_evidence_drops_from_l(self):
        packet_probe = {}
        model = FakeModel(action='VETO', reason_code='MATERIAL_COMPANY_EVENT_RISK',
                          evidence_ids=[], cost_micro=250)

        def _passthrough(packet, deadline):
            packet_probe.update(packet)
            return model.call(packet, deadline)

        stub = Mock()
        stub.call.side_effect = _passthrough
        records = evidence_records()
        # FakeModel 的 evidence_ids 需引用包内 id：先取一次包
        from scripts.portfolio_shadow.evidence import build_entry_packet
        from scripts.portfolio_shadow.evidence import events_from_records
        events = events_from_records(records, 'SEC-A', entry_decision_cutoff(SIGNAL))[0]
        pkt = build_entry_packet(opp(), self.quotes['SEC-A'], events, {},
                                 entry_decision_cutoff(SIGNAL))
        model.evidence_ids = [pkt['events'][0]['evidence_id']]
        kept, cost, _ = self._run(stub, records=records)
        self.assertEqual(kept, [])              # VETO ⇒ L 不建仓
        self.assertEqual(cost, 250)
        app = self.store.application(self.scope, opp().opportunity_id())
        self.assertEqual(app['action'], 'VETO')
        self.assertEqual(app['reason_code'], 'MATERIAL_COMPANY_EVENT_RISK')
        # 包内确实带上了可读正文，VETO 才有依据
        self.assertEqual(pkt['events'][0]['summary'], '公司下调全年指引')

    def test_rerun_reuses_recorded_decision_without_second_call(self):
        model = FakeModel(action='PASS', cost_micro=500)
        stub = Mock()
        stub.call.side_effect = lambda packet, deadline: model.call(packet, deadline)
        self._run(stub, records=evidence_records())
        first = stub.call.call_count
        self.assertEqual(first, 1)
        # 重跑同一 session：必须复用已落库决定，不得重复付费
        self._run(stub, records=evidence_records())
        self.assertEqual(stub.call.call_count, first)

    def test_unknown_cost_is_surfaced_not_swallowed(self):
        model = FakeModel(action='PASS', cost_micro=None, cost_uncertain=True)
        stub = Mock()
        stub.call.side_effect = lambda packet, deadline: model.call(packet, deadline)
        kept, cost, uncertain = self._run(stub, records=evidence_records())
        self.assertEqual(cost, 0)               # 尚未计入，不是免费
        self.assertEqual(len(uncertain), 1)
        app = self.store.application(self.scope, opp().opportunity_id())
        self.assertTrue(app['cost_uncertain'])
        self.assertEqual(app['attempt_id'], uncertain[0])


class ClaimTimingTests(unittest.TestCase):
    """#1 核心：机会只能在计划执行日被认领，引擎同时校验。"""

    def setUp(self):
        self.m = manifest()

    def test_intent_claimed_only_on_planned_session(self):
        o = opp(planned='2026-01-06')
        self.assertEqual(intents_for_session([o], '2026-01-05'), [])
        self.assertEqual(len(intents_for_session([o], '2026-01-06')), 1)

    def test_engine_rejects_early_execution(self):
        o = opp(planned='2026-01-06')
        state = new_account_state('SHADOW:exp1:L', to_micro(100000))
        bars = {'SEC-A': {'open': to_micro(100), 'high': to_micro(101),
                          'low': to_micro(99), 'close': to_micro(100.5)}}
        with self.assertRaises(ValueError):
            step(state, session='2026-01-05', bars=bars, corporate_actions=[],
                 intents=[o], manifest=self.m)
        # 计划日执行才成交
        res = step(state, session='2026-01-06', bars=bars, corporate_actions=[],
                   intents=[o], manifest=self.m)
        self.assertIn('SEC-A', res.state.positions)

    def test_veto_only_clears_that_execution_days_queue(self):
        """回归：过滤被否决机会时不得波及其它执行日的待执行队列。"""
        a = opp('SEC-A', planned='2026-01-06')
        b = opp('SEC-B', planned='2026-01-07')
        schedule = {'2026-01-06': [a], '2026-01-07': [b]}
        drop_from_schedule(schedule, '2026-01-06', {a.opportunity_id()})
        self.assertEqual(schedule['2026-01-06'], [])
        self.assertEqual([o.security_id for o in schedule['2026-01-07']], ['SEC-B'])


class TerminalTransitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.store.save_experiment(manifest())
        self.o = opp()
        self.store.put_opportunity(self.o)

    def test_unclaimed_opportunity_is_drained_as_missed_execution(self):
        self.store.set_opportunity_terminal(self.o.opportunity_id(), 'MISSED_EXECUTION',
                                            '2026-01-06', note='UNCLAIMED_AT_RUN_END')
        self.assertEqual(self.store.opportunity_terminals()[self.o.opportunity_id()],
                         'MISSED_EXECUTION')

    def test_repeated_terminal_write_is_idempotent(self):
        oid = self.o.opportunity_id()
        self.store.set_opportunity_terminal(oid, 'EXECUTED', '2026-01-06')
        self.store.set_opportunity_terminal(oid, 'EXECUTED', '2026-01-06')
        self.assertEqual(self.store.opportunity_terminals()[oid], 'EXECUTED')

    def test_rerun_ready_put_does_not_clobber_terminal(self):
        oid = self.o.opportunity_id()
        self.store.set_opportunity_terminal(oid, 'MISSED_EXECUTION', '2026-01-06')
        self.store.put_opportunity(self.o)      # 重跑重新生成的 READY
        self.assertEqual(self.store.opportunity_terminals().get(oid, 'MISSED_EXECUTION'),
                         'MISSED_EXECUTION')


if __name__ == '__main__':
    unittest.main()
