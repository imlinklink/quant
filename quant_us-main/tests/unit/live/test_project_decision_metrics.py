import tempfile
import unittest
from pathlib import Path

from scripts.live_trading.decision_ledger.decision_run_store import DecisionRunStore
from scripts.live_trading.decision_ledger.event_store import utc
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


if __name__ == '__main__':
    unittest.main()
