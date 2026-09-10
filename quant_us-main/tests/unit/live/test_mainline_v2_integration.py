import tempfile
import time
import unittest
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from scripts.live_trading.approval.proposal_store import ProposalStore
from scripts.live_trading.decision_bridge import (
    entry_legacy_projection, position_legacy_projection, selection_legacy_projection,
)
from scripts.live_trading.decision_engine import DecisionResult
from scripts.live_trading.decision_runtime import DecisionRuntime, engine_v2_config
from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.position_review import PositionReviewScheduler
from scripts.live_trading.decision_ledger.workflow import start_review
from scripts.live_trading.run_daily_selection import run_selection


def result(role, action, output=None, status='validated'):
    return DecisionResult(
        decision_id='d1', role=role, status=status,
        validated_output=output or {}, effective_action='rule_baseline',
        permission_level='shadow', validation_errors=(),
        input_snapshot_id='v2-input', attempt_id='a1', model_action=action)


class RuntimeTests(unittest.TestCase):
    def test_defaults_legacy_and_accepts_all_config_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 's.db', 'DRY-RUN')
            self.assertEqual(DecisionRuntime(registry).mode('selection'), 'legacy')
            cfg = {'llm_decision': {'engine_v2': {'entry': 'shadow'}}}
            self.assertTrue(DecisionRuntime(registry, config=cfg).is_shadow('entry'))
            self.assertEqual(engine_v2_config({'position': 'shadow'})['position'], 'shadow')

    def test_fallback_stops_before_model_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = DecisionRuntime(PositionRegistry(Path(tmp) / 's.db', 'DRY-RUN'))
            self.assertTrue(runtime.may_fallback('build_packet'))
            self.assertFalse(runtime.may_fallback('model_call'))


class ProjectionTests(unittest.TestCase):
    def test_selection_partitions_ranked(self):
        r = result('selection', 'llm_ranking', {'ranked': [
            {'code': 'US.A', 'decision': 'candidate'},
            {'code': 'US.B', 'decision': 'watch'},
            {'code': 'US.C', 'decision': 'exclude'},
        ], 'abstain_reason_codes': []})
        p = selection_legacy_projection(r)
        self.assertEqual([x['code'] for x in p['candidates']], ['US.A', 'US.B'])
        self.assertEqual([x['code'] for x in p['exclusions']], ['US.C'])
        self.assertEqual(p['decision_id'], 'd1')

    def test_selection_projection_accepts_string_invalidators(self):
        r = result('selection', 'llm_ranking', {'ranked': [{
            'code': 'US.A', 'decision': 'watch', 'portfolio_rank': 1,
            'confidence': 'low', 'setup_type': 'none', 'thesis': [],
            'invalidation_conditions': ['跌破程序保护线'],
        }]})
        self.assertEqual(selection_legacy_projection(r)['candidates'][0]['invalidators'],
                         ['跌破程序保护线'])

    def test_entry_projection_preserves_legacy_and_v2_snapshot_ids(self):
        req = {'review_id': 'r1', 'plan_id': 'p1', 'plan_version': 2,
               'input_snapshot_id': 'legacy-input', 'expires_at': time.time() + 300}
        p = entry_legacy_projection(
            result('entry', 'reject', {'reason_codes': ['EVENT_RISK'],
                                       'missing_information': []}), req)
        self.assertEqual(p['recommendation'], 'oppose_execute')
        self.assertEqual(p['input_snapshot_id'], 'legacy-input')
        self.assertEqual(p['decision_input_snapshot_id'], 'v2-input')

    def test_position_projection_is_shadow_only(self):
        p = position_legacy_projection(
            result('position', 'reduce', {'thesis_state': 'WEAKENING'}),
            trigger='new_evidence', legacy_input_snapshot_id='legacy')
        self.assertEqual(p['thesis_state'], 'weakened')
        self.assertTrue(p['shadow_only'])
        self.assertFalse(p['plan_change_applied'])


class ProposalV2CompletionTests(unittest.TestCase):
    def test_late_v2_callback_cannot_mutate_new_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 's.db', 'DRY-RUN')
            store = ProposalStore(log_dir=tmp, registry=registry)
            item = store.create(
                stock_code='US.A', side='buy', price=100, quantity=10,
                entry_mode='dip_buy', trade_plan={'initial_stop': 95},
                reason='rule', signal_id='s1', strategy_version='v1',
                timeframe='15m', quote_observed_at=time.time(), max_price_drift_pct=.03)
            request = store.begin_review(item['id'])
            store.revise_plan(item['id'], {}, 'new evidence')
            self.assertFalse(store.complete_v2_review(request, result('entry', 'reject')))
            self.assertIsNone(store.get(item['id'])['llm'])
            self.assertIsNotNone(store.events.get_snapshot('review', request['review_id']))


class _Advisor:
    enabled = True
    model = 'fake-v2'
    last_metadata = {}

    def __init__(self, raw):
        self.raw = raw
        self.calls = 0

    def chat(self, prompt, system=None):
        self.calls += 1
        return self.raw

    def review_plan(self, *args, **kwargs):
        raise AssertionError('shadow 主链路不得调用 legacy review_plan')


