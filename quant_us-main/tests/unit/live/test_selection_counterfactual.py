import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.live_trading.decision_ledger.selection_counterfactual import (
    SelectionCounterfactualLedger, freeze_payload,
)
from scripts.live_trading.position_registry import PositionRegistry


class SelectionCounterfactualTests(unittest.TestCase):
    def packet(self):
        return {'context': {'as_of': '2026-09-19T12:00:00+00:00'},
                'universe': {'discovery_codes': ['US.A', 'US.B', 'US.C'],
                             'execution_eligible_codes': ['US.A', 'US.B', 'US.C']}}

    def result(self):
        return SimpleNamespace(decision_id='d1', validated_output={'ranked': [
            {'code': 'US.C', 'decision': 'candidate', 'portfolio_rank': 1,
             'thesis': [{'evidence_ids': ['e3']}], 'counterevidence': []},
            {'code': 'US.A', 'decision': 'candidate', 'portfolio_rank': 2,
             'thesis': [{'evidence_ids': ['e1']}], 'counterevidence': []},
            {'code': 'US.B', 'decision': 'watch', 'portfolio_rank': 3,
             'thesis': [], 'counterevidence': []},
        ]})

    def test_selects_model_ranked_capacity_and_records_replacements(self):
        payload = freeze_payload(self.packet(), self.result(), batch_id='b1', max_positions=2)
        self.assertEqual(payload['rule_selected'], ['US.A', 'US.B'])
        self.assertEqual(payload['llm_selected'], ['US.C', 'US.A'])
        self.assertEqual(payload['replaced_out'], ['US.B'])
        self.assertEqual(payload['replaced_in'], ['US.C'])

    def test_event_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 'state.db', 'DRY-RUN')
            ledger = SelectionCounterfactualLedger(registry)
            ledger.freeze(self.packet(), self.result(), batch_id='b1', max_positions=2)
            ledger.freeze(self.packet(), self.result(), batch_id='b1', max_positions=2)
            self.assertEqual(sum(e['event_type'] == 'selection_counterfactual_frozen'
                                 for e in ledger.events.events()), 1)


if __name__ == '__main__':
    unittest.main()
