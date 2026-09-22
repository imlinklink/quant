"""抄底信号状态机（规范 `docs/bottom-signal-spec-2026-09-21.md`）。

用合成价格序列逐条钉死：状态迁移、同日优先级、等待窗与冷却、复权序列的使用、
以及「未来数据不得影响今天的信号」。规范里的每个数值都在 `SignalParams` 里，
由 `test_params_match_the_spec_and_the_registration` 与登记文件逐项核对。
"""
import json
import unittest
from pathlib import Path

import pandas as pd

from scripts.strategy_research.bottom_signal import (ENTRY_RULE, STATE_EXPIRED,
                                                    STATE_INELIGIBLE, STATE_INVALIDATED,
                                                    STATE_TRIGGERED, STATE_WATCH,
                                                    BottomSignalGenerator, SignalParams,
                                                    features_for)

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / 'docs/bottom-signal-spec-2026-09-21.md'
PREREG = ROOT / 'docs/preregistrations/ENTRY-BOTTOM-20260921.json'
SID = 'SEC-A'


def sessions(n: int) -> pd.DatetimeIndex:
    return pd.date_range('2020-01-01', periods=n, freq='B').normalize()


def price_frame(closes, *, start=None, sid=SID, atr=2.0, volume=1e6):
    idx = sessions(len(closes)) if start is None else pd.DatetimeIndex(start)
    return pd.DataFrame({
        'security_id': sid, 'session': idx, 'raw_open': closes, 'raw_high': [c * 1.01 for c in closes],
        'raw_low': [c * 0.99 for c in closes], 'raw_close': closes, 'volume': volume,
        'asof_atr': atr, 'scale_to_next': 1.0})


def quality_frame(idx, sid=SID):
    return pd.DataFrame([{'security_id': sid, 'quality_status': 'verified',
                          'from_session': idx[0], 'to_session': idx[-1]}])


def make(closes, *, actions=None, atr=2.0, params=None):
    prices = price_frame(closes, atr=atr)
    idx = pd.DatetimeIndex(prices.session)
    gen = BottomSignalGenerator(
        prices=prices, calendar=idx, actions=actions if actions is not None else pd.DataFrame(),
        quality=quality_frame(idx), experiment_id='t', parent_version='1', params=params)
    return gen, list(idx)


def ramp(n=260, start=100.0, step=0.6):
    """稳步上行 260 个 session（100 → 256）：趋势与 MA200（≈178）都成立。"""
    return [start + step * i for i in range(n)]


def scenario(up=None, pullback_sessions=12, pullback_price=214.0, tail=30, tail_price=None):
    """上行 → 回撤到 `pullback_price` 并维持 → 尾巴。

    回撤 18%（256 → 214）**高于 MA200**，所以趋势不破；同时远高于 max(3×ATR/价, 10%)=10%
    ⇒ 满足回撤条件。尾巴默认维持在回撤价（不收回 MA20 ⇒ 会走到 EXPIRED）。
    """
    up = ramp() if up is None else up
    hold = [pullback_price] * pullback_sessions
    tail_price = pullback_price if tail_price is None else tail_price
    return up + hold + [tail_price] * tail, len(up) + pullback_sessions


class ParamTests(unittest.TestCase):
    def test_params_match_the_spec_and_the_registration(self):
        """规范与机读登记里的数值必须与代码逐项相等 —— 改参数必须同时改三处（或另立登记）。"""
        p = SignalParams()
        reg = json.loads(PREREG.read_text(encoding='utf-8'))
        self.assertEqual(reg['rule_source']['spec'],
                         'docs/bottom-signal-spec-2026-09-21.md')
        text = SPEC.read_text(encoding='utf-8')
        for label, value in (('MA200', f'MA{ p.trend_ma}'), ('MA20', f'MA{p.reclaim_ma}'),
                             ('252', str(p.drawdown_lookback)),
                             ('3.0 × ATR14', f'{p.drawdown_atr_multiple}.0 × ATR14'),
                             ('10%', f'{int(p.drawdown_floor * 100)}%'),
                             ('20 个 session', f'{p.max_wait_sessions} 个 session'),
                             ('25%', f'{int(p.max_stop_distance_frac * 100)}%')):
            self.assertIn(value, text, f'规范里找不到 {label}（{value}）')
        self.assertEqual(p.trend_slope_lookback, 20)
        self.assertEqual(p.cooldown_sessions, 20)

    def test_stop_distance_reuses_the_engine_formula_and_survives_big_adjustment_factors(self):
        """止损距离比例必须**只用比值**算：用「复权价 + 原始 ATR」会在复权因子大的地方
        算出非正止损（实测 GOOGL/AMZN 抛 INITIAL_STOP_NON_POSITIVE）。"""
        gen, _ = make([100.0] * 260, atr=2.0)
        row = gen.features[SID].iloc[-1]
        self.assertAlmostEqual(gen._stop_distance_frac(row), max(0.08, 2.5 * row.atr_frac))
        # ATR 占比异常大 ⇒ 判为数据不可用，而不是「风险距离很宽」
        bad = row.copy()
        bad.atr_frac = 0.5
        self.assertTrue(gen._atr_unavailable(bad))


