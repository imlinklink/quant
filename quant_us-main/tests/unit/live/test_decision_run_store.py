"""PR2 决策存储回归：投影 schema 幂等、原因码 v2、DecisionRunStore。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.live_trading.decision_ledger.decision_run_store import (
    DecisionRunStore, build_context,
)
from scripts.live_trading.decision_ledger.event_store import migrate
from scripts.live_trading.decision_ledger.reason_codes import (
    LEGACY_MIGRATION, is_valid_reason, normalize_reason, valid_for_role,
)
from scripts.live_trading.position_registry import PositionRegistry


class ProjectionSchemaMigrationContracts(unittest.TestCase):
    def test_projection_tables_created_on_fresh_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.db'
            con = sqlite3.connect(path)
            migrate(con, path)
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for t in ('llm_decision_runs', 'llm_model_attempts', 'decision_outcomes_v2',
                      'decision_projection_schema'):
                self.assertIn(t, tables)
            con.close()

    def test_projection_schema_not_misread_as_decision_schema_v2(self):
        """旧库（decision_schema 只有 v1）打开时：投影表补齐，但不触发降级保护。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.db'
            con = sqlite3.connect(path)
            # 模拟旧 v1 库
            con.executescript('''
                CREATE TABLE decision_schema(version INTEGER PRIMARY KEY);
                INSERT INTO decision_schema VALUES (1);
                CREATE TABLE books(namespace TEXT PRIMARY KEY, payload TEXT NOT NULL);
            ''')
            con.commit()
            con.close()
            # 打开应不抛 RuntimeError，且补投影表
            con = sqlite3.connect(path)
            migrate(con, path)
            ver = con.execute('SELECT MAX(version) FROM decision_schema').fetchone()[0]
            self.assertEqual(ver, 1)  # 仍是 1，未被投影版本污染
            has = con.execute(
                "SELECT 1 FROM sqlite_master WHERE name='llm_decision_runs'").fetchone()
            self.assertTrue(has)
            con.close()


class ReasonCodeContracts(unittest.TestCase):
    def test_legacy_mapping(self):
        self.assertEqual(normalize_reason('event_risk'), 'EVENT_RISK')
        self.assertEqual(normalize_reason('weak_confirmation'), 'TECHNICAL_NOT_CONFIRMED')
        self.assertEqual(normalize_reason('data_gap'), 'INSUFFICIENT_EVIDENCE')

    def test_new_code_passthrough(self):
        self.assertEqual(normalize_reason('OPTIONS_DIVERGE'), 'OPTIONS_DIVERGE')

    def test_wrapped_and_unknown(self):
        self.assertEqual(normalize_reason('卖出|fixed_stop'), 'INSUFFICIENT_EVIDENCE')  # 非原因码
        self.assertEqual(normalize_reason('totally_unknown'), 'INSUFFICIENT_EVIDENCE')
        self.assertEqual(normalize_reason(None), 'INSUFFICIENT_EVIDENCE')

    def test_roles(self):
        self.assertTrue(valid_for_role('THESIS_INVALIDATED', 'position'))
        self.assertFalse(valid_for_role('THESIS_INVALIDATED', 'selection'))
        self.assertTrue(is_valid_reason('PORTFOLIO_CONCENTRATION'))
        self.assertFalse(is_valid_reason('NOPE'))
        self.assertIn('event_risk', LEGACY_MIGRATION)


class DecisionRunStoreContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 'state.db', 'DRY-RUN')
        self.store = DecisionRunStore(self.registry)

    def _ctx(self):
        return build_context(
            role='selection', subject_type='research_batch', subject_id='b1',
            account_scope='DRY-RUN', as_of='2026-09-08T00:00:00+00:00',
            versions={'packet_schema': 'v4', 'prompt': 'v4', 'output_schema': 'v4',
                      'model_id': 'deepseek-chat'},
            model={'model_id': 'deepseek-chat', 'provider': 'deepseek', 'temperature': 0})

    def test_context_decision_id_stable(self):
        a = self._ctx()
        b = self._ctx()
        self.assertEqual(a['decision_id'], b['decision_id'])
        # 无输入快照时 decision_id 为空（临时）；最终 id 由 finalize_decision_id 绑定输入
        self.assertEqual(a['decision_id'], '')
        from scripts.live_trading.decision_ledger.decision_run_store import finalize_decision_id
        d1 = finalize_decision_id(account_scope='DRY-RUN', role='selection', subject_id='b1',
                                  input_snapshot_id='snap_a', versions=a['versions'],
                                  model_id='deepseek-chat')
        d2 = finalize_decision_id(account_scope='DRY-RUN', role='selection', subject_id='b1',
                                  input_snapshot_id='snap_b', versions=a['versions'],
                                  model_id='deepseek-chat')
        self.assertNotEqual(d1, d2)
        self.assertTrue(d1.startswith('decision_'))

    def test_input_snapshot_saved_and_replayable(self):
        ctx = self._ctx()
        sid = self.store.save_input_snapshot('selection', 'b1',
                                             {'universe': ['US.A'], 'as_of': ctx['as_of']})
        self.assertTrue(sid)
        saved = self.store.get_snapshot('selection_input', sid)
        self.assertIsNotNone(saved)
        self.assertEqual(saved['universe'], ['US.A'])

    def test_save_and_get_run(self):
        ctx = self._ctx()
        self.store.save_input_snapshot('selection', 'b1', {'x': 1})
        sid = self.store.save_input_snapshot('selection', 'b1', {'universe': ['US.A'],
                                                                 'as_of': ctx['as_of']})
        run = dict(ctx,
                   status='complete', input_snapshot_id=sid,
                   prompt_version='v4', output_schema_version='v4',
                   feature_version='v2', rule_version='v2',
                   permission_version='v2', provider='deepseek',
                   model_id='deepseek-chat', created_at=ctx['as_of'],
                   selected_attempt_id='a1', effective_action='rank_llm')
        self.store.save_run(run)
        got = self.store.get_run(ctx['decision_id'])
        self.assertEqual(got['status'], 'complete')
        self.assertEqual(got['input_snapshot_id'], sid)
        self.assertEqual(got['effective_action'], 'rank_llm')

    def test_attempt_saved(self):
        attempt = dict(attempt_id='a1', decision_id='d1', status='complete',
                       started_at='2026-09-08T00:00:00+00:00', latency_ms=10,
                       parsed_response={'candidates': []})
        self.store.save_attempt(attempt)
        with self.store.events.transaction() as con:
            row = con.execute('SELECT parsed_response FROM llm_model_attempts '
                              'WHERE account_scope=? AND attempt_id=?',
                              ('DRY-RUN', 'a1')).fetchone()
        self.assertIn('candidates', row[0])

    def test_list_runs_filter_role(self):
        ctx = self._ctx()
        run = dict(ctx, status='complete',
                   input_snapshot_id='in1', prompt_version='v4',
                   output_schema_version='v4', feature_version='v2',
                   rule_version='v2', permission_version='v2',
                   provider='deepseek', model_id='deepseek-chat',
                   created_at=ctx['as_of'], effective_action='x')
        self.store.save_run(run)
        rows = self.store.list_runs(role='selection')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['role'], 'selection')


if __name__ == '__main__':
    unittest.main()
