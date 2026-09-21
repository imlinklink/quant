"""机械利润保护（预登记 `EXIT-PROTECT-20260921`）：规则本体 + 引擎接口 + 重放。

覆盖规划 §8 验收矩阵里属于本批的边界：原策略不变、保护执行（激活阈值两侧 / 只收紧 /
跳空 / 当天触发 / H60 冲突）、公司行动（拆股 / 分红锚点）、事件恢复（重放一致 / 中断续跑）。

每个断言都要能**在注入对应缺陷时失败**。四处已按「先注入、确认失败、再还原」验过：

  · 阈值：`test_the_threshold_assertion_is_sensitive_to_a_wrong_parameter` 用 k=1 跑出不同结果；
  · 只收紧：`test_a_pending_stop_below_the_current_stop_never_lowers_it` —— 去掉引擎里的
    `max()` 时**只有这一条**失败（正常路径上 pending 永远更高，其余测试对它不可观测）；
  · 引擎不另写一份 k：`test_engine_reads_the_multiple_from_the_policy`；
  · 前视/次序：把排期块移到「日内硬止损」之前并改成当天立即生效 ⇒ 13 条失败（含
    `test_the_new_stop_does_not_act_on_the_day_it_was_computed`）。
"""
import json
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.replay import replay
from scripts.portfolio_shadow.schema import Manifest, Opportunity, to_micro
from scripts.strategy_research.exit_policy import (ATR_PERIOD, DEFAULT_PROTECTION,
                                                   ProfitProtection)

PREREG = (Path(__file__).resolve().parents[3] / 'docs' /
          'preregistrations' / 'EXIT-PROTECT-20260921.json')


def make_manifest(horizon=60):
    return Manifest(
        experiment_id='x', status='DRAFT', parent_strategy_id='B3', parent_version='1',
        parent_code_hash='abc', universe_id='u', universe_hash='uh',
        account_scopes=('SHADOW:x:R',), initial_cash=to_micro(100000),
        risk_policy={'single_position_risk_bp': 100, 'max_weight_bp': 2000,
                     'max_positions': 5},
        execution_policy={'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': horizon},
        llm_policy={'overlay': 'fixed_pass'}, calendar_version='v1',
        evaluation_protocol={'main_metric': 'L_minus_R_return', 'enrollment_window': '3-6 months',
                             'review_date': '2026-12-31', 'cost_allocation': 'L_pays_model_cost'}
    ).freeze('2026-01-02')


def bar(o, h, low, c):
    return {'open': to_micro(o), 'high': to_micro(h), 'low': to_micro(low), 'close': to_micro(c)}


def opp(sid, exec_session, atr=2.0, policy_id='H60'):
    return Opportunity(experiment_id='x', security_id=sid, source_candidate_id=sid,
                       parent_version='1', signal_session='2026-01-01',
                       observed_at='2026-01-01T00:00Z', planned_execution_session=exec_session,
                       rank=1, entry_rule='b3', stop_reference={'atr14_micro': to_micro(atr)},
                       exit_policy_id=policy_id, input_hash='h')


class _Account:
    """按天推进一个小账户；只在指定的一天带 intent（其余天 intents 为空）。"""

    def __init__(self, protection=None, atr=2.0, horizon=60):
        self.manifest = make_manifest(horizon=horizon)
        self.state = new_account_state('SHADOW:x:R', to_micro(100000))
        self.protection = protection
        self.atr = atr
        self.events = []
        self.due = []

    def day(self, session, next_session, bars, acts=(), intents=None):
        atr_map = {} if self.atr is None else {sid: to_micro(self.atr) for sid in bars}
        r = step(self.state, session=session, bars=bars, corporate_actions=list(acts),
                 intents=list(intents or []), manifest=self.manifest,
                 protection=self.protection, atr=atr_map, next_session=next_session)
        self.state = r.state
        self.events.extend(r.events)
        return r

    def fills(self, reason=None):
        out = [e for e in self.events if e['type'] == 'fill' and e['side'] == 'SELL']
        return [e for e in out if reason is None or e['reason'] == reason]

    def reasons(self):
        return [e['reason'] for e in self.fills()]


