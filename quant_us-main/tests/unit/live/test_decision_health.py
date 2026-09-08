"""决策健康摘要（任务 A）回归：LLM 状态区分、不可批准原因码、漏斗口径、账户隔离。"""
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.live_trading.approval.proposal_store import ProposalStore
from scripts.live_trading.decision_ledger.decision_health import (
    build_health, llm_state, record_scan_heartbeat, unapprovable_reason,
)
from scripts.live_trading.decision_ledger.funnel_report import build_funnel
from scripts.live_trading.decision_ledger.workflow import candidate
from scripts.live_trading.position_registry import PositionRegistry


class DecisionHealthContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.registry = PositionRegistry(self.path / 'state.db', 'DRY-RUN')
        self.store = ProposalStore(log_dir=self.path, registry=self.registry)
        p = patch.object(ProposalStore, '_record_ledger')
        p.start()
        self.addCleanup(p.stop)
        self.config = {'risk_budget': {'code_groups': {'US.A': 'semis'},
                                       'group_limits': {'semis': .0075}}}
        self.owner = SimpleNamespace(config=self.config, approval_store=self.store,
                                     dry_run=True, pool=None)

    def proposal(self, side='buy', suffix='', **fields):
        decision = candidate(self.owner, 'US.A', 'dip_buy' + suffix,
                             datetime(2026, 9, 4, 15, tzinfo=timezone.utc), True,
                             {'score': 12, 'initial_stop': 95})
        return self.store.create(stock_code='US.A', side=side, price=100, quantity=20,
                                 entry_mode='dip_buy', trade_plan={'initial_stop': 95, 'target': 110},
                                 reason='已收盘15分钟信号', **decision, **fields)

    def raw(self, request, recommendation='support_execute', status='complete'):
        snapshot = self.store.events.get_snapshot('input', request['input_snapshot_id'])
        e = snapshot['evidence'][0]
        return dict(status=status, recommendation=recommendation,
                    proposed_action=('exit' if request['side'] == 'sell' else 'buy')
                    if recommendation == 'support_execute' else 'hold',
                    thesis_state='unchanged',
                    facts=[{'text': e['summary'], 'evidence_ids': [e['evidence_id']]}],
                    inferences=[], counterevidence=[],
                    missing_information=['尚未取得经营新闻'],
                    plan_change_requested=False,
                    next_review_conditions=['触及程序保护线'])

    def review(self, item, recommendation='support_execute', status='complete'):
        req = self.store.begin_review(item['id'])
        self.store.complete_review(req, self.raw(req, recommendation, status), 'fake-model',
                                   {'cost_usd': .01})
        return self.store.get(item['id'])

    # ---------- LLM 状态区分 ----------

    def test_llm_never_called_when_no_request(self):
        self.proposal()
        state = llm_state(self.store.events.events(), llm_enabled=True)
        self.assertTrue(state['enabled'])
        self.assertEqual(state['status'], 'never_called')
        self.assertEqual(state['request_count'], 0)

    def test_llm_success_and_failure_states(self):
        self.review(self.proposal())
        state = llm_state(self.store.events.events(), llm_enabled=True)
        self.assertEqual(state['status'], 'success')
        self.assertEqual(state['success_count'], 1)

        other = self.proposal(suffix='x')
        req = self.store.begin_review(other['id'])
        self.store.complete_review(req, None)
        state = llm_state(self.store.events.events(), llm_enabled=True)
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['failure_count'], 1)
        self.assertIsNotNone(state['last_failure_reason'])

    def test_llm_insufficient_information_is_not_failure(self):
        self.review(self.proposal(), status='insufficient_information')
        state = llm_state(self.store.events.events(), llm_enabled=True)
        self.assertEqual(state['status'], 'insufficient_information')
        self.assertEqual(state['insufficient_count'], 1)
        self.assertEqual(state['failure_count'], 0)

    # ---------- 不可批准原因码 ----------

    def test_pending_review_reason(self):
        item = self.proposal()
        code, label = unapprovable_reason(item)
        self.assertEqual(code, 'review_pending')

    def test_failed_review_reason_and_blocks_approval(self):
        item = self.proposal()
        req = self.store.begin_review(item['id'])
        self.store.complete_review(req, None)
        item = self.store.get(item['id'])
        code, label = unapprovable_reason(item)
        self.assertEqual(code, 'review_failed')
        self.assertIn('失败', label)
        self.assertFalse(self.store.llm_ready(item))

    def test_insufficient_information_reason(self):
        item = self.review(self.proposal(), status='insufficient_information')
        code, label = unapprovable_reason(item)
        self.assertEqual(code, 'insufficient_information')
        self.assertIn('资料不足', label)

    def test_revision_requires_new_review(self):
        item = self.review(self.proposal())
        self.assertIsNone(unapprovable_reason(item)[0])
        revised = self.store.revise_plan(item['id'], {'trade_plan': {'initial_stop': 94, 'target': 110}},
                                         '调整结构边界')
        # 修订清空旧评估 → 需重新评估；旧评估不得恢复批准资格
        code, _ = unapprovable_reason(revised)
        self.assertEqual(code, 'review_pending')
        self.assertIsNone(revised.get('llm'))

    def test_expired_proposal_reason(self):
        item = self.proposal(expires_at=time.time() - 1)
        code, _ = unapprovable_reason(item)
        self.assertEqual(code, 'proposal_expired')

    # ---------- 漏斗口径 ----------

    def test_rule_rejected_is_not_unhandled(self):
        candidate(self.owner, 'US.A', 'dip_buy',
                  datetime(2026, 9, 4, 16, tzinfo=timezone.utc), False, {'score': 3})
        report = build_funnel(self.store.events.events())
        self.assertEqual(report['candidates'], 1)
        self.assertEqual(report['stages']['rule_rejected'], 1)
        self.assertEqual(report['stages']['unhandled'], 0)
        self.assertEqual(report['rows'][0]['human'], 'rule_rejected')

        health = build_health(self.store.events.events(), llm_enabled=True,
                              proposals=self.store.get_all(), scope=self.store.events.scope)
        self.assertEqual(health['scan']['rule_rejected'], 1)
        self.assertEqual(health['scan']['rule_passed'], 0)
        self.assertEqual(health['proposals']['total'], 0)
        self.assertEqual(health['llm']['request_count'], 0)

    # ---------- 扫描心跳与账户隔离 ----------

    def test_heartbeat_recorded_and_reported(self):
        self.assertIsNone(build_health(self.store.events.events(), True,
                                       self.store.get_all(), self.store.events.scope)['scan']['last_heartbeat_at'])
        record_scan_heartbeat(self.store.events)
        health = build_health(self.store.events.events(), True,
                              self.store.get_all(), self.store.events.scope)
        self.assertIsNotNone(health['scan']['last_heartbeat_at'])

    def test_account_scopes_do_not_mix(self):
        self.proposal()
        other_registry = PositionRegistry(self.path / 'other.db', 'OTHER-ACCOUNT')
        other_store = ProposalStore(log_dir=self.path / 'other', registry=other_registry)
        other_store.events.record('rule_candidate', 'test-key',
                                  {'stock_code': 'US.B', 'passed': True})

        scopes = {e['account_scope'] for e in self.store.events.events()}
        self.assertEqual(scopes, {'DRY-RUN'})
        other_scopes = {e['account_scope'] for e in other_store.events.events()}
        self.assertEqual(other_scopes, {'OTHER-ACCOUNT'})


if __name__ == '__main__':
    unittest.main()
