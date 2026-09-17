"""R/L 双影子账户 paper engine + 重放：设计 §10 必过测试子集。"""
import unittest

from scripts.portfolio_shadow.paper_engine import (initial_stop_micro, new_account_state,
                                                   risk_sized_shares_micro, step)
from scripts.portfolio_shadow.replay import replay
from scripts.portfolio_shadow.schema import Manifest, Opportunity, to_micro


def make_manifest(horizon=60, risk_bp=100, max_weight_bp=2000, max_positions=5):
    return Manifest(
        experiment_id='x', status='DRAFT', parent_strategy_id='B3', parent_version='1',
        parent_code_hash='abc', universe_id='u', universe_hash='uh',
        account_scopes=('SHADOW:x:R', 'SHADOW:x:L'), initial_cash=to_micro(100000),
        risk_policy={'single_position_risk_bp': risk_bp, 'max_weight_bp': max_weight_bp,
                     'max_positions': max_positions},
        execution_policy={'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': horizon},
        llm_policy={'overlay': 'fixed_pass'}, calendar_version='v1',
        evaluation_protocol={'main_metric': 'L_minus_R_return', 'enrollment_window': '3-6 months',
                             'review_date': '2026-12-31', 'cost_allocation': 'L_pays_model_cost'}
    ).freeze('2026-01-02')


def bar(open_, high, low, close):
    return {'open': to_micro(open_), 'high': to_micro(high),
            'low': to_micro(low), 'close': to_micro(close)}


def opp(sid, exec_session, rank=1, atr=2.0):
    return Opportunity(experiment_id='x', security_id=sid, source_candidate_id=sid,
                       parent_version='1', signal_session='2026-01-01',
                       observed_at='2026-01-01T00:00Z', planned_execution_session=exec_session,
                       rank=rank, entry_rule='b3',
                       stop_reference={'atr14_micro': to_micro(atr)}, exit_policy_id='H60',
                       input_hash='h')


def bars1(open_, close, low=None, high=None):
    return {'SEC-A': bar(open_, high if high is not None else close + 1,
                         low if low is not None else open_ - 1, close)}


class SizingTests(unittest.TestCase):
    def test_initial_stop_uses_wider_of_8pct_and_2_5_atr(self):
        # 8% of 100 = 8；2.5×ATR(2)=5 → 取 8 → stop=92
        self.assertEqual(initial_stop_micro(to_micro(100), to_micro(2)), to_micro(92))
        # 2.5×ATR(10)=25 > 8 → stop=75
        self.assertEqual(initial_stop_micro(to_micro(100), to_micro(10)), to_micro(75))

    def test_risk_sized_shares_1pct(self):
        # 1% 风险、8% 止损 → 12.5% 仓位 → 125 股（$100 时）
        shares = risk_sized_shares_micro(to_micro(100), to_micro(92), to_micro(100000),
                                         to_micro(100000), risk_bp=100, max_weight_bp=2000,
                                         fee_bp=10)
        self.assertEqual(shares, 125)