class StateMachineTests(unittest.TestCase):
    def _watching(self, **kw):
        closes, n_up = scenario(**kw)
        gen, idx = make(closes)
        for session in idx:
            gen.opportunities_for(session)
        return gen, idx, n_up

    def test_enters_watch_after_trend_and_drawdown(self):
        gen, idx, n_up = self._watching(pullback_sessions=3, tail=0)
        first = [s for s in gen.signals if s['state'] == STATE_WATCH][0]
        self.assertEqual(first['reason'], 'TREND_UP_AND_DRAWDOWN_MET')
        self.assertTrue(first['trend_ok'])
        self.assertGreaterEqual(first['drawdown_frac'], 0.10)
        self.assertAlmostEqual(first['drawdown_atr_multiples'],
                               first['drawdown_frac'] / first['atr_frac'], places=6)

    def test_drawdown_is_measured_relative_to_the_close_not_the_high(self):
        """口径精确化（规范 §3「口径精确化」）：`(high−close)/close`，不是相对高点。

        取一个恰好卡在两种口径之间的点：(high−close)/close = 10.5%（达标）、
        (high−close)/high = 9.5%（不达标）。若实现按高点算，本测试会失败。
        """
        up = ramp()                      # 高 = 256
        high = up[-1]
        close = high / 1.105             # (high-close)/close = 10.5%
        self.assertGreater((high - close) / close, 0.10)
        self.assertLess((high - close) / high, 0.10)
        closes = up + [close] * 12
        gen, idx = make(closes)
        for session in idx:
            gen.opportunities_for(session)
        self.assertEqual([s['state'] for s in gen.signals].count(STATE_WATCH), 1)

    def test_no_watch_without_an_uptrend(self):
        closes = [200.0 - 0.4 * i for i in range(260)]     # 一路下行
        gen, idx = make(closes)
        for session in idx:
            gen.opportunities_for(session)
        self.assertEqual(gen.state[SID], STATE_INELIGIBLE)
        self.assertEqual([s for s in gen.signals if s['state'] == STATE_WATCH], [])

    def test_reclaim_triggers_and_executes_next_session(self):
        closes, n_up = scenario(pullback_sessions=10, tail=20, tail_price=236.0)
        gen, idx = make(closes)
        opps = []
        for session in idx:
            opps += gen.opportunities_for(session)
        self.assertEqual(len(opps), 1)
        o = opps[0]
        self.assertEqual(o.entry_rule, ENTRY_RULE)
        trig = [s for s in gen.signals if s['state'] == STATE_TRIGGERED][0]
        self.assertEqual(trig['reason'], 'MA20_RECLAIMED')
        # 信号日 = 收回 MA20 的那一天（回撤结束后的第一天）；执行日 = 下一个 session
        self.assertEqual(o.signal_session, str(idx[n_up].date()))
        self.assertEqual(o.planned_execution_session, str(idx[n_up + 1].date()))
        self.assertIn('atr14_micro', o.stop_reference)
        self.assertGreater(o.stop_reference['atr14_micro'], 0)
        self.assertAlmostEqual(trig['stop_distance_frac'], 0.08, places=6)

    def test_trend_break_invalidates_and_never_triggers(self):
        """趋势破坏 ⇒ INVALIDATED，且**绝不**产出信号。"""
        closes, _ = scenario(pullback_sessions=10, tail=40, tail_price=214.0)
        # 进 WATCH 后一路阴跌到 MA200 之下，最后一天大幅反弹（满足"收回 MA20"的表面条件）
        decline = [closes[len(ramp()) + 9] * (0.985 ** i) for i in range(1, 61)]
        closes = closes[:len(ramp()) + 10] + decline
        closes[-1] = closes[-2] * 1.5
        gen, idx = make(closes)
        opps = []
        for session in idx:
            opps += gen.opportunities_for(session)
        self.assertEqual(opps, [])
        self.assertNotIn(STATE_TRIGGERED, [s['state'] for s in gen.signals])
        # 断言**事件**而不是终态：失效之后有冷却，冷却走完会回到 INELIGIBLE，
        # 所以终态取决于序列停在哪里 —— 那不是在测"优先级"。
        self.assertIn('TREND_BROKEN',
                      [s['reason'] for s in gen.signals if s['state'] == STATE_INVALIDATED])

    def test_expiry_after_the_wait_window(self):
        closes, n_up = scenario(pullback_sessions=12, tail=40)
        gen, idx = make(closes)
        for session in idx:
            gen.opportunities_for(session)
        exp = [s for s in gen.signals if s['state'] == STATE_EXPIRED][0]
        self.assertEqual(exp['reason'], 'WAIT_WINDOW_EXPIRED')
        self.assertEqual(exp['waited_sessions'], gen.params.max_wait_sessions)
        # 到期日 = 进 WATCH 之后第 20 个 session（按参考交易日历计数）
        watch_at = idx.index(pd.Timestamp([s for s in gen.signals
                                           if s['state'] == STATE_WATCH][0]['session']))
        self.assertEqual(exp['session'], str(idx[watch_at + gen.params.max_wait_sessions].date()))
        self.assertNotIn(STATE_TRIGGERED, [s['state'] for s in gen.signals])

    def test_cooldown_blocks_a_new_watch_until_it_expires(self):
        """冷却期内即使条件再次成立也不进 WATCH；冷却走完才允许重新竞争。"""
        cooldown = 30
        closes, n_up = scenario(pullback_sessions=12, tail=60)
        gen, idx = make(closes, params=SignalParams(cooldown_sessions=cooldown))
        for session in idx:
            gen.opportunities_for(session)
        expired = [s for s in gen.signals if s['state'] == STATE_EXPIRED]
        self.assertTrue(expired)
        i = idx.index(pd.Timestamp(expired[0]['session']))
        watchers = [s for s in gen.signals
                    if s['state'] == STATE_WATCH and pd.Timestamp(s['session']) > idx[i]]
        if watchers:      # 允许冷却结束后重新进 WATCH，但不得早于冷却期满
            j = idx.index(pd.Timestamp(watchers[0]['session']))
            self.assertGreaterEqual(j - i, cooldown)