class MainlineEntryPositionTests(unittest.TestCase):
    def test_start_review_uses_decision_engine_once(self):
        raw = {
            'status': 'insufficient_information', 'action': 'execute_now',
            'template_id': '', 'confidence': 'low',
            'reason_codes': ['INSUFFICIENT_EVIDENCE'], 'facts': [],
            'inferences': [], 'counterevidence': [],
            'missing_information': ['缺少独立经营证据'],
        }
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 's.db', 'DRY-RUN')
            store = ProposalStore(log_dir=tmp, registry=registry)
            item = store.create(
                stock_code='US.A', side='buy', price=100, quantity=10,
                entry_mode='dip_buy', trade_plan={'initial_stop': 95},
                reason='rule', signal_id='s1', strategy_version='v1', timeframe='15m',
                quote_observed_at=time.time(), max_price_drift_pct=.03)
            raw['template_id'] = item['plan_id'] + ':standard'
            advisor = _Advisor(raw)
            owner = SimpleNamespace(
                config={'llm_decision': {'enabled': True, 'engine_v2': {'entry': 'shadow'}}},
                approval_store=store, llm_advisor=advisor)
            immediate = SimpleNamespace(put=lambda priority, task: task())
            with patch('scripts.live_trading.decision_ledger.workflow.review_queue',
                       return_value=immediate):
                start_review(owner, item)
            updated = store.get(item['id'])
            self.assertEqual(advisor.calls, 1)
            self.assertEqual(updated['decision_engine_version'], 'v2')
            self.assertEqual(updated['llm']['recommendation'], 'support_execute')

    def test_position_shadow_never_calls_legacy_or_mutates_holding(self):
        raw = {
            'status': 'insufficient_information', 'thesis_state': 'UNKNOWN',
            'action': 'hold', 'action_template_id': None, 'confidence': 'low',
            'reason_codes': ['INSUFFICIENT_EVIDENCE'], 'facts': [],
            'inferences': [], 'counterevidence': [],
            'missing_information': ['缺少新增独立证据'],
        }
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 's.db', 'DRY-RUN')
            registry.open('US.A', 'dip_buy', 10, 100, plan_id='plan', plan_version=1,
                          trade_id='trade', initial_stop=95)
            advisor = _Advisor(raw)
            scheduler = PositionReviewScheduler(
                registry, advisor,
                {'enabled': True, 'check_interval_seconds': 1},
                {'llm_decision': {'engine_v2': {'position': 'shadow'}}})
            plan = {'plan_id': 'plan', 'plan_version': 1,
                    'exit_policy': {'initial_stop': 95}, 'risk': {'initial_stop': 95}}
            with scheduler.events.transaction() as con:
                scheduler.events.snapshot(con, 'plan', 'plan', 1, plan)
            before = registry.all()
            immediate = SimpleNamespace(put=lambda priority, task: task())
            with patch('scripts.live_trading.position_review.review_queue',
                       return_value=immediate):
                self.assertTrue(scheduler.schedule('US.A', 96, event_reason='new_event'))
            self.assertEqual(advisor.calls, 1)
            self.assertEqual(before, registry.all())
            reviewed = [e for e in scheduler.events.events()
                        if e['event_type'] == 'position_reviewed']
            self.assertEqual(reviewed[0]['payload']['decision_engine_version'], 'v2')


class _SelectionAdvisor(_Advisor):
    def chat(self, prompt, system=None):
        self.calls += 1
        packet = json.loads(prompt)['input']
        stock = packet['stocks'][0]
        eid = stock['evidence'][0]['evidence_id']
        return {
            'status': 'complete',
            'market_view': {'risk_posture': 'normal', 'claims': []},
            'ranked': [{
                'code': stock['code'], 'standalone_rank': 1, 'portfolio_rank': 1,
                'decision': 'watch', 'confidence': 'medium', 'horizon': '1_5d',
                'setup_type': 'none', 'reason_codes': [],
                'thesis': [{'text': '行情快照支持继续观察', 'claim_type': 'inference',
                            'evidence_ids': [eid]}],
                'counterevidence': [], 'invalidation_conditions': [],
                'option_view_effect': 'unavailable',
            }],
            'abstain_reason_codes': [],
        }


class SelectionMainlineTests(unittest.TestCase):
    def test_run_selection_shadow_calls_engine_once_and_links_batch(self):
        dates = pd.date_range('2026-05-01', periods=70, freq='B')
        bars = pd.DataFrame({
            'date': dates, 'close': [100 + i * .1 for i in range(70)],
            'high': [101 + i * .1 for i in range(70)],
            'low': [99 + i * .1 for i in range(70)],
        })
        fetcher = SimpleNamespace(fetch_multiple_stocks=lambda codes, start, end: {'US.A': bars})
        advisor = _SelectionAdvisor(None)
        config = {
            'dip_buy': {'watch_list': ['US.A']}, 'trend_breakout': {'watch_list': []},
            'option_view': {'enabled': False},
            'llm_decision': {'engine_v2': {
                'selection': 'shadow', 'account_scope': 'DRY-RUN'}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 's.db', 'DRY-RUN')
            with patch('scripts.live_trading.signal_context.fetch_event_evidence', return_value=[]), \
                 patch('scripts.live_trading.llm_suggestions.store.save_research_batch'):
                batch = run_selection(
                    config, advisor, fetcher,
                    now=datetime(2026, 9, 8, 23, tzinfo=timezone.utc).timestamp(),
                    registry=registry)
            self.assertEqual(advisor.calls, 1)
            self.assertEqual(batch['decision_engine_version'], 'v2')
            self.assertEqual(batch['candidates'][0]['decision'], 'watch')
            self.assertTrue(batch['decision_id'])


if __name__ == '__main__':
    unittest.main()