class FixedPassAndIsolationTests(unittest.TestCase):
    def test_fixed_pass_r_equals_l(self):
        m = make_manifest(horizon=3)
        bars = bars1(100, 100.5)
        intents = [opp('SEC-A', '2026-01-05')]
        r = step(new_account_state('SHADOW:x:R', to_micro(100000)), session='2026-01-05',
                 bars=bars, corporate_actions=[], intents=intents, manifest=m)
        l = step(new_account_state('SHADOW:x:L', to_micro(100000)), session='2026-01-05',
                 bars=bars, corporate_actions=[], intents=intents, manifest=m)
        # scope 不同，但持仓/现金/费用/NAV 完全一致（固定 PASS 下 R==L）
        self.assertEqual(r.state.positions, l.state.positions)
        self.assertEqual(r.state.cash_available, l.state.cash_available)
        self.assertEqual(r.state.fees, l.state.fees)
        self.assertEqual(r.nav['equity'], l.nav['equity'])

    def test_account_isolation(self):
        m = make_manifest(horizon=3)
        r_state = new_account_state('SHADOW:x:R', to_micro(100000))
        l_state = new_account_state('SHADOW:x:L', to_micro(100000))
        # R 买 SEC-A，L 空仓
        r = step(r_state, session='2026-01-05', bars=bars1(100, 100.5),
                 corporate_actions=[], intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        l = step(l_state, session='2026-01-05', bars=bars1(100, 100.5),
                 corporate_actions=[], intents=[], manifest=m)
        self.assertEqual(len(r.state.positions), 1)
        self.assertEqual(len(l.state.positions), 0)
        self.assertNotEqual(r.state.state_hash(), l.state.state_hash())


class EntryAndExitTests(unittest.TestCase):
    def test_entry_then_intraday_stop(self):
        m = make_manifest(horizon=60)
        # 入场日：开盘 100 买入，日内 low 90 <= stop(92) → 止损卖出
        bars = bars1(100, 98, low=90, high=101)
        res = step(new_account_state('SHADOW:x:R', to_micro(100000)), session='2026-01-05',
                   bars=bars, corporate_actions=[], intents=[opp('SEC-A', '2026-01-05')],
                   manifest=m)
        self.assertEqual(len(res.state.positions), 0)
        sells = [e for e in res.events if e['type'] == 'fill' and e['side'] == 'SELL']
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]['reason'], 'STOP')

    def test_gap_stop_at_open(self):
        m = make_manifest(horizon=60)
        # day1 买入（未触发止损），day2 开盘跳空到 90 <= stop(92) → GAP_STOP
        s = new_account_state('SHADOW:x:R', to_micro(100000))
        r1 = step(s, session='2026-01-05', bars=bars1(100, 100.5),
                  corporate_actions=[], intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        self.assertEqual(len(r1.state.positions), 1)
        r2 = step(r1.state, session='2026-01-06', bars=bars1(90, 89, low=88, high=90),
                  corporate_actions=[], intents=[], manifest=m)
        self.assertEqual(len(r2.state.positions), 0)
        sells = [e for e in r2.events if e['type'] == 'fill' and e['side'] == 'SELL']
        self.assertEqual(sells[0]['reason'], 'GAP_STOP')

    def test_time_exit_at_close(self):
        m = make_manifest(horizon=2)
        s = new_account_state('SHADOW:x:R', to_micro(100000))
        r1 = step(s, session='2026-01-05', bars=bars1(100, 100.5),
                  corporate_actions=[], intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        self.assertEqual(len(r1.state.positions), 1)
        r2 = step(r1.state, session='2026-01-06', bars=bars1(100.5, 101.5),
                  corporate_actions=[], intents=[], manifest=m)
        self.assertEqual(len(r2.state.positions), 0)
        sells = [e for e in r2.events if e['type'] == 'fill' and e['side'] == 'SELL']
        self.assertEqual(sells[0]['reason'], 'TIME_EXIT')


class CorporateActionTests(unittest.TestCase):
    def test_split_preserves_economic_value(self):
        m = make_manifest(horizon=60)
        s = new_account_state('SHADOW:x:R', to_micro(100000))
        r1 = step(s, session='2026-01-05', bars=bars1(100, 100.5),
                  corporate_actions=[], intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        before = r1.state.equity({'SEC-A': to_micro(100.5)})
        # 2:1 拆股：次日价格从 100 变 50（复权后），数量翻倍
        r2 = step(r1.state, session='2026-01-06', bars=bars1(50, 50.25),
                  corporate_actions=[{'security_id': 'SEC-A', 'action_type': 'split',
                                      'ex_date': '2026-01-06', 'ratio': 2}],
                  intents=[], manifest=m)
        pos = r2.state.positions['SEC-A']
        self.assertEqual(pos.shares, 250)  # 125 → 250
        after = r2.state.equity({'SEC-A': to_micro(50.25)})
        # 拆股前后经济价值一致（价格减半、数量翻倍）
        self.assertAlmostEqual(micro_to_dollars(before), micro_to_dollars(after), places=0)

    def test_dividend_ex_then_pay(self):
        m = make_manifest(horizon=60)
        s = new_account_state('SHADOW:x:R', to_micro(100000))
        r1 = step(s, session='2026-01-05', bars=bars1(100, 100.5),
                  corporate_actions=[], intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        cash_before = r1.state.cash_available
        # ex_date：记应收，不增加可用现金
        r2 = step(r1.state, session='2026-01-06', bars=bars1(100, 100.5),
                  corporate_actions=[{'security_id': 'SEC-A', 'action_type': 'cash_dividend',
                                      'ex_date': '2026-01-06', 'pay_date': '2026-01-08',
                                      'cash_amount_micro': to_micro(1.0)}],
                  intents=[], manifest=m)
        self.assertEqual(r2.state.dividend_receivable, {'2026-01-08': 125 * to_micro(1.0)})
        self.assertEqual(r2.state.cash_available, cash_before)
        # pay_date：应收转可用
        r3 = step(r2.state, session='2026-01-08', bars=bars1(100, 100.5),
                  corporate_actions=[], intents=[], manifest=m)
        self.assertEqual(r3.state.dividend_receivable, {})
        self.assertEqual(r3.state.cash_available, cash_before + 125 * to_micro(1.0))

    def test_dividend_reduces_stop(self):
        # 除息日止损随分红下调（镜像历史引擎 stop -= cash_amount）
        m = make_manifest(horizon=60)
        s = new_account_state('SHADOW:x:R', to_micro(100000))
        r1 = step(s, session='2026-01-05', bars=bars1(100, 100.5),
                  corporate_actions=[], intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        stop_before = r1.state.positions['SEC-A'].stop_micro
        r2 = step(r1.state, session='2026-01-06', bars=bars1(100, 100.5),
                  corporate_actions=[{'security_id': 'SEC-A', 'action_type': 'cash_dividend',
                                      'ex_date': '2026-01-06', 'pay_date': '2026-01-08',
                                      'cash_amount_micro': to_micro(1.0)}],
                  intents=[], manifest=m)
        self.assertEqual(r2.state.positions['SEC-A'].stop_micro, stop_before - to_micro(1.0))

    def test_dividend_stop_reduction_replays(self):
        m = make_manifest(horizon=60)
        state = new_account_state('SHADOW:x:R', to_micro(100000))
        events = []
        res = step(state, session='2026-01-05', bars=bars1(100, 100.5),
                   corporate_actions=[], intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        state = res.state
        events.extend(res.events)
        res = step(state, session='2026-01-06', bars=bars1(100, 100.5),
                   corporate_actions=[{'security_id': 'SEC-A', 'action_type': 'cash_dividend',
                                       'ex_date': '2026-01-06', 'pay_date': '2026-01-08',
                                       'cash_amount_micro': to_micro(1.0)}],
                   intents=[], manifest=m)
        state = res.state
        events.extend(res.events)
        replayed = replay('SHADOW:x:R', to_micro(100000), events)
        self.assertEqual(replayed.positions['SEC-A'].stop_micro, state.positions['SEC-A'].stop_micro)
        self.assertEqual(replayed.state_hash(), state.state_hash())


class SettlementTests(unittest.TestCase):
    def test_sell_proceeds_not_available_same_day(self):
        m = make_manifest(horizon=2)
        s = new_account_state('SHADOW:x:R', to_micro(100000))
        r1 = step(s, session='2026-01-05', bars=bars1(100, 100.5),
                  corporate_actions=[], intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        # day2 时间退出：卖出款进 unsettled，不进入 available
        r2 = step(r1.state, session='2026-01-06', bars=bars1(100.5, 101.5),
                  corporate_actions=[], intents=[opp('SEC-A', '2026-01-06')], manifest=m)
        self.assertGreater(r2.state.unsettled_cash, 0)
        # day3：结算进入 available
        r3 = step(r2.state, session='2026-01-07', bars=bars1(101.5, 102),
                  corporate_actions=[], intents=[], manifest=m)
        self.assertEqual(r3.state.unsettled_cash, 0)


class FunnelTests(unittest.TestCase):
    def test_funnel_terminal_states(self):
        m = make_manifest(horizon=60, max_positions=1)
        s = new_account_state('SHADOW:x:R', to_micro(100000))
        # SEC-A 买入占满仓位；SEC-B 被 MAX_POSITIONS 拒绝；SEC-C 无行情 DATA_BLOCKED
        intents = [opp('SEC-A', '2026-01-05', rank=1), opp('SEC-B', '2026-01-05', rank=2),
                   opp('SEC-C', '2026-01-05', rank=3)]
        bars = {'SEC-A': bar(100, 100.5, 99, 101), 'SEC-B': bar(50, 50.5, 49, 51)}
        res = step(s, session='2026-01-05', bars=bars, corporate_actions=[],
                   intents=intents, manifest=m)
        missed = {e['security_id']: e['reason'] for e in res.events if e['type'] == 'missed'}
        self.assertEqual(missed.get('SEC-B'), 'MAX_POSITIONS')
        self.assertEqual(missed.get('SEC-C'), 'DATA_BLOCKED')
        self.assertEqual(len(res.state.positions), 1)


class InvariantAndReplayTests(unittest.TestCase):
    def test_invariants_hold(self):
        m = make_manifest(horizon=2)
        s = new_account_state('SHADOW:x:R', to_micro(100000))
        r1 = step(s, session='2026-01-05', bars=bars1(100, 100.5),
                  corporate_actions=[], intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        self.assertEqual(r1.state.invariants(), [])
        self.assertGreaterEqual(r1.state.cash_available, 0)

    def test_replay_consistency(self):
        m = make_manifest(horizon=5)
        state = new_account_state('SHADOW:x:R', to_micro(100000))
        all_events = []
        sessions = ['2026-01-05', '2026-01-06', '2026-01-07']
        bars_list = [bars1(100, 100.5), bars1(100.5, 101.5), bars1(101.5, 102.5)]
        for i, sess in enumerate(sessions):
            intents = [opp('SEC-A', sess)] if i == 0 else []
            res = step(state, session=sess, bars=bars_list[i], corporate_actions=[],
                       intents=intents, manifest=m)
            state = res.state
            all_events.extend(res.events)
        replayed = replay('SHADOW:x:R', to_micro(100000), all_events)
        self.assertEqual(state.state_hash(), replayed.state_hash())


def micro_to_dollars(x):
    from decimal import Decimal
    return Decimal(x) / 1_000_000


class ReviewFixTests(unittest.TestCase):
    """针对 review 发现的验收阻塞项：幂等 / holding_sessions 恢复 / 缺行情阻止开仓。"""

    def test_step_is_idempotent_per_session(self):
        m = make_manifest(horizon=60)
        state = new_account_state('SHADOW:x:R', m.initial_cash)
        r1 = step(state, session='2026-01-05', bars=bars1(100, 100.5), corporate_actions=[],
                  intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        r2 = step(r1.state, session='2026-01-05', bars=bars1(100, 100.5), corporate_actions=[],
                  intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        self.assertIsNone(r2.nav)
        self.assertEqual(r2.state.sequence, r1.state.sequence)
        self.assertEqual(r2.state.state_hash(), r1.state.state_hash())

    def test_replay_restores_holding_sessions(self):
        m = make_manifest(horizon=60)
        state = new_account_state('SHADOW:x:R', m.initial_cash)
        events = []
        res = step(state, session='2026-01-05', bars=bars1(100, 100.5), corporate_actions=[],
                   intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        state = res.state
        events.extend(res.events)
        self.assertEqual(state.positions['SEC-A'].holding_sessions, 1)
        replayed = replay('SHADOW:x:R', m.initial_cash, events)
        self.assertEqual(replayed.positions['SEC-A'].holding_sessions, 1)
        self.assertEqual(replayed.state_hash(), state.state_hash())

    def test_missing_held_bars_blocks_new_entries(self):
        m = make_manifest(horizon=60)
        state = new_account_state('SHADOW:x:R', m.initial_cash)
        r1 = step(state, session='2026-01-05', bars={'SEC-A': bar(100, 100.5, 99, 101)},
                  corporate_actions=[], intents=[opp('SEC-A', '2026-01-05')], manifest=m)
        self.assertEqual(len(r1.state.positions), 1)
        # day2：SEC-A 缺行情，但 SEC-B 有行情 → 估值不完整应阻止新开仓
        r2 = step(r1.state, session='2026-01-06', bars={'SEC-B': bar(50, 50.5, 49, 51)},
                  corporate_actions=[], intents=[opp('SEC-B', '2026-01-06')], manifest=m)
        missed = [e for e in r2.events if e['type'] == 'missed']
        self.assertEqual(missed[0]['reason'], 'VALUATION_INCOMPLETE')
        self.assertEqual(len(r2.state.positions), 1)  # SEC-A 仍在
        self.assertEqual(r2.state.valuation_status, 'PROVISIONAL')


class IntentSessionGuardTests(unittest.TestCase):
    """执行日守卫：intent 的计划执行日必须等于当前 session，否则整批拒绝、不按历史价补成交。"""

    def test_mismatched_planned_session_raises_with_locators(self):
        m = make_manifest(horizon=3)
        bad = opp('SEC-A', '2026-01-06')  # 计划次日，却在当日执行
        with self.assertRaises(ValueError) as ctx:
            step(new_account_state('SHADOW:x:R', to_micro(100000)), session='2026-01-05',
                 bars=bars1(100, 100.5), corporate_actions=[], intents=[bad], manifest=m)
        msg = str(ctx.exception)
        self.assertIn('INTENT_SESSION_MISMATCH', msg)
        self.assertIn(bad.opportunity_id(), msg)
        self.assertIn('planned=2026-01-06', msg)
        self.assertIn('session=2026-01-05', msg)

    def test_whole_batch_validated_before_any_state_change(self):
        m = make_manifest(horizon=3)
        state = new_account_state('SHADOW:x:R', to_micro(100000))
        good = opp('SEC-A', '2026-01-05')
        bad = opp('SEC-B', '2026-01-06')
        bars = {'SEC-A': bar(100, 101, 99, 100.5), 'SEC-B': bar(50, 51, 49, 50.5)}
        with self.assertRaises(ValueError):
            step(state, session='2026-01-05', bars=bars, corporate_actions=[],
                 intents=[good, bad], manifest=m)
        # positions 是 replace() 的浅拷贝共享字典，可观察：合法的第一笔也没被成交
        self.assertEqual(state.positions, {})

    def test_matching_planned_session_executes(self):
        m = make_manifest(horizon=3)
        good = opp('SEC-A', '2026-01-05')
        res = step(new_account_state('SHADOW:x:R', to_micro(100000)), session='2026-01-05',
                   bars=bars1(100, 100.5), corporate_actions=[], intents=[good], manifest=m)
        self.assertIn('SEC-A', res.state.positions)

    def test_all_violations_reported_in_one_error(self):
        m = make_manifest(horizon=3)
        bad1, bad2 = opp('SEC-A', '2026-01-06'), opp('SEC-B', '2026-01-07')
        with self.assertRaises(ValueError) as ctx:
            step(new_account_state('SHADOW:x:R', to_micro(100000)), session='2026-01-05',
                 bars=bars1(100, 100.5), corporate_actions=[], intents=[bad1, bad2], manifest=m)
        msg = str(ctx.exception)
        self.assertIn(bad1.opportunity_id(), msg)
        self.assertIn(bad2.opportunity_id(), msg)
        self.assertIn('planned=2026-01-06', msg)
        self.assertIn('planned=2026-01-07', msg)


class ModelCostUncertaintyTests(unittest.TestCase):
    """成本不可知：金额记 0 但必须挂账待补记，不是免费；补记幂等且不改原事件。"""

    def _step(self, state, session, **kw):
        return step(state, session=session, bars=bars1(100, 100.5), corporate_actions=[],
                    intents=[], manifest=make_manifest(horizon=3), **kw)

    def test_uncertain_cost_books_zero_but_marks_settlement_due(self):
        res = self._step(new_account_state('SHADOW:x:L', to_micro(100000)), '2026-01-05',
                         model_cost_uncertain=('a1',))
        self.assertEqual(res.state.model_cost, 0)  # 尚未计入，不是零成本
        self.assertEqual(res.state.model_cost_uncertain_count, 1)
        self.assertEqual(res.state.cost_status, 'PROVISIONAL')
        self.assertEqual(res.nav['cost_status'], 'PROVISIONAL')
        self.assertEqual(res.nav['model_cost_uncertain_count'], 1)
        ev = [e for e in res.events if e['type'] == 'model_cost' and e.get('uncertain')]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]['amount_micro'], 0)
        self.assertEqual(ev[0]['attempt_id'], 'a1')

    def test_uncertain_attempt_is_not_double_booked(self):
        r1 = self._step(new_account_state('SHADOW:x:L', to_micro(100000)), '2026-01-05',
                        model_cost_uncertain=('a1',))
        r2 = self._step(r1.state, '2026-01-06', model_cost_uncertain=('a1',))
        self.assertEqual(r2.state.model_cost_uncertain_count, 1)
        self.assertEqual([e for e in r2.events if e['type'] == 'model_cost'], [])

    def test_settlement_deducts_and_clears(self):
        r1 = self._step(new_account_state('SHADOW:x:L', to_micro(100000)), '2026-01-05',
                        model_cost_uncertain=('a1',))
        r2 = self._step(r1.state, '2026-01-06', model_cost_settlements={'a1': 1234})
        self.assertEqual(r2.state.model_cost, 1234)
        self.assertEqual(r2.state.model_cost_uncertain_count, 0)
        self.assertEqual(r2.state.cost_status, 'OK')
        self.assertEqual(r2.nav['full_cost_equity'], r2.nav['equity'] - 1234)
        ev = [e for e in r2.events if e['type'] == 'model_cost_settlement']
        self.assertEqual(ev[0]['attempt_id'], 'a1')
        self.assertEqual(ev[0]['amount_micro'], 1234)

    def test_settlement_is_idempotent(self):
        r1 = self._step(new_account_state('SHADOW:x:L', to_micro(100000)), '2026-01-05',
                        model_cost_uncertain=('a1',))
        r2 = self._step(r1.state, '2026-01-06', model_cost_settlements={'a1': 1234})
        r3 = self._step(r2.state, '2026-01-07', model_cost_settlements={'a1': 1234})
        self.assertEqual(r3.state.model_cost, 1234)  # 未重复扣减
        self.assertEqual([e for e in r3.events if e['type'] == 'model_cost_settlement'], [])

    def test_settlement_of_unknown_attempt_is_noop(self):
        r = self._step(new_account_state('SHADOW:x:L', to_micro(100000)), '2026-01-05',
                       model_cost_settlements={'never-seen': 999})
        self.assertEqual(r.state.model_cost, 0)
        self.assertEqual(r.events, [e for e in r.events if e['type'] != 'model_cost_settlement'])

    def test_replay_reproduces_uncertain_then_settled_cost(self):
        scope = 'SHADOW:x:L'
        r1 = self._step(new_account_state(scope, to_micro(100000)), '2026-01-05',
                        model_cost_uncertain=('a1', 'a2'))
        r2 = self._step(r1.state, '2026-01-06', model_cost_settlements={'a1': 700})
        replayed = replay(scope, to_micro(100000), r1.events + r2.events)
        self.assertEqual(replayed.model_cost, 700)
        self.assertEqual(replayed.model_cost_unsettled, ('a2',))
        self.assertEqual(replayed.state_hash(), r2.state.state_hash())


if __name__ == '__main__':
    unittest.main()