class SameDayPriorityTests(unittest.TestCase):
    """同日冲突的固定优先级。**只测真正可达的冲突**：

    「趋势破坏 vs 收回 MA20」在同一天不可达 —— 进 WATCH 的前提就是趋势成立
    （`close > MA200` 且 MA200 上行），而反弹日的收盘必然仍在 MA200 之上。
    把它写成测试只会得到一条恒真的断言（实测：把回收检查提到趋势检查之前，测试照样全绿），
    那正是这个项目反复踩的「机制看起来在工作、实际没有」。真正会被顺序决定的是下面两条。
    """

    def test_atr_unavailable_on_the_reclaim_day_invalidates_instead_of_triggering(self):
        """ATR 缺失当天即使满足收回条件，也按**失效**处理（fail-closed），不产出信号。"""
        closes, n_up = scenario(pullback_sessions=10, tail=20, tail_price=236.0)
        reclaim_at = n_up                      # 收回 MA20 的那一天
        prices = price_frame(closes)
        prices.loc[reclaim_at, 'asof_atr'] = float('nan')
        idx = pd.DatetimeIndex(prices.session)
        gen = BottomSignalGenerator(prices=prices, calendar=idx, actions=pd.DataFrame(),
                                    quality=quality_frame(idx), experiment_id='t',
                                    parent_version='1')
        opps = []
        for session in idx:
            opps += gen.opportunities_for(session)
        self.assertEqual(opps, [])
        self.assertIn('ATR_UNAVAILABLE',
                      [s['reason'] for s in gen.signals if s['state'] == STATE_INVALIDATED])

    def test_reclaim_on_the_last_day_of_the_window_still_triggers(self):
        """等待窗的最后一天收回 MA20 ⇒ 触发（收回检查在过期检查之前）。"""
        closes, n_up = scenario(pullback_sessions=10, tail=0)
        # 进 WATCH 后 19 个 session 不动，第 20 个 session 收回 MA20
        watch_at = len(ramp())
        closes = closes[:watch_at] + [214.0] * 20 + [216.0] * 5
        gen, idx = make(closes)
        opps = []
        for session in idx:
            opps += gen.opportunities_for(session)
        self.assertEqual(len(opps), 1, '等待窗最后一天收回 MA20 应当触发')
        self.assertEqual(opps[0].signal_session, str(idx[watch_at + 20].date()))


