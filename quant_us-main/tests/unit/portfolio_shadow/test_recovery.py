"""恢复与幂等一致性（第3条）：重启继续 / 崩溃补齐 / 两进程并发 / 乱序 / 估值缺口。"""
import tempfile
import unittest
from pathlib import Path

from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.report import paired_performance
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
                       observed_at='2026-01-01T00:00:00+00:00', planned_execution_session=sess,
                       rank=1, entry_rule='b3', stop_reference={'atr14_micro': to_micro(2.0)},
                       exit_policy_id='H60', input_hash='h')


def bars_a(close=100.0):
    return {'SEC-A': {'open': to_micro(100.0), 'high': to_micro(101.0),
                      'low': to_micro(99.0), 'close': to_micro(close)}}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = ShadowStore(Path(self.tmp) / 'ledger.sqlite3', 'exp1')
        self.store.save_experiment(manifest())
        self.m = manifest()

    def _step(self, state, session, bars, intents=None, model_cost=0):
        return step(state, session=session, bars=bars, corporate_actions=[],
                    intents=intents or [], manifest=self.m, model_cost=model_cost)

    def test_continuous_equals_restart_continue(self):
        days = [('2026-01-05', bars_a(100.5)), ('2026-01-06', bars_a(101.0)),
                ('2026-01-07', bars_a(102.0))]
        # 连续运行
        cont = new_account_state('SHADOW:exp1:R', self.m.initial_cash)
        for sess, bars in days:
            intents = [opp('SEC-A', sess)] if sess == days[0][0] else []
            cont = self._step(cont, sess, bars, intents).state
        # 中途重启（每天落库并从库恢复）
        restart = new_account_state('SHADOW:exp1:R', self.m.initial_cash)
        for sess, bars in days:
            intents = [opp('SEC-A', sess)] if sess == days[0][0] else []
            res = self._step(restart, sess, bars, intents)
            self.store.save_state('SHADOW:exp1:R', res.state, res.nav, res.events)
            _, saved = self.store.latest_state('SHADOW:exp1:R')
            restart = state_from_dict(saved)
        self.assertEqual(cont.state_hash(), restart.state_hash())
        self.assertEqual(cont.positions, restart.positions)

    def test_crash_after_r_committed_fills_l_only(self):
        # session 1：R 提交、L 崩溃未提交
        r = self._step(new_account_state('SHADOW:exp1:R', self.m.initial_cash), '2026-01-05',
                       bars_a(100.5), intents=[opp('SEC-A', '2026-01-05')])
        self.store.save_state('SHADOW:exp1:R', r.state, r.nav, r.events)
        # 重启：R 重跑 no-op，L 补齐
        _, r_saved = self.store.latest_state('SHADOW:exp1:R')
        r_rerun = self._step(state_from_dict(r_saved), '2026-01-05', bars_a(100.5),
                             intents=[opp('SEC-A', '2026-01-05')])
        self.assertIsNone(r_rerun.nav)  # R 不重复推进
        l = self._step(new_account_state('SHADOW:exp1:L', self.m.initial_cash), '2026-01-05',
                       bars_a(100.5), intents=[opp('SEC-A', '2026-01-05')], model_cost=100)
        self.store.save_state('SHADOW:exp1:L', l.state, l.nav, l.events)
        self.assertEqual(self.store.latest_state('SHADOW:exp1:R')[0], 1)
        self.assertEqual(self.store.latest_state('SHADOW:exp1:L')[0], 1)
        # L 模型成本只计一次
        self.assertEqual(state_from_dict(self.store.latest_state('SHADOW:exp1:L')[1]).model_cost, 100)

    def test_two_processes_same_session_commit_once(self):
        a = self._step(new_account_state('SHADOW:exp1:R', self.m.initial_cash), '2026-01-05',
                       bars_a(100.5), intents=[opp('SEC-A', '2026-01-05')])
        b = self._step(new_account_state('SHADOW:exp1:R', self.m.initial_cash), '2026-01-05',
                       bars_a(100.5), intents=[opp('SEC-A', '2026-01-05')])
        self.store.save_state('SHADOW:exp1:R', a.state, a.nav, a.events)
        self.store.save_state('SHADOW:exp1:R', b.state, b.nav, b.events)  # 幂等
        self.assertEqual(len(self.store.daily_nav('SHADOW:exp1:R')), 1)
        self.assertEqual(self.store.latest_state('SHADOW:exp1:R')[0], 1)

    def test_same_sequence_different_state_conflicts(self):
        a = self._step(new_account_state('SHADOW:exp1:R', self.m.initial_cash), '2026-01-05',
                       bars_a(100.5), intents=[opp('SEC-A', '2026-01-05')])
        b = self._step(new_account_state('SHADOW:exp1:R', self.m.initial_cash), '2026-01-05',
                       bars_a(110.0), intents=[opp('SEC-A', '2026-01-05')])
        self.store.save_state('SHADOW:exp1:R', a.state, a.nav, a.events)
        with self.assertRaises(ValueError):
            self.store.save_state('SHADOW:exp1:R', b.state, b.nav, b.events)

    def test_out_of_order_session_rejected(self):
        state = new_account_state('SHADOW:exp1:R', self.m.initial_cash)
        res = self._step(state, '2026-01-06', bars_a(100.5))
        with self.assertRaises(ValueError):
            self._step(res.state, '2026-01-05', bars_a(100.5))

    def test_paired_performance_stops_at_gap(self):
        m = self.m
        # day1 完整（买入 SEC-A），day2 缺行情 → PROVISIONAL 缺口
        for scope in ('SHADOW:exp1:R', 'SHADOW:exp1:L'):
            res = self._step(new_account_state(scope, m.initial_cash), '2026-01-05',
                             bars_a(100.5), intents=[opp('SEC-A', '2026-01-05')])
            self.store.save_state(scope, res.state, res.nav, res.events)
        for scope in ('SHADOW:exp1:R', 'SHADOW:exp1:L'):
            _, saved = self.store.latest_state(scope)
            res = self._step(state_from_dict(saved), '2026-01-06', bars={})
            self.store.save_state(scope, res.state, res.nav, res.events)
        p = paired_performance(self.store, m)
        self.assertEqual(p['common_sessions'], 1)  # 停在缺口前，不跨过继续
        self.assertEqual(p['total_sessions'], 2)
        self.assertEqual(p['excluded_after_gap'], 1)

    def test_opportunity_rewrite_is_idempotent(self):
        o = opp('SEC-A', '2026-01-05')
        self.store.put_opportunity(o)
        self.store.put_opportunity(o)  # 重复写入不应报「事件冲突」
        self.assertEqual(len(self.store.opportunities()), 1)

    def test_cli_style_crash_recovery_rewrites_opportunity(self):
        # 模拟 run-session 顺序：先写机会 → 再检查账户；R 已提交 L 未提交时重启
        m = self.m
        o = opp('SEC-A', '2026-01-05')
        # 第一次：写机会 + R 提交，L 崩溃
        self.store.put_opportunity(o)
        r = self._step(new_account_state('SHADOW:exp1:R', m.initial_cash), '2026-01-05',
                       bars_a(100.5), intents=[o])
        self.store.save_state('SHADOW:exp1:R', r.state, r.nav, r.events)
        # 重启：再次写同一机会（幂等，不崩），R no-op，L 补齐
        self.store.put_opportunity(o)
        _, r_saved = self.store.latest_state('SHADOW:exp1:R')
        r_rerun = self._step(state_from_dict(r_saved), '2026-01-05', bars_a(100.5), intents=[o])
        self.assertIsNone(r_rerun.nav)  # R 不重复推进
        l = self._step(new_account_state('SHADOW:exp1:L', m.initial_cash), '2026-01-05',
                       bars_a(100.5), intents=[o], model_cost=100)
        self.store.save_state('SHADOW:exp1:L', l.state, l.nav, l.events)
        self.assertEqual(self.store.latest_state('SHADOW:exp1:R')[0], 1)
        self.assertEqual(self.store.latest_state('SHADOW:exp1:L')[0], 1)

    def test_replay_then_continue_until_time_exit(self):
        # 事件重放重建状态后，继续步进直到时间退出
        from scripts.portfolio_shadow.replay import replay
        m = make_manifest_horizon3()
        state = new_account_state('SHADOW:exp1:R', m.initial_cash)
        events = []
        for sess in ['2026-01-05', '2026-01-06']:
            res = step(state, session=sess, bars=bars_a(100.5), corporate_actions=[],
                       intents=[opp('SEC-A', sess)] if sess == '2026-01-05' else [], manifest=m)
            state = res.state
            events.extend(res.events)
        replayed = replay('SHADOW:exp1:R', m.initial_cash, events)
        self.assertEqual(replayed.positions['SEC-A'].holding_sessions, 2)
        # 从重放状态继续：第 3 天应时间退出（horizon=3）
        res = step(replayed, session='2026-01-07', bars=bars_a(100.5), corporate_actions=[],
                   intents=[], manifest=m)
        self.assertEqual(len(res.state.positions), 0)
        sells = [e for e in res.events if e['type'] == 'fill' and e['side'] == 'SELL']
        self.assertEqual(sells[0]['reason'], 'TIME_EXIT')

    def test_paired_performance_detects_both_missing_with_calendar(self):
        m = self.m
        # day1 完整、day2 双方都漏、day3 完整
        for scope in ('SHADOW:exp1:R', 'SHADOW:exp1:L'):
            res = self._step(new_account_state(scope, m.initial_cash), '2026-01-05',
                             bars_a(100.5), intents=[])
            self.store.save_state(scope, res.state, res.nav, res.events)
        for scope in ('SHADOW:exp1:R', 'SHADOW:exp1:L'):
            _, saved = self.store.latest_state(scope)
            res = self._step(state_from_dict(saved), '2026-01-07', bars_a(100.5), intents=[])
            self.store.save_state(scope, res.state, res.nav, res.events)
        # 无日历：union 视为连续（漏掉的 01-06 无法识别）
        self.assertEqual(paired_performance(self.store, m)['common_sessions'], 2)
        # 有日历：识别 01-06 双方漏掉 → 停在 01-05
        p = paired_performance(self.store, m,
                               calendar=['2026-01-05', '2026-01-06', '2026-01-07'])
        self.assertEqual(p['common_sessions'], 1)


def make_manifest_horizon3():
    return Manifest(
        experiment_id='exp1', status='DRAFT', parent_strategy_id='B3', parent_version='1',
        parent_code_hash='abc', universe_id='u', universe_hash='uh',
        account_scopes=('SHADOW:exp1:R', 'SHADOW:exp1:L'), initial_cash=to_micro(100000),
        risk_policy={'single_position_risk_bp': 100, 'max_weight_bp': 2000, 'max_positions': 5},
        execution_policy={'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': 3},
        llm_policy={'overlay': 'fixed_pass'}, calendar_version='v1',
        evaluation_protocol={'main_metric': 'L_minus_R_return', 'enrollment_window': '3-6 months',
                             'review_date': '2026-12-31', 'cost_allocation': 'L_pays_model_cost'}
    ).freeze('2026-01-02')


if __name__ == '__main__':
    unittest.main()