def one(sid, session, next_session, o, h, low, c, **kw):
    return {'SEC-A': bar(o, h, low, c)}


class PolicyUnitTests(unittest.TestCase):
    """规则本体：整数、阈值、只收紧、粘性。"""

    def test_activation_price_is_entry_plus_one_r(self):
        p = DEFAULT_PROTECTION
        # 入场 100、止损 92 ⇒ R0/股 = 8 ⇒ 激活价 108
        self.assertEqual(p.activation_price_micro(to_micro(100), to_micro(92)), to_micro(108))

    def test_activation_threshold_is_inclusive_and_boundary_is_exact(self):
        p = DEFAULT_PROTECTION
        kw = dict(entry_price_micro=to_micro(100), initial_stop_micro=to_micro(92),
                  current_stop_micro=to_micro(92), atr14_micro=to_micro(2.0), activated=False)
        # 差 1 微美元不激活
        d = p.evaluate(high_close_micro=to_micro(108) - 1, **kw)
        self.assertFalse(d.activated)
        self.assertEqual(d.reason, 'NOT_ACTIVATED')
        # 恰好到激活价即激活
        self.assertTrue(p.evaluate(high_close_micro=to_micro(108), **kw).activated)

    def test_line_is_k_atr_below_high_and_only_tightens(self):
        p = DEFAULT_PROTECTION
        self.assertEqual(p.line_micro(to_micro(110), to_micro(2.0)), to_micro(104))
        # 线低于既有保护线 ⇒ 不安排更新（NO_CHANGE，stop_micro 为 None）
        d = p.evaluate(entry_price_micro=to_micro(100), initial_stop_micro=to_micro(92),
                       current_stop_micro=to_micro(105), high_close_micro=to_micro(110),
                       atr14_micro=to_micro(2.0), activated=True)
        self.assertEqual(d.reason, 'NO_CHANGE')
        self.assertIsNone(d.stop_micro)

    def test_atr_unavailable_keeps_the_stop(self):
        p = DEFAULT_PROTECTION
        d = p.evaluate(entry_price_micro=to_micro(100), initial_stop_micro=to_micro(92),
                       current_stop_micro=to_micro(92), high_close_micro=to_micro(110),
                       atr14_micro=None, activated=False)
        self.assertEqual(d.reason, 'ATR_UNAVAILABLE')
        self.assertIsNone(d.stop_micro)
        # 激活本身是**价格事实**（H ≥ 激活价），与 ATR 是否可得无关；ATR 缺失只意味着
        # 现在算不出线 ⇒ 保持已生效保护线，绝不放宽硬止损。所以激活为真、线为空。
        self.assertTrue(d.activated)

    def test_activation_is_sticky_because_dividends_lower_high(self):
        """除息把 H 按每股分红下调。非粘性实现会在除息后「取消激活」，已保本的持仓失去保护。"""
        p = DEFAULT_PROTECTION
        d = p.evaluate(entry_price_micro=to_micro(100), initial_stop_micro=to_micro(92),
                       current_stop_micro=to_micro(104), high_close_micro=to_micro(100),
                       atr14_micro=to_micro(2.0), activated=True)
        self.assertTrue(d.activated)

    def test_no_float_in_the_arithmetic(self):
        """大额价格上必须逐微美元精确（浮点会在大数上丢精度）。"""
        p = ProfitProtection()
        entry, stop, atr = 123_456_789_012, 100_000_000_000, 987_654_321
        self.assertEqual(p.activation_price_micro(entry, stop),
                         entry + (entry - stop))
        self.assertEqual(p.line_micro(entry, atr), entry - 3 * atr)

    def test_code_matches_the_frozen_registration(self):
        """参数在预登记里冻结。代码与登记不一致时本测试失败 —— 改参数必须另立登记。"""
        d = json.loads(PREREG.read_text())
        params = d['policy_under_test']['parameters']
        self.assertEqual(params['atr_period'], ATR_PERIOD)
        self.assertEqual(params['atr_period'], DEFAULT_PROTECTION.atr_period)
        self.assertEqual(params['atr_multiple_k'], DEFAULT_PROTECTION.atr_num /
                         DEFAULT_PROTECTION.atr_den)
        self.assertEqual(params['activation_multiple_of_R0'],
                         DEFAULT_PROTECTION.activation_num / DEFAULT_PROTECTION.activation_den)


