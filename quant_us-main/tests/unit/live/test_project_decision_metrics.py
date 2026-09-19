import tempfile
import unittest
from pathlib import Path

from scripts.live_trading.decision_ledger.decision_run_store import DecisionRunStore
from scripts.live_trading.decision_ledger.event_store import utc
from scripts.live_trading.decision_ledger.event_store import EventStore
from scripts.live_trading.decision_ledger.outcome_jobs import OutcomeSettlement
from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.project_decision_metrics import ProjectDecisionMetrics


class ProjectDecisionMetricsTests(unittest.TestCase):
    def test_overview_joins_attempt_with_role(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = PositionRegistry(Path(tmp.name) / 'state.db', 'DRY-RUN')
        store = DecisionRunStore(registry)
        store.save_run({
            'decision_id': 'd1', 'role': 'entry', 'subject_type': 'signal',
            'subject_id': 's1', 'as_of': utc(), 'status': 'validated',
            'input_snapshot_id': 'i1', 'prompt_version': 'p',
            'output_schema_version': 's', 'feature_version': 'f',
            'rule_version': 'r', 'permission_version': 'v', 'provider': 'test',
            'model_id': 'm', 'created_at': utc(), 'effective_action': 'rule_baseline',
        })
        store.save_attempt({
            'attempt_id': 'a1', 'decision_id': 'd1', 'started_at': utc(),
            'completed_at': utc(), 'status': 'completed', 'latency_ms': 25,
        })
        out = ProjectDecisionMetrics(registry).overview('entry')
        self.assertEqual(out['by_role']['entry']['validated'], 1)
        self.assertEqual(out['attempts_total'], 1)
        self.assertEqual(out['latency']['all']['p50'], 25)

    def test_health_marks_small_sample_as_insufficient(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = PositionRegistry(Path(tmp.name) / 'state.db', 'DRY-RUN')
        store = DecisionRunStore(registry)
        store.save_run({
            'decision_id': 'd1', 'role': 'selection', 'subject_type': 'research_batch',
            'subject_id': 'batch1', 'as_of': utc(), 'status': 'validated',
            'input_snapshot_id': 'i1', 'prompt_version': 'p',
            'output_schema_version': 's', 'feature_version': 'f',
            'rule_version': 'r', 'permission_version': 'v', 'provider': 'test',
            'model_id': 'm', 'created_at': utc(), 'effective_action': 'rule_ranking',
        })
        EventStore(registry).record('decision_effective_action', 'k1',
                                    {'effective_action': 'rule_ranking'}, decision_id='d1')
        out = ProjectDecisionMetrics(registry).health()
        self.assertEqual(out['status'], 'insufficient_sample')
        self.assertEqual(out['sample_size'], 1)
        self.assertEqual(out['independence_group_count'], 1)
        self.assertIsNone(out['all_same_action'])
        self.assertFalse(out['all_same_action_alert'])

    def test_position_counterfactual_metrics(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = PositionRegistry(Path(tmp.name) / 'state.db', 'DRY-RUN')
        settlement = OutcomeSettlement(registry)
        settlement.write_outcome('d1', {
            'horizon': '5d', 'return_pct': 0.03, 'benchmark_return_pct': -0.02,
            'excess_return_pct': 0.05, 'mae_pct': -0.01, 'data_quality': 'good',
            'body': {'r_max_drawdown_pct': -0.08, 'saved_loss_pct': 0.05,
                     'missed_upside_pct': 0.0}}, subject_key='t1:position_cf')
        metric = ProjectDecisionMetrics(registry).position_metrics()['counterfactual']['5d']
        self.assertEqual(metric['count'], 1)
        self.assertAlmostEqual(metric['mean_delta_return_pct'], 0.05)
        self.assertAlmostEqual(metric['llm_win_rate'], 1.0)

    def test_entry_counterfactual_metrics(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = PositionRegistry(Path(tmp.name) / 'state.db', 'DRY-RUN')
        settlement = OutcomeSettlement(registry)
        settlement.write_outcome('d1', {
            'horizon': '3d', 'return_pct': 0.0, 'benchmark_return_pct': -0.04,
            'excess_return_pct': 0.04, 'mae_pct': 0.0, 'data_quality': 'good',
            'body': {'saved_loss_pct': 0.04, 'missed_upside_pct': 0.0},
        }, subject_key='s1:entry_cf')
        metric = ProjectDecisionMetrics(registry).entry_metrics()['counterfactual']['3d']
        self.assertEqual(metric['count'], 1)
        self.assertAlmostEqual(metric['mean_saved_loss_pct'], 0.04)

    def test_selection_counterfactual_metrics(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = PositionRegistry(Path(tmp.name) / 'state.db', 'DRY-RUN')
        settlement = OutcomeSettlement(registry)
        settlement.write_outcome('d1', {
            'horizon': '5d', 'return_pct': 0.06, 'benchmark_return_pct': 0.02,
            'excess_return_pct': 0.04, 'data_quality': 'good', 'body': {}},
            subject_key='b1:selection_cf')
        metric = ProjectDecisionMetrics(registry).selection_metrics()['counterfactual']['5d']
        self.assertEqual(metric['count'], 1)
        self.assertAlmostEqual(metric['mean_delta_return_pct'], 0.04)


if __name__ == '__main__':
    unittest.main()
