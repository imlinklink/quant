"""store + replay 集成：save_state / latest_state / events 重放一致 + scope 隔离。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.replay import replay
from scripts.portfolio_shadow.schema import Manifest, Opportunity, to_micro
from scripts.portfolio_shadow.store import (SHADOW_SCHEMA_VERSION, LedgerSchemaMismatch,
                                           ShadowStore, state_from_dict)


def manifest():
    return Manifest(
        experiment_id='exp1', status='DRAFT', parent_strategy_id='B3', parent_version='1',
        parent_code_hash='abc', universe_id='u', universe_hash='uh',
        account_scopes=('SHADOW:exp1:R', 'SHADOW:exp1:L'), initial_cash=to_micro(100000),
        risk_policy={'single_position_risk_bp': 100, 'max_weight_bp': 2000, 'max_positions': 5},
        execution_policy={'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': 60},
        llm_policy={'overlay': 'fixed_pass'}, calendar_version='v1',
        evaluation_protocol={'main_metric': 'L_minus_R_return', 'enrollment_window': '3-6 months',
                             'review_date': '2026-12-31', 'cost_allocation': 'L_pays_model_cost'}
    ).freeze('2026-01-02')


def opp(sid, sess):
    return Opportunity(experiment_id='exp1', security_id=sid, source_candidate_id=sid,
                       parent_version='1', signal_session='2026-01-01',
                       observed_at='2026-01-01T00:00Z', planned_execution_session=sess, rank=1,
                       entry_rule='b3', stop_reference={'atr14_micro': to_micro(2.0)},
                       exit_policy_id='H60', input_hash='h')


class StoreReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / 'ledger.sqlite3'

    def _run_and_save(self, store, scope, session, bars=None, **kw):
        m = manifest()
        if bars is None:
            bars = {'SEC-A': {'open': to_micro(100), 'high': to_micro(101),
                              'low': to_micro(99), 'close': to_micro(100.5)}}
        row = store.latest_state(scope)
        state = state_from_dict(row[1]) if row else new_account_state(scope, m.initial_cash)
        res = step(state, session=session, bars=bars,
                   corporate_actions=[], intents=kw.pop('intents', [opp('SEC-A', session)]),
                   manifest=m, **kw)
        store.save_state(scope, res.state, res.nav, res.events)
        return res

    def test_save_replay_roundtrip(self):
        store = ShadowStore(self.path, 'exp1')
        store.save_experiment(manifest())
        r = self._run_and_save(store, 'SHADOW:exp1:R', '2026-01-05')
        self._run_and_save(store, 'SHADOW:exp1:L', '2026-01-05')

        # 重放 R 的事件，哈希与落库一致
        state = replay('SHADOW:exp1:R', manifest().initial_cash, store.events('SHADOW:exp1:R'))
        _, saved = store.latest_state('SHADOW:exp1:R')
        self.assertEqual(state.state_hash(), state_from_dict(saved).state_hash())
        # 直接比对最近一次 live state
        self.assertEqual(state.state_hash(), r.state.state_hash())

    def test_scope_isolation_in_store(self):
        store = ShadowStore(self.path, 'exp1')
        store.save_experiment(manifest())
        self._run_and_save(store, 'SHADOW:exp1:R', '2026-01-05')
        # L 未运行，latest_state 应为 None
        self.assertIsNone(store.latest_state('SHADOW:exp1:L'))
        # R 有状态
        seq, saved = store.latest_state('SHADOW:exp1:R')
        self.assertEqual(seq, 1)
        self.assertEqual(saved['scope'], 'SHADOW:exp1:R')

    def test_opportunity_roundtrip(self):
        store = ShadowStore(self.path, 'exp1')
        store.save_experiment(manifest())
        o = opp('SEC-A', '2026-01-05')
        store.put_opportunity(o)
        opps = store.opportunities()
        self.assertEqual(len(opps), 1)
        self.assertEqual(opps[0]['security_id'], 'SEC-A')

    def test_missed_events_reach_the_ledger(self):
        """missed 事件此前被 save_state 白名单丢弃，只在内存断言里存在过。"""
        store = ShadowStore(self.path, 'exp1')
        store.save_experiment(manifest())
        scope = 'SHADOW:exp1:R'
        # SEC-B 无行情 → DATA_BLOCKED，产出 missed 事件
        self._run_and_save(store, scope, '2026-01-05', bars={},
                           intents=[opp('SEC-B', '2026-01-05')])
        missed = [e for e in store.events(scope) if e['type'] == 'missed']
        self.assertEqual(len(missed), 1)
        self.assertEqual(missed[0]['reason'], 'DATA_BLOCKED')

    def test_uncertain_cost_and_settlement_roundtrip_through_store(self):
        store = ShadowStore(self.path, 'exp1')
        store.save_experiment(manifest())
        scope = 'SHADOW:exp1:L'
        self._run_and_save(store, scope, '2026-01-05', model_cost_uncertain=('a1',))
        r2 = self._run_and_save(store, scope, '2026-01-06', model_cost_settlements={'a1': 500})

        events = store.events(scope)
        self.assertTrue(any(e['type'] == 'model_cost' and e.get('uncertain') for e in events))
        self.assertTrue(any(e['type'] == 'model_cost_settlement' for e in events))
        state = replay(scope, manifest().initial_cash, events)
        self.assertEqual(state.state_hash(), r2.state.state_hash())
        self.assertEqual(state.model_cost, 500)
        self.assertEqual(state.cost_status, 'OK')


if __name__ == '__main__':
    unittest.main()


class LedgerSchemaVersionTests(unittest.TestCase):
    """影子账本是追加式证据：跨代码版本续写必须显式拒绝，不能让实验中途悄悄换尺子。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / 'ledger.sqlite3'

    def _stored_version(self):
        con = sqlite3.connect(str(self.path))
        try:
            return con.execute('SELECT MAX(version) FROM shadow_schema').fetchone()[0]
        finally:
            con.close()

    def test_fresh_ledger_records_the_code_version(self):
        store = ShadowStore(self.path, 'exp1')
        store.save_experiment(manifest())
        self.assertEqual(self._stored_version(), SHADOW_SCHEMA_VERSION)

    def test_ledger_from_another_version_is_refused_on_write(self):
        store = ShadowStore(self.path, 'exp1')
        store.save_experiment(manifest())
        con = sqlite3.connect(str(self.path))
        con.execute('DELETE FROM shadow_schema')
        con.execute('INSERT INTO shadow_schema(version) VALUES (?)', (99,))
        con.commit()
        con.close()
        with self.assertRaises(LedgerSchemaMismatch):
            store.save_experiment(manifest())

    def test_ledger_from_another_version_is_refused_on_read_too(self):
        store = ShadowStore(self.path, 'exp1')
        store.save_experiment(manifest())
        con = sqlite3.connect(str(self.path))
        con.execute('DELETE FROM shadow_schema')
        con.execute('INSERT INTO shadow_schema(version) VALUES (?)', (1,))
        con.commit()
        con.close()
        # 读也拒绝：state_hash 的输入集变了，用新代码解读旧状态只会得出错误结论
        with self.assertRaises(LedgerSchemaMismatch):
            store.latest_state('SHADOW:exp1:R')