class ProtectionOffTests(unittest.TestCase):
    """原策略不变：protection=None 时逐事件与本参数引入前相同。"""

    def test_off_produces_no_protection_events_and_keeps_the_original_exit(self):
        a = _Account(protection=None)
        a.day('2026-01-05', '2026-01-06', one('SEC-A', '2026-01-05', '2026-01-06', 100, 101, 99, 100),
              intents=[opp('SEC-A', '2026-01-05')])
        a.day('2026-01-06', '2026-01-07', one('SEC-A', '2026-01-06', '2026-01-07', 100, 112, 100, 110))
        a.day('2026-01-07', '2026-01-08', one('SEC-A', '2026-01-07', '2026-01-08', 106, 107, 103, 105))
        self.assertEqual([e['type'] for e in a.events if e['type'].startswith('protection')
                          or e['type'] == 'stop_update_applied'], [])
        self.assertEqual(a.reasons(), [])          # 止损 92 未被打到
        self.assertIn('SEC-A', a.state.positions)

    def test_off_is_exactly_the_same_as_not_passing_the_argument(self):
        """显式 None 与完全不传参必须给出同一份事件流（默认值不得改变行为）。"""
        def run(pass_none):
            m = make_manifest(horizon=3)
            st = new_account_state('SHADOW:x:R', to_micro(100000))
            evs = []
            kw = {} if not pass_none else {'protection': None, 'atr': None, 'next_session': None}
            for s, nxt in (('2026-01-05', '2026-01-06'), ('2026-01-06', '2026-01-07'),
                           ('2026-01-07', '2026-01-08')):
                r = step(st, session=s, bars=one('SEC-A', s, nxt, 100, 101, 99, 100.5), intents=(
                    [opp('SEC-A', '2026-01-05')] if s == '2026-01-05' else []),
                    corporate_actions=[], manifest=m, **kw)
                st, evs = r.state, evs + r.events
            return evs, st.state_hash()
        self.assertEqual(*[run(flag) for flag in (False, True)])

    def test_protection_requires_next_session(self):
        """没有日历上的下一交易日就没法说「T+1 生效」⇒ 拒绝执行，不猜。"""
        m = make_manifest()
        st = new_account_state('SHADOW:x:R', to_micro(100000))
        with self.assertRaises(ValueError) as ctx:
            step(st, session='2026-01-05', bars=one('SEC-A', '2026-01-05', '2026-01-06',
                                                    100, 101, 99, 100),
                 corporate_actions=[], intents=[], manifest=m,
                 protection=DEFAULT_PROTECTION, atr={})
        self.assertIn('PROTECTION_REQUIRES_NEXT_SESSION', str(ctx.exception))


