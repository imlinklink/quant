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

    def _save_run(self, store, status, effective_action):
        store.save_run({
            'decision_id': 'd1', 'role': 'selection', 'subject_type': 'research_batch',
            'subject_id': 'b1', 'as_of': utc(), 'status': status,
            'input_snapshot_id': 'i1', 'prompt_version': 'p',
            'output_schema_version': 's', 'feature_version': 'f',
            'rule_version': 'r', 'permission_version': 'v', 'provider': 'test',
            'model_id': 'm', 'created_at': utc(), 'effective_action': effective_action,
        })

    def test_failed_decision_audits_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 'state.db', 'DRY-RUN')
            store = DecisionRunStore(registry)
            self._save_run(store, 'failed', '')
            batch = {'research_batch_id': 'b1', 'decision_id': 'd1',
                     'universe': ['US.A'], 'candidates': [], 'error': 'llm_failed'}
            out = reconcile_selection(registry, batch)
            self.assertEqual(out['selection_status'], 'failed')
            self.assertEqual(out['audit_status'], 'passed')
            self.assertTrue(out['passed'])
            self.assertTrue(out['checks']['no_order_side_effects'])

    def test_failed_decision_without_error_fails_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 'state.db', 'DRY-RUN')
            store = DecisionRunStore(registry)
            self._save_run(store, 'failed', '')
            batch = {'research_batch_id': 'b1', 'decision_id': 'd1',
                     'universe': ['US.A'], 'candidates': []}
            out = reconcile_selection(registry, batch)
            self.assertEqual(out['audit_status'], 'failed')
            self.assertFalse(out['checks']['failure_recorded'])

    def test_side_effect_fails_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 'state.db', 'DRY-RUN')
            store = DecisionRunStore(registry)
            self._save_run(store, 'validated', 'rule_ranking')
            store.events.record('order_intent_created', 'k1', {'decision_id': 'd1', 'code': 'US.A'})
            batch = {'research_batch_id': 'b1', 'decision_id': 'd1',
                     'universe': ['US.A'], 'candidates': [{'code': 'US.A'}],
                     'permission_level': 'shadow', 'effective_action': 'rule_ranking'}
            out = reconcile_selection(registry, batch)
            self.assertEqual(out['audit_status'], 'failed')
            self.assertFalse(out['checks']['no_order_side_effects'])

    def test_batch_for_session_does_not_fall_back_to_history(self):
        from unittest.mock import patch
        from scripts.live_trading.llm_suggestions.store import load_research_batch_for_session
        with patch('scripts.live_trading.llm_suggestions.store.load_research_batches',
                   return_value=[{'as_of': '2026-09-13T20:00:00+00:00', 'decision_id': 'yesterday'}]):
            self.assertIsNone(load_research_batch_for_session('2026-09-14'))

    def test_batch_for_session_matches_ny_date(self):
        from unittest.mock import patch
        from scripts.live_trading.llm_suggestions.store import load_research_batch_for_session
        with patch('scripts.live_trading.llm_suggestions.store.load_research_batches',
                   return_value=[{'as_of': '2026-09-14T20:00:00+00:00', 'decision_id': 'today'}]):
            self.assertEqual(load_research_batch_for_session('2026-09-14')['decision_id'], 'today')


if __name__ == '__main__':
    unittest.main()
