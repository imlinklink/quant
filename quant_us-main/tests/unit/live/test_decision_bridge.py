"""DecisionBridge 影子接入测试（T3）。"""
import tempfile
import time
import unittest
from pathlib import Path

from mutifactor.llm.contracts.selection_v4 import validate_selection_v4
from scripts.live_trading.decision_bridge import (
    ShadowBridge, build_entry_packet, build_position_packet,
    build_selection_packet, evidence_item, stocks_from_packets,
)
from scripts.live_trading.decision_ledger.event_store import utc
from scripts.live_trading.decision_ledger.evidence_packet import build_evidence_packet
from scripts.live_trading.position_registry import PositionRegistry


class _FakeAdvisor:
    model = 'test-model'
    last_metadata = {}

    def __init__(self, raw):
        self._raw = raw

    def chat(self, prompt, system=None):
        return self._raw


def _valid_selection_raw(eid):
    return {
        'status': 'complete',
        'market_view': {'risk_posture': 'normal', 'claims': []},
        'ranked': [{
            'code': 'US.A', 'standalone_rank': 1, 'portfolio_rank': 1,
            'decision': 'candidate', 'confidence': 'medium', 'horizon': '1_5d',
            'setup_type': 'none', 'reason_codes': [],
            'thesis': [{'text': '财报超预期，营收同比增长 20%', 'claim_type': 'inference',
                        'evidence_ids': [eid]}],
            'counterevidence': [], 'invalidation_conditions': [],
            'option_view_effect': 'unavailable',
        }],
        'abstain_reason_codes': [],
    }


class BridgePacketTests(unittest.TestCase):
    def test_evidence_item_normalizes_legacy(self):
        e = evidence_item({'summary': 's', 'source': 'internal:x', 'observed_at': time.time(),
                           'kind': 'news', 'subject_code': 'MARKET'}, 'US.A')
        self.assertEqual(e['subject_code'], 'MARKET')
        self.assertEqual(e['kind'], 'news')
        self.assertIn('evidence_id', e)

    def test_evidence_item_does_not_invent_missing_subject(self):
        e = evidence_item({'summary': 's', 'source': 'internal:x',
                           'observed_at': time.time(), 'kind': 'news'}, 'US.A')
        self.assertIsNone(e['subject_code'])

    def test_build_selection_packet(self):
        packet = build_evidence_packet(
            'US.A', quote={'price': 100.0, 'observed_at': time.time() - 60},
            events=[{'summary': '财报超预期', 'source': 'internal:test',
                     'published_at': time.time() - 3600, 'observed_at': time.time() - 3600,
                     'kind': 'fundamental', 'subject_code': 'US.A'}])
        stocks = stocks_from_packets([packet])
        p = build_selection_packet(batch_id='b1', account_scope='DRY-RUN',
                                   discovery_codes=['US.A'], stocks=stocks, as_of=utc())
        self.assertEqual(p['universe']['discovery_codes'], ['US.A'])
        self.assertEqual(p['stocks'][0]['code'], 'US.A')
        self.assertTrue(p['stocks'][0]['evidence'])

    def test_selection_subject_allowlist_comes_from_packet_identity(self):
        for subject, valid in (('MARKET', True), ('US.SOXX', True),
                               ('semis', True), ('US.XLE', False), (None, False)):
            ev = evidence_item({'evidence_id': 'e1', 'summary': '财报超预期，营收同比增长 20%',
                                'source': 'internal:test', 'observed_at': time.time() - 60,
                                'kind': 'news', 'subject_code': subject})
            packet = {'code': 'US.A', 'identity': {'sector': 'US.SOXX',
                                                   'risk_group': 'semis'},
                      'events': [ev]}
            errs = validate_selection_v4(_valid_selection_raw('e1'), ['US.A'], [packet],
                                         as_of=utc())
            self.assertEqual(not errs, valid, (subject, errs))

    def test_build_entry_and_position_packet(self):
        plan = {'plan_id': 'p1', 'stock_code': 'US.AAPL',
                'risk': {'initial_stop': 90.0}, 'entry_constraints': {'price': 100.0}}
        ep = build_entry_packet(signal={'signal_id': 's1'}, plan=plan, evidence=[],
                                account_scope='DRY-RUN', subject_id='s1', as_of=utc(),
                                standard_quantity=100, entry_price=100.0, initial_stop=90.0)
        self.assertEqual(len(ep['templates']), 4)
        pp = build_position_packet(trade={'trade_id': 't1', 'code': 'US.AAPL',
                                          'remaining_qty': 100.0, 'direction': 'long'},
                                   protection={'active_stop': 90.0}, new_evidence=[],
                                   account_scope='DRY-RUN', subject_id='t1', as_of=utc())
        self.assertGreaterEqual(len(pp['allowed_actions']), 4)


class ShadowBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 's.db', 'DRY-RUN')

    def test_run_selection_shadow(self):
        packet = build_evidence_packet(
            'US.A', quote={'price': 100.0, 'observed_at': time.time() - 60},
            events=[{'summary': '财报超预期，营收同比增长 20%', 'source': 'internal:test',
                     'published_at': time.time() - 3600, 'observed_at': time.time() - 3600,
                     'kind': 'fundamental', 'subject_code': 'US.A'}],
            now=time.time() - 60)
        eid = packet['events'][0]['evidence_id']
        bridge = ShadowBridge(self.registry, _FakeAdvisor(_valid_selection_raw(eid)))
        result = bridge.run_selection(batch_id='b1', account_scope='DRY-RUN',
                                      discovery_codes=['US.A'], packets=[packet], as_of=utc())
        self.assertEqual(result.status, 'validated')
        # shadow 权限：effective action = 规则基线
        self.assertEqual(result.effective_action, 'rule_ranking')
        self.assertEqual(result.model_action, 'llm_ranking')
        # 决策已落库，可被回放
        run = bridge.engine.store.get_run(result.decision_id)
        self.assertIsNotNone(run)
        self.assertEqual(run['role'], 'selection')

    def test_run_entry_shadow(self):
        raw = {'status': 'insufficient_information', 'action': 'execute_now',
               'template_id': 'p1:standard', 'confidence': 'high',
               'reason_codes': ['OPTIONS_CONFIRM'],
               'facts': [], 'inferences': [], 'counterevidence': [],
               'missing_information': ['资料不足']}
        plan = {'plan_id': 'p1', 'stock_code': 'US.AAPL',
                'risk': {'initial_stop': 90.0}, 'entry_constraints': {'price': 100.0}}
        bridge = ShadowBridge(self.registry, _FakeAdvisor(raw))
        result = bridge.run_entry(signal={'signal_id': 's1'}, plan=plan, evidence=[],
                                  account_scope='DRY-RUN', subject_id='s1', as_of=utc(),
                                  standard_quantity=100, entry_price=100.0, initial_stop=90.0)
        self.assertEqual(result.status, 'validated')
        self.assertEqual(result.effective_action, 'rule_baseline')


if __name__ == '__main__':
    unittest.main()