class ProtectionOnTests(unittest.TestCase):
    """保护执行：次日生效、只收紧、跳空、当天触发、H60 冲突。"""

    def _activate_then_pull_back(self, protection):
        a = _Account(protection=protection)
        a.day('2026-01-05', '2026-01-06', one('SEC-A', '2026-01-05', '2026-01-06', 100, 101, 99, 100),
              intents=[opp('SEC-A', '2026-01-05')])
        a.day('2026-01-06', '2026-01-07', one('SEC-A', '2026-01-06', '2026-01-07', 100, 112, 100, 110))
        a.day('2026-01-07', '2026-01-08', one('SEC-A', '2026-01-07', '2026-01-08', 106, 107, 103, 105))
        return a

    def test_activation_schedules_for_the_next_session_and_raises_the_stop(self):
        a = self._activate_then_pull_back(DEFAULT_PROTECTION)
        sched = [e for e in a.events if e['type'] == 'protection_state' and e['stop_micro']]
        self.assertEqual(len(sched), 1)
        e = sched[0]
        self.assertEqual(e['session'], '2026-01-06')          # T 收盘算
        self.assertEqual(e['effective_session'] if 'effective_session' in e
                         else e['pending_stop_effective_session'], '2026-01-07')
        self.assertEqual(e['stop_micro'], to_micro(104))      # 110 − 3×2
        # 次日生效，且确实抬上去了
        applied = [x for x in a.events if x['type'] == 'stop_update_applied']
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]['session'], '2026-01-07')
        self.assertEqual(applied[0]['previous_stop_micro'], to_micro(92))
        self.assertEqual(applied[0]['stop_micro'], to_micro(104))
        # 保护触发：104 被打到 ⇒ 卖出原因 STOP，价格 = 保护价
        self.assertEqual(a.reasons(), ['STOP'])
        self.assertEqual(a.fills()[0]['price_micro'], to_micro(104))

    def test_the_new_stop_does_not_act_on_the_day_it_was_computed(self):
        """T 收盘算出的线不能用来解释 T 当天的成交（无未来信息）。"""
        a = self._activate_then_pull_back(DEFAULT_PROTECTION)
        sched = [e for e in a.events if e['type'] == 'protection_state' and e['stop_micro']][0]
        # 排期当天（01-06）的 low = 100 > 92（旧止损）⇒ 当天没有卖出
        same_day = [e for e in a.events if e['type'] == 'fill' and e['session'] == '2026-01-06'
                    and e['side'] == 'SELL']
        self.assertEqual(same_day, [])
        self.assertEqual(sched['session'], '2026-01-06')

    def test_the_threshold_assertion_is_sensitive_to_a_wrong_parameter(self):
        """反证：把 k 改小（线更高）会提前一天触发 ⇒ 上面的断言不是恒真。"""
        a = self._activate_then_pull_back(ProfitProtection(atr_num=1))  # 线 = 110 − 2 = 108
        self.assertEqual([x['stop_micro'] for x in a.events
                          if x['type'] == 'stop_update_applied'], [to_micro(108)])
        # 108 高于 01-07 开盘 106 ⇒ 按**开盘价**成交，而不是保护价
        self.assertEqual(a.fills()[0]['price_micro'], to_micro(106))
        self.assertEqual(a.fills()[0]['reason'], 'GAP_STOP')

    def test_gap_exit_uses_the_open_not_the_protection_price(self):
        """§4.2：新保护线高于下一开盘时按开盘触发跳空，不假定能在保护价成交。"""
        a = _Account(protection=ProfitProtection(atr_num=1))
        a.day('2026-01-05', '2026-01-06', one('SEC-A', '2026-01-05', '2026-01-06', 100, 101, 99, 100),
              intents=[opp('SEC-A', '2026-01-05')])
        a.day('2026-01-06', '2026-01-07', one('SEC-A', '2026-01-06', '2026-01-07', 100, 112, 100, 110))
        a.day('2026-01-07', '2026-01-08', one('SEC-A', '2026-01-07', '2026-01-08', 105, 105, 104, 104.5))
        self.assertEqual(a.reasons(), ['GAP_STOP'])
        self.assertEqual(a.fills()[0]['price_micro'], to_micro(105))

    def test_never_lowers_a_stop_and_keeps_rising_with_new_highs(self):
        a = _Account(protection=DEFAULT_PROTECTION)
        a.day('2026-01-05', '2026-01-06', one('SEC-A', '2026-01-05', '2026-01-06', 100, 101, 99, 100),
              intents=[opp('SEC-A', '2026-01-05')])
        a.day('2026-01-06', '2026-01-07', one('SEC-A', '2026-01-06', '2026-01-07', 100, 120, 100, 118))
        a.day('2026-01-07', '2026-01-08', one('SEC-A', '2026-01-07', '2026-01-08', 118, 130, 117, 128))
        a.day('2026-01-08', '2026-01-09', one('SEC-A', '2026-01-08', '2026-01-09', 128, 130, 124, 125))
        stops = [e['stop_micro'] for e in a.events if e['type'] == 'stop_update_applied']
        self.assertEqual(stops, [to_micro(112), to_micro(122)])   # 118−6, 128−6，严格递增
        # 回落之后线不下降：01-08 收盘 125 ⇒ 候选 119 < 122 ⇒ NO_CHANGE
        last = [e for e in a.events if e['type'] == 'protection_state'
                and e['session'] == '2026-01-08'][0]
        self.assertEqual(last['reason'], 'NO_CHANGE')
        self.assertIsNone(last['stop_micro'])

    def test_time_exit_still_works_and_intraday_stop_wins_on_a_tie_day(self):
        """H60 继续有效；同日两者都成立时，盘中止损先成交（冻结优先级）。"""
        a = _Account(protection=DEFAULT_PROTECTION, horizon=3)
        a.day('2026-01-05', '2026-01-06', one('SEC-A', '2026-01-05', '2026-01-06', 100, 101, 99, 100),
              intents=[opp('SEC-A', '2026-01-05')])
        a.day('2026-01-06', '2026-01-07', one('SEC-A', '2026-01-06', '2026-01-07', 100, 112, 100, 110))
        a.day('2026-01-07', '2026-01-08', one('SEC-A', '2026-01-07', '2026-01-08', 106, 107, 103, 105))
        self.assertEqual(a.reasons(), ['STOP'])   # 不是 TIME_EXIT

    def test_a_pending_stop_below_the_current_stop_never_lowers_it(self):
        """**只收紧**是硬不变量，不能托付给调用方。

        `exit_policy.evaluate` 只在严格更高时才排期，所以正常的排期永远更高 —— 这意味着
        「去掉 max()」在正常路径上**不可观测**，上面那些测试抓不到它。这里直接构造一个
        低于既有 stop 的 pending：去掉 max() 时本测试必须失败（那会放宽风险，是危及账户的缺陷）。
        """
        a = _Account(protection=DEFAULT_PROTECTION)
        a.day('2026-01-05', '2026-01-06', one('SEC-A', '2026-01-05', '2026-01-06', 100, 101, 99, 100),
              intents=[opp('SEC-A', '2026-01-05')])
        pos = a.state.positions['SEC-A']
        a.state.positions['SEC-A'] = replace(pos, stop_micro=to_micro(130),
                                             pending_stop_micro=to_micro(120),
                                             pending_stop_effective_session='2026-01-06')
        a.day('2026-01-06', '2026-01-07', one('SEC-A', '2026-01-06', '2026-01-07', 200, 210, 195, 205))
        applied = [e for e in a.events if e['type'] == 'stop_update_applied'][0]
        self.assertEqual(applied['previous_stop_micro'], to_micro(130))
        self.assertEqual(applied['stop_micro'], to_micro(130))
        self.assertEqual(a.state.positions['SEC-A'].stop_micro, to_micro(130))

    def test_atr_missing_keeps_the_previous_stop_and_records_the_reason(self):
        a = _Account(protection=DEFAULT_PROTECTION)
        a.day('2026-01-05', '2026-01-06', one('SEC-A', '2026-01-05', '2026-01-06', 100, 101, 99, 100),
              intents=[opp('SEC-A', '2026-01-05')])
        a.day('2026-01-06', '2026-01-07', one('SEC-A', '2026-01-06', '2026-01-07', 100, 120, 100, 118))
        a.atr = None
        a.day('2026-01-07', '2026-01-08', one('SEC-A', '2026-01-07', '2026-01-08', 118, 130, 117, 128))
        rec = [e for e in a.events if e['type'] == 'protection_state'
               and e['session'] == '2026-01-07'][0]
        self.assertEqual(rec['reason'], 'ATR_UNAVAILABLE')
        self.assertIsNone(rec['stop_micro'])
        # 已排期的 112 仍然生效（数据不足不放宽、也不撤销已排期的更新）
        self.assertEqual([x['stop_micro'] for x in a.events
                          if x['type'] == 'stop_update_applied'], [to_micro(112)])

    def test_engine_reads_the_multiple_from_the_policy_not_from_a_constant(self):
        """注入缺陷即失败：把 k 改成 5，线必须随之改变（说明引擎没有另写一份 3.0）。"""
        a = self._activate_then_pull_back(ProfitProtection(atr_num=5))
        self.assertEqual([x['stop_micro'] for x in a.events
                          if x['type'] == 'stop_update_applied'], [to_micro(100)])


