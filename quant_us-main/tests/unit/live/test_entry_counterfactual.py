import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.live_trading.decision_ledger.entry_counterfactual import (
    EntryCounterfactualLedger, freeze_payload, simulate,
)
from scripts.live_trading.position_registry import PositionRegistry


class EntryCounterfactualTests(unittest.TestCase):
    def packet(self):
        return {
            'context': {'as_of': '2026-09-19T12:00:00+00:00'},
            'signal': {'signal_id': 's1'},
            'plan': {'stock_code': 'US.A'},
            'templates': [
                {'template_id': 'p:standard', 'kind': 'standard', 'quantity': 10},
                {'template_id': 'p:reject', 'kind': 'reject', 'quantity': 0},
            ],
        }

    def result(self):
        return SimpleNamespace(
            decision_id='d1', model_action='reject', effective_action='rule_baseline',
            permission_level='shadow',
            validated_output={
                'template_id': 'p:reject',
                'facts': [{'evidence_ids': ['e1']}],
                'inferences': [], 'counterevidence': []})

    def test_freezes_rule_and_llm_paths(self):
        payload = freeze_payload(self.packet(), self.result(), proposal_id='p1', review_id='r1')
        self.assertEqual(payload['rule_path']['template']['kind'], 'standard')
        self.assertEqual(payload['llm_path']['action'], 'reject')
        self.assertEqual(payload['llm_path']['template']['quantity'], 0)
        self.assertEqual(payload['evidence_ids'], ['e1'])

    def test_event_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 'state.db', 'DRY-RUN')
            ledger = EntryCounterfactualLedger(registry)
            first = ledger.freeze(self.packet(), self.result(), proposal_id='p1', review_id='r1')
            second = ledger.freeze(self.packet(), self.result(), proposal_id='p1', review_id='r1')
            self.assertEqual(first, second)
            events = [e for e in ledger.events.events()
                      if e['event_type'] == 'entry_counterfactual_frozen']
            self.assertEqual(len(events), 1)

    def test_reject_avoids_loss_and_misses_upside(self):
        payload = freeze_payload(self.packet(), self.result(), proposal_id='p1', review_id='r1')
        down = simulate(payload, [{'open': 100, 'low': 95, 'close': 96}], horizons=(1,))
        self.assertAlmostEqual(down[0]['r_return_pct'], -0.04)
        self.assertEqual(down[0]['l_return_pct'], 0.0)
        self.assertAlmostEqual(down[0]['saved_loss_pct'], 0.04)
        up = simulate(payload, [{'open': 100, 'low': 99, 'close': 110}], horizons=(1,))
        self.assertAlmostEqual(up[0]['missed_upside_pct'], 0.10)


if __name__ == '__main__':
    unittest.main()
