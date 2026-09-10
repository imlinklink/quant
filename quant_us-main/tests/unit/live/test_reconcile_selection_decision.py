import tempfile
import unittest
from pathlib import Path

from scripts.live_trading.decision_ledger.decision_run_store import DecisionRunStore
from scripts.live_trading.decision_ledger.event_store import utc
from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.reconcile_selection_decision import reconcile_selection


class ReconcileSelectionTests(unittest.TestCase):
    def test_incomplete_ledger_fails_with_specific_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 'state.db', 'DRY-RUN')
            store = DecisionRunStore(registry)
            store.save_run({
                'decision_id': 'd1', 'role': 'selection', 'subject_type': 'research_batch',
                'subject_id': 'b1', 'as_of': utc(), 'status': 'validated',
                'input_snapshot_id': 'i1', 'prompt_version': 'p',
                'output_schema_version': 's', 'feature_version': 'f',
                'rule_version': 'r', 'permission_version': 'v', 'provider': 'test',
                'model_id': 'm', 'created_at': utc(), 'effective_action': 'rule_ranking',
            })
            batch = {'research_batch_id': 'b1', 'decision_id': 'd1',
                     'universe': ['US.A'], 'candidates': [{'code': 'US.A'}],
                     'permission_level': 'shadow', 'effective_action': 'rule_ranking'}
            out = reconcile_selection(registry, batch)
            self.assertFalse(out['passed'])
            self.assertTrue(out['checks']['batch_linked'])
            self.assertTrue(out['checks']['universe_covered'])
            self.assertFalse(out['checks']['snapshots_complete'])


if __name__ == '__main__':
    unittest.main()