class CorporateActionTests(unittest.TestCase):
    def test_split_scales_high_and_pending_so_no_spurious_exit(self):
        """2:1 拆股：只调 stop 不调 H 会让保护线永久高于市价 ⇒ 立刻假止损。"""
        a = _Account(protection=DEFAULT_PROTECTION)
        a.day('2026-01-05', '2026-01-06', one('SEC-A', '2026-01-05', '2026-01-06', 100, 101, 99, 100),
              intents=[opp('SEC-A', '2026-01-05')])
        a.day('2026-01-06', '2026-01-07', one('SEC-A', '2026-01-06', '2026-01-07', 100, 120, 100, 118))
        # 01-07 除权 2:1：价格折半
        a.day('2026-01-07', '2026-01-08', one('SEC-A', '2026-01-07', '2026-01-08', 60, 61, 59, 60),
              acts=[{'security_id': 'SEC-A', 'action_type': 'split', 'ex_date': '2026-01-07',
                     'ratio': 2}])
        self.assertEqual(a.reasons(), [])
        pos = a.state.positions['SEC-A']
        # 拆股当日收盘 60（已是拆后价）> 拆后 H=59 ⇒ H 取 60
        self.assertEqual(pos.highest_completed_close_micro, to_micro(60))
        self.assertEqual(pos.stop_micro, to_micro(56))                      # 112//2
        # 拆股当天排的新线：H=60 ⇒ 60 − 6 = 54 < 56 ⇒ 只收紧 ⇒ 不更新
        rec = [e for e in a.events if e['type'] == 'protection_state'
               and e['session'] == '2026-01-07'][0]
        self.assertEqual(rec['reason'], 'NO_CHANGE')

    def test_dividend_lowers_high_and_pending_by_the_per_share_amount(self):
        """除息不调整 H ⇒ 保护线按除息前的价格水平停留，除息当天把正常持仓判成触发。"""
        a = _Account(protection=DEFAULT_PROTECTION)
        a.day('2026-01-05', '2026-01-06', one('SEC-A', '2026-01-05', '2026-01-06', 100, 101, 99, 100),
              intents=[opp('SEC-A', '2026-01-05')])
        a.day('2026-01-06', '2026-01-07', one('SEC-A', '2026-01-06', '2026-01-07', 100, 120, 100, 118))
        a.day('2026-01-07', '2026-01-08', one('SEC-A', '2026-01-07', '2026-01-08', 113, 114, 111, 112),
              acts=[{'security_id': 'SEC-A', 'action_type': 'cash_dividend',
                     'ex_date': '2026-01-07', 'pay_date': '2026-01-09',
                     'cash_amount_micro': to_micro(5.0)}])
        self.assertEqual(a.reasons(), [])
        pos = a.state.positions['SEC-A']
        self.assertEqual(pos.highest_completed_close_micro, to_micro(113))  # 118 − 5
        self.assertEqual(pos.stop_micro, to_micro(107))                     # 112 − 5
        # 派发日晚于窗口：钱留在应收里，不伪造已支付
        self.assertEqual(pos.highest_completed_close_micro, to_micro(113))
        self.assertIn('2026-01-09', a.state.dividend_receivable)

    def test_pending_scales_with_a_split_that_happens_before_it_takes_effect(self):
        a = _Account(protection=DEFAULT_PROTECTION)
        a.day('2026-01-05', '2026-01-06', one('SEC-A', '2026-01-05', '2026-01-06', 100, 101, 99, 100),
              intents=[opp('SEC-A', '2026-01-05')])
        # 01-06 收盘 118 ⇒ 排期 112 到 01-07；但 01-07 一开盘就是拆股后的价格
        a.day('2026-01-06', '2026-01-07', one('SEC-A', '2026-01-06', '2026-01-07', 100, 120, 100, 118))
        a.day('2026-01-07', '2026-01-08', one('SEC-A', '2026-01-07', '2026-01-08', 58, 59, 57, 58),
              acts=[{'security_id': 'SEC-A', 'action_type': 'split', 'ex_date': '2026-01-07',
                     'ratio': 2}])
        applied = [x for x in a.events if x['type'] == 'stop_update_applied'][0]
        self.assertEqual(applied['stop_micro'], to_micro(56))   # 112//2
        self.assertEqual(a.reasons(), [])


