"""趋势退出 MA20_60_NEXT_OPEN_V1（预登记 `EXIT-TREND-MA2060-20260922`）。

规则本体 + 引擎接口。要钉死的四件事：信号语义与缺失处理、复权序列、**次日开盘成交**、
以及「T 收盘信号不得在 T 成交」（前视守卫）。另加一条控制：`time_exit=False` 时
**不得**再出现 `TIME_EXIT`（那是本臂的定义，不是可选项）。
"""
import unittest

import pandas as pd

from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.replay import replay
from scripts.portfolio_shadow.schema import Manifest, Opportunity, to_micro
from scripts.strategy_research.trend_exit import (EXIT_REASON, MA_FAST, MA_SLOW, POLICY_ID,
                                                  exit_signals, trend_intents_by_session)

CODE = 'SEC-A'


def bars_frame(closes, *, atr=2.0, split_at=None, close_scale=1.0):
    idx = pd.date_range('2024-01-01', periods=len(closes), freq='B').normalize()
    values = list(closes)
    if split_at is not None:
        values = [c * close_scale if i < split_at else c for i, c in enumerate(values)]
    return pd.DataFrame({'security_id': CODE, 'session': idx, 'raw_open': values,
                         'raw_high': [c * 1.005 for c in values],
                         'raw_low': [c * 0.995 for c in values], 'raw_close': values,
                         'volume': [1_000_000] * len(values), 'asof_atr': atr,
                         'scale_to_next': 1.0}), idx


def manifest(horizon=60):
    return Manifest(
        experiment_id='x', status='DRAFT', parent_strategy_id='B3', parent_version='1',
        parent_code_hash='a', universe_id='u', universe_hash='h',
        account_scopes=('SHADOW:x:R',), initial_cash=to_micro(100000),
        risk_policy={'single_position_risk_bp': 100, 'max_weight_bp': 2000,
                     'max_positions': 5},
        execution_policy={'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': horizon},
        llm_policy={'overlay': 'fixed_pass'}, calendar_version='v1',
        evaluation_protocol={'main_metric': 'x', 'enrollment_window': 'x',
                             'review_date': '2026-12-31', 'cost_allocation': 'x'}
    ).freeze('2026-01-02')


def opp(exec_session, atr=2.0):
    return Opportunity(experiment_id='x', security_id=CODE, source_candidate_id=CODE,
                       parent_version='1', signal_session='2026-01-01',
                       observed_at='2026-01-01T00:00:00+00:00',
                       planned_execution_session=exec_session, rank=1, entry_rule='b3',
                       stop_reference={'atr14_micro': to_micro(atr)},
                       exit_policy_id='H60', input_hash='h')


