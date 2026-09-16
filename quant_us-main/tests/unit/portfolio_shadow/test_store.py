"""store + replay 集成：save_state / latest_state / events 重放一致 + scope 隔离。"""
import tempfile
import unittest
from pathlib import Path

from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.replay import replay
from scripts.portfolio_shadow.schema import Manifest, Opportunity, to_micro
from scripts.portfolio_shadow.store import ShadowStore, state_from_dict


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

    def _run_and_save(self, store, scope, session):
        m = manifest()
        bars = {'SEC-A': {'open': to_micro(100), 'high': to_micro(101),
                          'low': to_micro(99), 'close': to_micro(100.5)}}
        res = step(new_account_state(scope, m.initial_cash), session=session, bars=bars,
                   corporate_actions=[], intents=[opp('SEC-A', session)], manifest=m)
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


if __name__ == '__main__':
    unittest.main()
