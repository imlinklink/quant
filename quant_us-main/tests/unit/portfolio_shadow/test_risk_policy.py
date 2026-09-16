"""回撤阶梯 + 引擎集成 + 重放一致测试（PR4）。"""
import unittest
from dataclasses import replace

from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.replay import replay
from scripts.portfolio_shadow.risk_policy import (budget_bp, entry_allowed, evaluate_ladder)
from scripts.portfolio_shadow.schema import Manifest, Opportunity, to_micro


def make_manifest(horizon=60, risk_bp=100):
    return Manifest(
        experiment_id='x', status='DRAFT', parent_strategy_id='B3', parent_version='1',
        parent_code_hash='abc', universe_id='u', universe_hash='uh',
        account_scopes=('SHADOW:x:R', 'SHADOW:x:L'), initial_cash=to_micro(100000),
        risk_policy={'single_position_risk_bp': risk_bp, 'max_weight_bp': 2000,
                     'max_positions': 5},
        execution_policy={'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': horizon},
        llm_policy={'overlay': 'fixed_pass'}, calendar_version='v1',
        evaluation_protocol={'main_metric': 'L_minus_R_return', 'enrollment_window': '3-6 months',
                             'review_date': '2026-12-31', 'cost_allocation': 'L_pays_model_cost'}
    ).freeze('2026-01-02')


def opp(sid, sess):
    return Opportunity(experiment_id='x', security_id=sid, source_candidate_id=sid,
                       parent_version='1', signal_session='2026-01-01',
                       observed_at='2026-01-01T00:00:00+00:00', planned_execution_session=sess,
                       rank=1, entry_rule='b3', stop_reference={'atr14_micro': to_micro(2.0)},
                       exit_policy_id='H60', input_hash='h')


def bars1(open_, close, low=None):
    return {'SEC-A': {'open': to_micro(open_), 'high': to_micro(close + 1),
                      'low': to_micro(low if low is not None else open_ - 1),
                      'close': to_micro(close)}}


class LadderTests(unittest.TestCase):
    def test_rising_thresholds(self):
        self.assertEqual(evaluate_ladder(0.21, 'NORMAL', 0, None), ('LIMIT_BREACH', 0))
        self.assertEqual(evaluate_ladder(0.19, 'NORMAL', 0, None), ('REVIEW_REQUIRED', 0))
        self.assertEqual(evaluate_ladder(0.16, 'NORMAL', 0, None), ('PAUSED_ENTRY', 0))
        self.assertEqual(evaluate_ladder(0.11, 'NORMAL', 0, None), ('REDUCED', 0))
        self.assertEqual(evaluate_ladder(0.05, 'NORMAL', 0, None), ('NORMAL', 0))

    def test_reduced_recovery_requires_5_sessions(self):
        # streak 3 → 4，未满 5
        self.assertEqual(evaluate_ladder(0.07, 'REDUCED', 3, None), ('REDUCED', 4))
        # streak 4 → 5 → NORMAL
        self.assertEqual(evaluate_ladder(0.07, 'REDUCED', 4, None), ('NORMAL', 0))

    def test_reduced_between_8_and_10_stays_reduced(self):
        # 8%~10% 之间：既不升也不恢复（迟滞）
        self.assertEqual(evaluate_ladder(0.09, 'REDUCED', 0, None), ('REDUCED', 0))

    def test_paused_recovery_downgrades_to_reduced(self):
        self.assertEqual(evaluate_ladder(0.11, 'PAUSED_ENTRY', 4, None), ('REDUCED', 0))

    def test_review_and_limit_do_not_auto_recover(self):
        self.assertEqual(evaluate_ladder(0.02, 'REVIEW_REQUIRED', 0, None), ('REVIEW_REQUIRED', 0))
        self.assertEqual(evaluate_ladder(0.02, 'LIMIT_BREACH', 0, None), ('LIMIT_BREACH', 0))

    def test_entry_allowed_and_budget(self):
        self.assertTrue(entry_allowed('NORMAL'))
        self.assertTrue(entry_allowed('REDUCED'))
        self.assertFalse(entry_allowed('PAUSED_ENTRY'))
        self.assertEqual(budget_bp('NORMAL', 100), 100)
        self.assertEqual(budget_bp('REDUCED', 100), 50)
        self.assertEqual(budget_bp('PAUSED_ENTRY', 100), 0)


class EngineLadderTests(unittest.TestCase):
    def test_paused_state_blocks_entries(self):
        m = make_manifest()
        state = replace(new_account_state('SHADOW:x:R', m.initial_cash), risk_state='PAUSED_ENTRY')
        res = step(state, session='2026-01-05', bars=bars1(100, 100.5), corporate_actions=[],
                   intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        self.assertEqual(len(res.state.positions), 0)
        missed = [e for e in res.events if e['type'] == 'missed']
        self.assertEqual(missed[0]['reason'], 'RISK_PAUSED')

    def test_drawdown_triggers_paused_entry(self):
        m = make_manifest()
        state = replace(new_account_state('SHADOW:x:R', m.initial_cash),
                        high_water=to_micro(120000))  # 模拟历史高点，当前 10 万 → 回撤 16.7%
        res = step(state, session='2026-01-05', bars=bars1(100, 100), corporate_actions=[],
                   intents=[], manifest=m)
        self.assertEqual(res.state.risk_state, 'PAUSED_ENTRY')

    def test_replay_consistent_with_ladder(self):
        m = make_manifest(horizon=60)
        state = new_account_state('SHADOW:x:R', m.initial_cash)
        events = []
        for sess, (o, c) in [('2026-01-05', (100, 100)), ('2026-01-06', (100, 100))]:
            res = step(state, session=sess, bars=bars1(o, c), corporate_actions=[],
                       intents=[], manifest=m)
            state = res.state
            events.extend(res.events)
        replayed = replay('SHADOW:x:R', m.initial_cash, events)
        self.assertEqual(state.state_hash(), replayed.state_hash())
        self.assertEqual(replayed.risk_state, state.risk_state)


if __name__ == '__main__':
    unittest.main()