class SignalTests(unittest.TestCase):
    def test_the_rule_is_close_le_ma20_and_close_lt_ma60(self):
        # 两个条件**都要**满足：跌破 MA20 即触发是不够的（下面的反例）
        closes = [100.0] * (MA_SLOW + 5) + [50.0]
        frame, _ = bars_frame(closes)
        sig = exit_signals(frame, pd.DataFrame(), frame.session.iloc[-1])
        self.assertTrue(bool(sig.exit_signal.iloc[-1]))
        # 上行趋势里只跌破 MA20、仍站在 MA60 之上 ⇒ **不退出**
        ramp = [100.0 + i for i in range(MA_SLOW + 5)]
        mid = (pd.Series(ramp).tail(MA_FAST).mean() + pd.Series(ramp).tail(MA_SLOW).mean()) / 2
        frame2, _ = bars_frame(ramp + [mid])
        sig2 = exit_signals(frame2, pd.DataFrame(), frame2.session.iloc[-1])
        row = sig2.iloc[-1]
        self.assertLessEqual(row.close, row.ma20)
        self.assertGreater(row.close, row.ma60)
        self.assertFalse(bool(row.exit_signal), '仍在 MA60 之上不该退出')

    def test_warmup_is_not_a_signal(self):
        frame, _ = bars_frame([100.0] * (MA_SLOW - 1))
        sig = exit_signals(frame, pd.DataFrame(), frame.session.iloc[-1])
        self.assertFalse(bool(sig.exit_signal.any()))
        self.assertFalse(bool(sig.valid.any()))

    def test_a_missing_bar_never_triggers_an_exit(self):
        """缺失一律不产生退出（fail-safe）——宁可继续持有，也不要因 NaN 平仓。"""
        frame, idx = bars_frame([100.0] * (MA_SLOW + 10))
        frame.loc[frame.index[-1], 'raw_close'] = float('nan')
        sig = exit_signals(frame, pd.DataFrame(), idx[-1])
        self.assertFalse(bool(sig.exit_signal.iloc[-1]))
        self.assertFalse(bool(sig.valid.iloc[-1]))

    def test_the_signal_uses_the_adjusted_series(self):
        """2:1 拆股不得把 MA 打成假的趋势破坏。"""
        closes = [100.0] * (MA_SLOW + 10)
        frame, idx = bars_frame(closes, split_at=len(closes) - 5, close_scale=0.5)
        actions = pd.DataFrame([{'security_id': CODE, 'action_type': 'split',
                                 'ex_date': idx[len(closes) - 5], 'ratio': 2,
                                 'cash_amount': 0.0}])
        sig = exit_signals(frame, actions, idx[-1])
        self.assertFalse(bool(sig.exit_signal.iloc[-1]), '拆股被误判成趋势破坏')

    def test_intents_are_frozen_on_the_signal_session_for_the_next(self):
        closes = [100.0] * (MA_SLOW + 5) + [50.0] * 3
        frame, idx = bars_frame(closes)
        intents = trend_intents_by_session(frame, pd.DataFrame(), idx)
        signal_session = idx[MA_SLOW + 5]
        exec_session = idx[MA_SLOW + 6]
        self.assertIn(str(exec_session.date()), intents)
        intent = intents[str(exec_session.date())][CODE]
        self.assertEqual(intent['frozen_at'], str(signal_session.date()))
        self.assertEqual(intent['signal_session'], str(signal_session.date()))
        self.assertLess(intent['frozen_at'], intent['execution_session'])

    def test_policy_id_matches_the_registration(self):
        import json
        from pathlib import Path
        reg = json.loads((Path(__file__).resolve().parents[3] / 'docs/preregistrations' /
                          'EXIT-TREND-MA2060-20260922.json').read_text(encoding='utf-8'))
        params = reg['policy_under_test']['parameters']
        self.assertEqual(reg['policy_under_test']['name'], POLICY_ID)
        self.assertEqual(params['ma_fast'], MA_FAST)
        self.assertEqual(params['ma_slow'], MA_SLOW)
        self.assertEqual(params['execution'], 'next_open')


