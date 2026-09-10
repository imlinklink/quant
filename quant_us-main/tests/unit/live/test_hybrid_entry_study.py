"""日线阶段识别 + 四组对照实验的离线验证（合成数据，不依赖 Futu）。"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from mutifactor.strategies.daily_regime import (
    ACTIONABLE, annotate, classify_state, daily_indicators, state_series,
    trend_qualified,
)
from scripts.run_hybrid_entry_study import (
    arm_daily_only, build_report, confirmation_events, make_daily_gate,
    simulate_daily_exit, summarize,
)


def synthetic_daily(n=300, seed=3, fall_then_recover=True):
    """构造「先下跌、后企稳、再突破」的日线，确保能触发状态机。"""
    rng = np.random.default_rng(seed)
    if fall_then_recover:
        down = np.linspace(200, 120, n // 2)
        up = np.linspace(120, 160, n - n // 2)
        close = np.concatenate([down, up]) + rng.normal(0, 1.0, n)
    else:
        close = np.linspace(100, 200, n) + rng.normal(0, 0.5, n)
    return pd.DataFrame({
        'date': pd.date_range('2021-01-04', periods=n, freq='B'),
        'open': close - rng.normal(0, 0.3, n),
        'high': close + 1.2, 'low': close - 1.2, 'close': close,
        'volume': rng.uniform(1e6, 3e6, n),
    })


class IndicatorTests(unittest.TestCase):
    def test_columns_present(self):
        d = daily_indicators(synthetic_daily())
        for c in ('ma20', 'ma50', 'ma200', 'atr', 'hh10', 'll20',
                  'higher_low', 'ret5', 'vol_ratio', 'ma20_slope'):
            self.assertIn(c, d.columns)

    def test_no_lookahead_on_indicators(self):
        df = synthetic_daily(n=200)
        full = daily_indicators(df)
        cut = 150
        trunc = daily_indicators(df.iloc[:cut + 1])
        # 截断点的指标不应受未来数据影响（滚动窗口以当日结尾）
        for col in ('ma20', 'ma50', 'atr'):
            a, b = full[col].iloc[cut], trunc[col].iloc[cut]
            if np.isfinite(a) and np.isfinite(b):
                self.assertAlmostEqual(a, b, places=6, msg=f'{col} 受未来数据影响')

    def test_hh10_excludes_current_day(self):
        df = synthetic_daily(n=60)
        d = daily_indicators(df)
        # 当日高点高于此前 10 日最高时，hh10 仍应等于此前的最大值
        i = 55
        expect = df['high'].iloc[i - 10:i].max()
        self.assertAlmostEqual(d['hh10'].iloc[i], expect, places=6)


class StateMachineTests(unittest.TestCase):
    def test_falling_in_downtrend(self):
        df = synthetic_daily(n=200)
        d = daily_indicators(df)
        # 下跌段中段应判为 FALLING
        self.assertEqual(classify_state(d.iloc[80]), 'FALLING')

    def test_confirmed_after_recovery(self):
        df = synthetic_daily(n=300)
        s = state_series(daily_indicators(df))
        # 后段上行应至少出现过 REVERSING 或 CONFIRMED
        self.assertTrue(set(s.tail(80)) & set(ACTIONABLE), '企稳后未进入可买状态')

    def test_state_no_lookahead(self):
        """截断未来数据不得改变过去某日的状态。"""
        df = synthetic_daily(n=260)
        full = state_series(daily_indicators(df))
        cut = 200
        trunc = state_series(daily_indicators(df.iloc[:cut + 1]))
        for i in range(cut + 1):
            self.assertEqual(full.iloc[i], trunc.iloc[i], f'{i} 状态受未来数据影响')

    def test_never_confirmed_without_higher_low(self):
        # 一路单调下跌：不应出现 CONFIRMED
        n = 200
        close = np.linspace(200, 100, n)
        df = pd.DataFrame({
            'date': pd.date_range('2021-01-04', periods=n, freq='B'),
            'open': close, 'high': close + 0.5, 'low': close - 0.5, 'close': close,
            'volume': np.full(n, 1e6)})
        s = state_series(daily_indicators(df))
        self.assertNotIn('CONFIRMED', set(s))

    def test_trend_qualified(self):
        df = synthetic_daily(n=300, fall_then_recover=False)  # 单调上行
        d = daily_indicators(df)
        self.assertTrue(trend_qualified(d.iloc[-1]))
        self.assertFalse(trend_qualified(d.iloc[5]))  # 数据不足


class ConfirmationEventTests(unittest.TestCase):
    def test_only_first_entry(self):
        df = synthetic_daily(n=300)
        d = annotate(df)
        events = confirmation_events(d)
        # 事件之间不应相邻重复（只在首次进入时记录）
        for a, b in zip(events, events[1:]):
            self.assertNotEqual(a, b)

    def test_events_are_actionable(self):
        d = annotate(synthetic_daily(n=300))
        for i in confirmation_events(d):
            self.assertIn(d.iloc[i]['state'], ACTIONABLE)


class DailyExitTests(unittest.TestCase):
    def test_stop_hit(self):
        df = synthetic_daily(n=60)
        i = 10
        # 人为砸盘穿越止损
        df.loc[i + 3:, 'low'] = 50.0
        df.loc[i + 3:, 'close'] = 50.0
        info = simulate_daily_exit(df, i, entry_px=100.0, atr_entry=2.0)
        self.assertTrue(info['stop_hit'])
        self.assertLess(info['exit_px'], 100.0)
        self.assertLess(info['mae_pct'], 0)

    def test_max_hold(self):
        df = synthetic_daily(n=120)
        df['high'] = 1e6   # 保证不触发止损
        df['low'] = 1e6
        info = simulate_daily_exit(df, 10, entry_px=100.0, atr_entry=1.0, max_hold=5)
        self.assertEqual(info['exit_reason'], 'max_hold')
        self.assertEqual(info['hold_days'], 5)

    def test_mae_tracked(self):
        df = synthetic_daily(n=60)
        info = simulate_daily_exit(df, 5, entry_px=float(df['close'].iloc[5]), atr_entry=1e9)
        self.assertLessEqual(info['mae_pct'], 0.0)


class DailyGateTests(unittest.TestCase):
    def test_gate_uses_prior_day_only(self):
        """闸门必须用「严格早于当日」的日线状态，不能看当天（防前视）。"""
        d = annotate(synthetic_daily(n=300))
        gate = make_daily_gate(d)
        # 状态首次变为可买的那一天，闸门在该日**盘中**应为 False（当日尚未收盘）
        events = confirmation_events(d)
        self.assertTrue(events)
        ev_date = pd.Timestamp(d.iloc[events[0]]['date'])
        self.assertFalse(gate(ev_date))
        # 次日应放行
        self.assertTrue(gate(ev_date + pd.Timedelta(days=1)) or
                        gate(ev_date + pd.Timedelta(days=3)))

    def test_gate_false_before_any_data(self):
        d = annotate(synthetic_daily(n=100))
        gate = make_daily_gate(d)
        self.assertFalse(gate(pd.Timestamp('2020-01-01')))


class ArmTests(unittest.TestCase):
    def test_daily_only_produces_trades(self):
        d = annotate(synthetic_daily(n=300))
        events = confirmation_events(d)
        trades = arm_daily_only('US.SYN', d, events)
        self.assertTrue(trades)
        self.assertEqual(trades[0]['arm'], 'A3_daily_only')

    def test_no_events_no_trades(self):
        n = 200
        close = np.linspace(200, 100, n)   # 单调下跌，无确认
        df = pd.DataFrame({
            'date': pd.date_range('2021-01-04', periods=n, freq='B'),
            'open': close, 'high': close + 0.5, 'low': close - 0.5, 'close': close,
            'volume': np.full(n, 1e6)})
        d = annotate(df)
        self.assertEqual(arm_daily_only('US.SYN', d, confirmation_events(d)), [])


class Find15mEntryTests(unittest.TestCase):
    """A4 的入场搜索：**绝不能**用确认日当天的盘中 bar（那是前视）。"""

    @staticmethod
    def _m15_covering(start, days, price=100.0):
        import pandas as _pd
        ts = []
        day = _pd.Timestamp(start, tz='America/New_York')
        while len(ts) < days * 26:
            if day.weekday() < 5:
                b = day.replace(hour=9, minute=30)
                ts.extend(b + _pd.Timedelta(minutes=15 * k) for k in range(26))
            day = day + _pd.Timedelta(days=1)
        n = len(ts)
        return _pd.DataFrame({'time_key': ts, 'open': [price] * n,
                              'high': [price + .5] * n, 'low': [price - .5] * n,
                              'close': [price] * n, 'volume': [1e6] * n})

    def test_never_uses_confirmation_day(self):
        from unittest.mock import patch as _patch
        from scripts.run_hybrid_entry_study import find_15m_entry
        confirm = '2021-03-01'          # 周一
        m15 = self._m15_covering('2021-02-22', days=12)
        with _patch('scripts.run_dip_buy_backtest.analyze_score',
                    lambda w, p, t: {'signal': 'buy', 'score': 9}):
            hit = find_15m_entry(m15, pd.Timestamp(confirm), {}, 7)
        self.assertIsNotNone(hit)
        entry_time = pd.Timestamp(m15.iloc[hit[0]]['time_key'])
        # 入场时间必须严格晚于确认日（次日起）
        self.assertGreater(entry_time.date(), pd.Timestamp(confirm).date(),
                           '入场落在了确认日当天 → 前视')

    def test_returns_none_when_no_signal(self):
        from unittest.mock import patch as _patch
        from scripts.run_hybrid_entry_study import find_15m_entry
        m15 = self._m15_covering('2021-02-22', days=6)
        with _patch('scripts.run_dip_buy_backtest.analyze_score',
                    lambda w, p, t: {'signal': 'none', 'score': 0}):
            self.assertIsNone(find_15m_entry(m15, pd.Timestamp('2021-03-01'), {}, 7))

    def test_none_on_empty(self):
        from scripts.run_hybrid_entry_study import find_15m_entry
        self.assertIsNone(find_15m_entry(pd.DataFrame(), pd.Timestamp('2021-03-01'), {}, 7))


class MatchedPairsTests(unittest.TestCase):
    def test_pairs_reference_next_open(self):
        """匹配对里的参考价必须是**次日开盘**，与 A3 的入场口径一致。"""
        from unittest.mock import patch as _patch
        from scripts.run_hybrid_entry_study import matched_pairs
        d = annotate(synthetic_daily(n=300))
        events = confirmation_events(d)
        self.assertTrue(events)
        i = events[0]
        m15 = Find15mEntryTests._m15_covering('2021-01-04', days=400)
        with _patch('scripts.run_dip_buy_backtest.analyze_score',
                    lambda w, p, t: {'signal': 'buy', 'score': 9}):
            pairs = matched_pairs(d, events, m15, {}, 7)
        self.assertTrue(pairs)
        self.assertAlmostEqual(pairs[0]['ref_entry_px'],
                               float(d.iloc[i + 1]['open']), places=6)


class SummaryTests(unittest.TestCase):
    def test_summary_metrics(self):
        trades = [
            {'stock': 'A', 'arm': 'x', 'entry_date': '2021-01-01', 'net_pnl_usd': 100.0,
             'open': False, 'stop_hit': False, 'mae_pct': -0.01, 'hold_days': 5},
            {'stock': 'A', 'arm': 'x', 'entry_date': '2021-01-10', 'net_pnl_usd': -50.0,
             'open': False, 'stop_hit': True, 'mae_pct': -0.05, 'hold_days': 3},
        ]
        m = summarize(trades)
        self.assertEqual(m['trades'], 2)
        self.assertAlmostEqual(m['win_rate'], 0.5)
        self.assertAlmostEqual(m['stop_rate'], 0.5)

    def test_empty_is_safe(self):
        self.assertEqual(summarize([])['trades'], 0)

    def test_report_handles_no_pairs(self):
        rep = build_report({'A3_daily_only': [], 'A4_daily_plus_15m': []}, [], 0)
        self.assertIn('无法判定', rep)
        self.assertIn('四组汇总', rep)


if __name__ == '__main__':
    unittest.main()