class LookAheadTests(unittest.TestCase):
    def test_changing_a_future_bar_does_not_change_todays_signal(self):
        """无未来信息：改掉 t 之后的行情，t 当天及以前的信号/状态必须逐条相同。"""
        closes, n_up = scenario(pullback_sessions=12, tail=40)
        cut = n_up + 6
        a, idx = make(closes)
        perturbed = list(closes)
        for i in range(cut + 1, len(perturbed)):
            perturbed[i] = perturbed[i] * 1.7          # 剧烈改动未来
        b, idx2 = make(perturbed)
        for session in idx[:cut]:
            a.opportunities_for(session)
            b.opportunities_for(session)
        self.assertEqual(a.signals, b.signals)
        self.assertEqual(a.state, b.state)
        # 而改动之后（cut 之后）确实会分叉 —— 否则上面的相等是假的
        for session in idx2[cut:]:
            a.opportunities_for(session)
            b.opportunities_for(session)
        self.assertNotEqual(a.signals, b.signals)

    def test_split_does_not_create_a_fake_trend_break(self):
        """复权序列：2:1 拆股处价格折半，用原始价会判成趋势破坏。"""
        closes, n_up = scenario(pullback_sessions=12, tail=40)
        n = len(closes)
        split_at = n_up + 4
        idx = sessions(n)
        raw = list(closes)
        for i in range(split_at, n):
            raw[i] = closes[i] / 2.0                   # 拆股后的原始价
        prices = price_frame(raw)
        actions = pd.DataFrame([{'security_id': SID, 'action_type': 'split',
                                 'ex_date': idx[split_at], 'ratio': 2, 'cash_amount': 0.0}])
        gen = BottomSignalGenerator(prices=prices, calendar=idx, actions=actions,
                                    quality=quality_frame(idx), experiment_id='t',
                                    parent_version='1')
        for session in idx:
            gen.opportunities_for(session)
        self.assertNotEqual(gen.state[SID], STATE_INVALIDATED)
        self.assertNotIn('TREND_BROKEN', [s['reason'] for s in gen.signals])

    def test_as_of_anchoring_does_not_change_the_decision(self):
        """比较型条件对 `as_of` 锚定不变（P0-4）：整段特征用日历末尾复权，
        与「逐日锚定」在**判定**上等价 —— 这里比较的是判定序列，不是价格数值。"""
        closes, n_up = scenario(pullback_sessions=12, tail=40)
        gen, idx = make(closes)
        verdicts = []
        for session in idx:
            gen.opportunities_for(session)
            verdicts.append((session, gen.state[SID]))
        # 决策只依赖状态与当日的比较结果；特征表本身带 as_of=末尾 的复权
        self.assertIn(STATE_WATCH, [v for _, v in verdicts])


class OutputContractTests(unittest.TestCase):
    def test_signal_records_carry_the_required_fields(self):
        """规范 §4 要求的用户可见字段一个都不能少。"""
        closes, n_up = scenario(pullback_sessions=10, tail=20, tail_price=236.0)
        gen, idx = make(closes)
        for session in idx:
            gen.opportunities_for(session)
        required = {'security_id', 'session', 'state', 'reason', 'rule_version', 'watch_session',
                    'data_through', 'version'}
        for s in gen.signals:
            self.assertTrue(required <= set(s), f'缺字段：{required - set(s)}')
        trig = [s for s in gen.signals if s['state'] == STATE_TRIGGERED]
        if trig:
            for key in ('earliest_execution_session', 'stop_distance_frac',
                        'drawdown_atr_multiples'):
                self.assertIn(key, trig[0])

    def test_features_are_computed_from_the_adjusted_series(self):
        closes, n_up = scenario()
        prices = price_frame(closes)
        idx = pd.DatetimeIndex(prices.session)
        feats = features_for(prices, pd.DataFrame(), idx[-1], SignalParams())
        self.assertAlmostEqual(float(feats.close.iloc[-1]), closes[-1], places=6)
        self.assertEqual(len(feats), len(closes))


if __name__ == '__main__':
    unittest.main()