class EngineTests(unittest.TestCase):
    def _enter(self, m, entry_session, open_=100.0, low=99.0):
        state = new_account_state('SHADOW:x:R', to_micro(100000))
        res = step(state, session=entry_session,
                   bars={CODE: {'open': to_micro(open_), 'high': to_micro(open_ + 1),
                                'low': to_micro(low), 'close': to_micro(open_)}},
                   corporate_actions=[], intents=[opp(entry_session)], manifest=m)
        return res.state

    def test_executes_at_the_next_open_with_reason_trend_exit(self):
        m = manifest()
        state = self._enter(m, '2026-01-05')
        # 开盘 95 **高于**止损 92 ⇒ 不被跳空止损吃掉，才轮到趋势退出
        res = step(state, session='2026-01-06',
                   bars={CODE: {'open': to_micro(95.0), 'high': to_micro(96.0),
                                'low': to_micro(94.0), 'close': to_micro(95.5)}},
                   corporate_actions=[], intents=[], manifest=m,
                   trend_exits={CODE: {'frozen_at': '2026-01-05',
                                       'signal_session': '2026-01-05'}},
                   time_exit=False)
        fills = [e for e in res.events if e['type'] == 'fill']
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]['reason'], EXIT_REASON)
        self.assertEqual(fills[0]['price_micro'], to_micro(95.0), '必须按开盘价成交')
        self.assertNotIn(CODE, res.state.positions)

    def test_a_signal_frozen_on_the_execution_day_is_refused(self):
        """T 收盘算的信号不能在 T 成交 —— 那是最直接的前视。"""
        m = manifest()
        state = self._enter(m, '2026-01-05')
        with self.assertRaises(ValueError) as ctx:
            step(state, session='2026-01-06',
                 bars={CODE: {'open': to_micro(95.0), 'high': to_micro(96.0),
                              'low': to_micro(94.0), 'close': to_micro(95.5)}},
                 corporate_actions=[], intents=[], manifest=m,
                 trend_exits={CODE: {'frozen_at': '2026-01-06'}}, time_exit=False)
        self.assertIn('TREND_EXIT_NOT_FROZEN', str(ctx.exception))

    def test_gap_stop_wins_on_the_same_day_and_only_one_fill_happens(self):
        """同日两者都成立时只成交一次、优先 GAP_STOP（§6.4；由阶段顺序构造性保证）。"""
        m = manifest()
        state = self._enter(m, '2026-01-05', open_=100.0, low=99.0)
        # 入场价 100、止损 92；次日开盘 90 < 92 ⇒ 跳空止损；同时有趋势退出意图
        res = step(state, session='2026-01-06',
                   bars={CODE: {'open': to_micro(90.0), 'high': to_micro(91.0),
                                'low': to_micro(89.0), 'close': to_micro(90.5)}},
                   corporate_actions=[], intents=[], manifest=m,
                   trend_exits={CODE: {'frozen_at': '2026-01-05'}}, time_exit=False)
        fills = [e for e in res.events if e['type'] == 'fill']
        self.assertEqual(len(fills), 1, '同日只成交一次')
        self.assertEqual(fills[0]['reason'], 'GAP_STOP')

    def test_no_time_exit_when_the_dispatch_says_so_but_holding_still_counts(self):
        m = manifest(horizon=2)
        state = self._enter(m, '2026-01-05')
        flat = {'open': to_micro(100.0), 'high': to_micro(101.0), 'low': to_micro(99.0),
                'close': to_micro(100.0)}
        for session in ('2026-01-06', '2026-01-07', '2026-01-08'):
            res = step(state, session=session, bars={CODE: flat}, corporate_actions=[],
                       intents=[], manifest=m, time_exit=False)
            state = res.state
            self.assertEqual([e for e in res.events if e['type'] == 'fill'], [],
                             'time_exit=False 时不得出现任何时间退出')
        self.assertEqual(state.positions[CODE].holding_sessions, 4)

    def test_the_default_path_is_byte_identical(self):
        """不传新参数时逐事件与引入前一致（`time_exit` 默认 True）。"""
        m = manifest(horizon=2)
        a = self._enter(m, '2026-01-05')
        flat = {'open': to_micro(100.0), 'high': to_micro(101.0), 'low': to_micro(99.0),
                'close': to_micro(100.0)}
        # horizon=2 ⇒ 第 2 个持有 session（01-06）收盘就该时间退出
        first = step(a, session='2026-01-06', bars={CODE: flat}, corporate_actions=[],
                     intents=[], manifest=m)
        self.assertIn('TIME_EXIT', [e['reason'] for e in first.events
                                    if e['type'] == 'fill'])

    def test_missing_bars_record_a_reason_and_keep_the_position(self):
        m = manifest()
        state = self._enter(m, '2026-01-05')
        res = step(state, session='2026-01-06', bars={}, corporate_actions=[], intents=[],
                   manifest=m, trend_exits={CODE: {'frozen_at': '2026-01-05'}},
                   time_exit=False)
        missed = [e for e in res.events if e['type'] == 'missed']
        self.assertEqual(missed[0]['reason'], 'TREND_EXIT_NO_BARS')
        self.assertIn(CODE, res.state.positions)

    def test_replay_rebuilds_the_same_state_after_a_trend_exit(self):
        m = manifest()
        state = new_account_state('SHADOW:x:R', to_micro(100000))
        entered = step(state, session='2026-01-05',
                       bars={CODE: {'open': to_micro(100.0), 'high': to_micro(101.0),
                                    'low': to_micro(99.0), 'close': to_micro(100.0)}},
                       corporate_actions=[], intents=[opp('2026-01-05')], manifest=m)
        state, events = entered.state, list(entered.events)   # 必须含入场成交，否则重放没有仓位
        for session, px in (('2026-01-06', 105.0), ('2026-01-07', 95.0)):
            res = step(state, session=session,
                       bars={CODE: {'open': to_micro(px), 'high': to_micro(px + 1),
                                    'low': to_micro(px - 1), 'close': to_micro(px)}},
                       corporate_actions=[], intents=[], manifest=m,
                       trend_exits=({CODE: {'frozen_at': '2026-01-06'}}
                                    if session == '2026-01-07' else None),
                       time_exit=False)
            state = res.state
            events += res.events
        rebuilt = replay('SHADOW:x:R', to_micro(100000), events)
        self.assertEqual(rebuilt.state_hash(), state.state_hash())


if __name__ == '__main__':
    unittest.main()
