"""PR3 Replay CLI 回归：project/validate/compare，不连执行器。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.live_trading.decision_ledger.decision_run_store import DecisionRunStore
from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.replay_decision import ReplayEngine, _deep_diff


class ReplayEngineContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 'state.db', 'DRY-RUN')
        self.store = DecisionRunStore(self.registry)
        self.engine = ReplayEngine(registry=self.registry, store=self.store)
        self.decision_id = 'decision_test_0001'

    def _seed(self, attempt_parsed):
        sid = self.store.save_input_snapshot('selection', 'b1', {'universe': ['US.A']})
        run = dict(
            decision_id=self.decision_id, role='selection', subject_type='research_batch',
            subject_id='b1', as_of='2026-09-08T00:00:00+00:00', status='complete',
            input_snapshot_id=sid, prompt_version='v4', output_schema_version='v4',
            feature_version='v2', rule_version='v2', permission_version='v2',
            provider='deepseek', model_id='deepseek-chat', created_at='2026-09-08T00:00:00+00:00',
            selected_attempt_id='a1', effective_action='rank_llm')
        self.store.save_run(run)
        self.store.save_attempt(dict(attempt_id='a1', decision_id=self.decision_id,
                                     status='complete', started_at='2026-09-08T00:00:00+00:00',
                                     parsed_response=attempt_parsed))

    def test_project_rebuilds_input_and_attempts(self):
        self._seed({'candidates': [{'code': 'US.A', 'rank': 1}]})
        proj = self.engine.project(self.decision_id)
        self.assertIsNotNone(proj)
        self.assertFalse(proj['network_used'])
        self.assertEqual(proj['run']['role'], 'selection')
        self.assertEqual(proj['input_snapshot']['universe'], ['US.A'])
        self.assertEqual(len(proj['attempts']), 1)

    def test_validate_rechecks_parsed(self):
        self._seed({'candidates': [{'code': 'US.A', 'rank': 1}]})
        rep = self.engine.validate(self.decision_id)
        self.assertTrue(rep['validated'])
        self.assertTrue(rep['input_snapshot_present'])
        self.assertEqual(rep['checks'][0]['has_parsed'], True)

    def test_validate_missing_decision(self):
        rep = self.engine.validate('decision_missing')
        self.assertEqual(rep.get('error'), 'not_found')

    def test_compare_two_attempts(self):
        sid = self.store.save_input_snapshot('selection', 'b1', {'universe': ['US.A']})
        run = dict(decision_id=self.decision_id, role='selection',
                   subject_type='research_batch', subject_id='b1',
                   as_of='2026-09-08T00:00:00+00:00', status='complete',
                   input_snapshot_id=sid, prompt_version='v4',
                   output_schema_version='v4', feature_version='v2', rule_version='v2',
                   permission_version='v2', provider='deepseek', model_id='deepseek-chat',
                   created_at='2026-09-08T00:00:00+00:00', selected_attempt_id='a2',
                   effective_action='rank_llm')
        self.store.save_run(run)
        self.store.save_attempt(dict(attempt_id='a1', decision_id=self.decision_id,
                                     status='complete', started_at='2026-09-08T00:00:00+00:00',
                                     parsed_response={'rank': 1}))
        self.store.save_attempt(dict(attempt_id='a2', decision_id=self.decision_id,
                                     status='complete', started_at='2026-09-08T00:00:01+00:00',
                                     parsed_response={'rank': 2}))
        rep = self.engine.compare(self.decision_id, 'a1')
        self.assertEqual(rep['attempt_a'], 'a1')
        self.assertEqual(rep['attempt_b'], 'a2')
        self.assertTrue(any(d['type'] == 'changed' for d in rep['diff']))

    def test_deep_diff_scalar_and_missing(self):
        d = _deep_diff({'a': 1, 'b': 2}, {'a': 1, 'b': 3, 'c': 4})
        types = {x['type'] for x in d}
        self.assertIn('changed', types)
        self.assertIn('missing_in_historical', types)


if __name__ == '__main__':
    unittest.main()