class ReplayTests(unittest.TestCase):
    def _events(self):
        a = _Account(protection=DEFAULT_PROTECTION)
        a.day('2026-01-05', '2026-01-06', one('SEC-A', '2026-01-05', '2026-01-06', 100, 101, 99, 100),
              intents=[opp('SEC-A', '2026-01-05')])
        a.day('2026-01-06', '2026-01-07', one('SEC-A', '2026-01-06', '2026-01-07', 100, 120, 100, 118))
        a.day('2026-01-07', '2026-01-08', one('SEC-A', '2026-01-07', '2026-01-08', 118, 130, 117, 128))
        return a

    def test_replay_rebuilds_the_same_state_hash(self):
        a = self._events()
        rebuilt = replay('SHADOW:x:R', to_micro(100000), a.events)
        self.assertEqual(rebuilt.state_hash(), a.state.state_hash())

    def test_replay_after_a_crash_midway_matches(self):
        """写入前后中断：用已落库的前两天事件重建，保护线与 H 必须一模一样。"""
        a = self._events()
        cut = [e for e in a.events if e['session'] <= '2026-01-06']
        rebuilt = replay('SHADOW:x:R', to_micro(100000), cut)
        self.assertNotEqual(rebuilt.state_hash(), a.state.state_hash())  # 确实少了一天
        pos = rebuilt.positions['SEC-A']
        self.assertEqual(pos.highest_completed_close_micro, to_micro(118))
        self.assertTrue(pos.protection_activated)
        self.assertEqual(pos.pending_stop_micro, to_micro(112))
        self.assertEqual(pos.pending_stop_effective_session, '2026-01-07')

    def test_resuming_from_the_rebuilt_state_reproduces_the_same_step(self):
        """崩溃后重入：从重建态继续跑第三天，得到与连续运行相同的状态与成交。"""
        a = self._events()
        cut = [e for e in a.events if e['session'] <= '2026-01-06']
        rebuilt = replay('SHADOW:x:R', to_micro(100000), cut)
        r = step(rebuilt, session='2026-01-07',
                 bars=one('SEC-A', '2026-01-07', '2026-01-08', 118, 130, 117, 128),
                 corporate_actions=[], intents=[], manifest=make_manifest(horizon=60),
                 protection=DEFAULT_PROTECTION, atr={'SEC-A': to_micro(2.0)},
                 next_session='2026-01-08')
        self.assertEqual(r.state.state_hash(), a.state.state_hash())
        self.assertEqual(r.state.positions['SEC-A'].stop_micro, to_micro(112))
        self.assertEqual(r.state.positions['SEC-A'].pending_stop_micro, to_micro(122))
        self.assertEqual(r.events, [e for e in a.events if e['session'] == '2026-01-07'])

    def test_a_split_without_adjusting_high_would_break_the_replay(self):
        """反证：重放若不同口径调整 H，哈希就对不上（说明这一步真的进了状态）。"""
        a = self._events()
        mutated = [dict(e) for e in a.events]
        for e in mutated:
            if e['type'] == 'protection_state' and e['security_id'] == 'SEC-A':
                e['high_close_micro'] = e['high_close_micro'] + 1
        rebuilt = replay('SHADOW:x:R', to_micro(100000), mutated)
        self.assertNotEqual(rebuilt.state_hash(), a.state.state_hash())


if __name__ == '__main__':
    unittest.main()
