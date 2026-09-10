"""dip_buy 回测的离线验证（合成 15M 数据 + 打桩评分，不依赖 Futu）。

重点验证：
  1. **无前视**——信号在当根收盘成立，入场必须用**下一根**开盘；
  2. 时段过滤、时间出场、出场引擎按预期工作；
  3. 与 donchian 的组合相关性计算正确。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_dip_buy_backtest import (
    PAGE_LIMIT, _exit_kwargs, atr_for_bars, build_report, combine_with_donchian,
    fetch_15m_range, merge_trades, replay_stock, session_allowed,
)

ET = 'America/New_York'
SENTINEL_CLOSE = 999.0  # 打桩评分只在最后一根收盘价等于它时返回 buy


def make_bars(n=100, price=100.0, start='2024-03-04'):
    """生成 15M bar，**只落在美东常规时段（09:30–16:00）**，跨日跳空。

    不能用连续 24h 的 date_range——那样绝大多数 bar 落在非交易时段，
    会被 session_filter 全部挡掉，导致评分函数根本不被调用。
    """
    times = []
    day = pd.Timestamp(start, tz=ET)
    while len(times) < n:
        if day.weekday() < 5:
            base = day.replace(hour=9, minute=30)
            times.extend(base + pd.Timedelta(minutes=15 * k) for k in range(26))
        day = day + pd.Timedelta(days=1)
    times = times[:n]
    return pd.DataFrame({
        'time_key': times,
        'open': np.full(n, price),
        'high': np.full(n, price + 0.5),
        'low': np.full(n, price - 0.5),
        'close': np.full(n, price),
        'volume': np.full(n, 1e6),
    })


def stub_score(buy_at_close=SENTINEL_CLOSE):
    """打桩 analyze_score：仅当窗口最后一根收盘 == buy_at_close 时返回 buy。"""
    def _f(window, price, threshold):
        last = float(window['close'].iloc[-1])
        if last == buy_at_close:
            return {'signal': 'buy', 'score': 9}
        return {'signal': 'none', 'score': 0}
    return _f


CFG = {'dip_buy': {'buy_threshold': 7, 'time_exit_bars': 3,
                   'session_filter': {'enabled': True, 'allow_regular': True,
                                      'allow_pre_market': False, 'allow_after_hours': False,
                                      'allow_overnight': False}},
       'chandelier': {'atr_period': 14, 'atr_threshold_pct': 0.1,
                      'profiles': {'dip_buy': {'fixed_stop_pct': 0.05, 'breakeven_pct': 0.02,
                                              'trailing_activate_pct': 0.04,
                                              'trailing_pullback_pct': 0.02,
                                              'atr_threshold_pct': 0.1,
                                              'atr_trailing_mult': 2.0,
                                              'trailing_enabled': True}}}}


class AtrTimeframeTests(unittest.TestCase):
    """ATR 必须按 60M（与实盘 atr_cache 一致）且无前视。"""

    def test_length_matches_bars(self):
        df = make_bars(n=100)
        self.assertEqual(len(atr_for_bars(df)), len(df))

    def test_no_lookahead(self):
        df = make_bars(n=120)
        a_full = atr_for_bars(df)
        cutoff = 80
        a_trunc = atr_for_bars(df.iloc[:cutoff + 1])
        # 截断后的前 cutoff+1 个值必须与全量一致（不受未来 bar 影响）
        for k in range(cutoff + 1):
            v1, v2 = a_full[k], a_trunc[k]
            if np.isnan(v1):
                self.assertTrue(np.isnan(v2))
            else:
                self.assertAlmostEqual(v1, v2, places=6)

    def test_atr_reflects_price_range(self):
        # 放大振幅后，ATR 应随之上升
        quiet = make_bars(n=200)
        wild = make_bars(n=200)
        wild['high'] = wild['close'] + 5.0
        wild['low'] = wild['close'] - 5.0
        self.assertGreater(np.nanmax(atr_for_bars(wild)), np.nanmax(atr_for_bars(quiet)))


class SessionFilterTests(unittest.TestCase):
    def test_regular_allowed(self):
        self.assertTrue(session_allowed(pd.Timestamp('2024-03-04 10:00', tz=ET), CFG))

    def test_pre_market_blocked_by_config(self):
        self.assertFalse(session_allowed(pd.Timestamp('2024-03-04 08:00', tz=ET), CFG))

    def test_after_hours_blocked_by_config(self):
        self.assertFalse(session_allowed(pd.Timestamp('2024-03-04 17:00', tz=ET), CFG))

    def test_after_hours_allowed_when_enabled(self):
        cfg = {'dip_buy': {'session_filter': {'enabled': True, 'allow_after_hours': True}}}
        self.assertTrue(session_allowed(pd.Timestamp('2024-03-04 17:00', tz=ET), cfg))

    def test_disabled_allows_all(self):
        cfg = {'dip_buy': {'session_filter': {'enabled': False}}}
        self.assertTrue(session_allowed(pd.Timestamp('2024-03-04 03:00', tz=ET), cfg))


class ExitConfigTests(unittest.TestCase):
    def test_reads_dip_buy_profile(self):
        kw = _exit_kwargs(CFG)
        self.assertAlmostEqual(kw['fixed_stop_pct'], 0.05)
        self.assertAlmostEqual(kw['trailing_activate_pct'], 0.04)
        self.assertTrue(kw['trailing_enabled'])


class NoLookaheadTests(unittest.TestCase):
    def _bars_with_signal(self, at=70):
        # at 必须 >= WINDOW_BARS-1（重放从第 60 根起）；末尾留出入场 bar
        df = make_bars(n=100)
        df.loc[at, 'close'] = SENTINEL_CLOSE      # 触发信号（当根收盘）
        df.loc[at + 1, 'open'] = 111.11           # 下一根开盘（应作为入场价）
        return df

    def test_entry_uses_next_bar_open(self):
        df = self._bars_with_signal(at=70)
        with patch('scripts.run_dip_buy_backtest.analyze_score', stub_score()):
            trades = replay_stock('US.SYN', df, CFG, threshold=7, time_exit_bars=3)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]['entry_price'], 111.11, places=2)

    def test_entry_time_is_next_bar(self):
        df = self._bars_with_signal(at=70)
        with patch('scripts.run_dip_buy_backtest.analyze_score', stub_score()):
            trades = replay_stock('US.SYN', df, CFG, threshold=7, time_exit_bars=3)
        expected = str(df.loc[71, 'time_key'])
        self.assertEqual(trades[0]['entry_date'], expected)

    def test_scoring_window_never_includes_future(self):
        """打桩函数记录每次收到的窗口末尾时间，必须单调且不超过当前 bar。"""
        seen = []

        def spy(window, price, threshold):
            seen.append(pd.Timestamp(window['time_key'].iloc[-1]))
            return {'signal': 'none', 'score': 0}
        df = make_bars(n=90)
        with patch('scripts.run_dip_buy_backtest.analyze_score', spy):
            replay_stock('US.SYN', df, CFG, threshold=7, time_exit_bars=3)
        self.assertTrue(all(seen[i] <= seen[i + 1] for i in range(len(seen) - 1)))
        # 窗口末尾最多到倒数第二根（最后一根留作入场 bar，不用于评分）
        self.assertLessEqual(max(seen), pd.Timestamp(df.iloc[-2]['time_key']))


class ExitBehaviourTests(unittest.TestCase):
    def test_time_exit_after_n_bars(self):
        df = make_bars(n=100)
        df.loc[70, 'close'] = SENTINEL_CLOSE
        with patch('scripts.run_dip_buy_backtest.analyze_score', stub_score()):
            trades = replay_stock('US.SYN', df, CFG, threshold=7, time_exit_bars=3)
        self.assertEqual(trades[0]['reason'], 'TIME_EXIT')

    def test_stop_loss_triggers(self):
        df = make_bars(n=100)
        df.loc[70, 'close'] = SENTINEL_CLOSE
        # 入场后一路跌破 5% 固定止损（入场价 100 → 止损 95）
        df.loc[71, 'open'] = 100.0
        df.loc[72:, 'low'] = 90.0
        df.loc[72:, 'close'] = 90.0
        with patch('scripts.run_dip_buy_backtest.analyze_score', stub_score()):
            trades = replay_stock('US.SYN', df, CFG, threshold=7, time_exit_bars=99)
        self.assertEqual(len(trades), 1)
        self.assertNotEqual(trades[0]['reason'], 'TIME_EXIT')
        self.assertLess(trades[0]['exit_price'], 96.0)

    def test_no_signal_no_trade(self):
        df = make_bars(n=100)  # 无 sentinel → 打桩永不返回 buy
        with patch('scripts.run_dip_buy_backtest.analyze_score', stub_score()):
            self.assertEqual(replay_stock('US.SYN', df, CFG, 7, 3), [])

    def test_short_data_returns_empty(self):
        df = make_bars(n=10)
        with patch('scripts.run_dip_buy_backtest.analyze_score', stub_score()):
            self.assertEqual(replay_stock('US.SYN', df, CFG, 7, 3), [])


class PaginationTests(unittest.TestCase):
    """富途单次上限 1000 根，必须逐页前进；且**限流错误不能被当成数据结束**。"""

    @staticmethod
    def _pager(pages, per=PAGE_LIMIT, start='2021-01-04', constant=False):
        calls = {'n': 0}
        t0 = pd.Timestamp(start, tz=ET)

        def _fetch(s, e):
            i = calls['n']
            calls['n'] += 1
            if i >= pages:
                return None
            offset = 0 if constant else i
            idx = pd.date_range(t0 + pd.Timedelta(minutes=15 * per * offset),
                                periods=per, freq='15min')
            return pd.DataFrame({'time_key': idx, 'open': 1.0, 'high': 1.0,
                                 'low': 1.0, 'close': 1.0, 'volume': 1.0})
        return _fetch, calls

    def test_pages_accumulate(self):
        fetch, calls = self._pager(pages=3)
        df, info = fetch_15m_range(fetch, '2021-01-04', '2030-01-01', retries=1, backoff=0)
        self.assertEqual(len(df), 3 * PAGE_LIMIT)
        self.assertFalse(info['truncated'])
        self.assertEqual(info['pages'], 3)

    def test_partial_last_page_stops_without_truncation(self):
        calls = {'n': 0}
        t0 = pd.Timestamp('2021-01-04', tz=ET)

        def _fetch(s, e):
            i = calls['n']
            calls['n'] += 1
            n = PAGE_LIMIT if i == 0 else 500          # 第二页不足一页 = 正常到末尾
            idx = pd.date_range(t0 + pd.Timedelta(minutes=15 * PAGE_LIMIT * i),
                                periods=n, freq='15min')
            return pd.DataFrame({'time_key': idx, 'open': 1.0, 'high': 1.0,
                                 'low': 1.0, 'close': 1.0, 'volume': 1.0})
        df, info = fetch_15m_range(_fetch, '2021-01-04', '2030-01-01', retries=1, backoff=0)
        self.assertEqual(len(df), PAGE_LIMIT + 500)
        self.assertFalse(info['truncated'])            # 自然结束 ≠ 截断
        self.assertEqual(calls['n'], 2)

    def test_api_error_marks_truncated_not_silent(self):
        """限流导致 API 报错：必须标记 truncated，不能当成「没有更多数据」。"""
        calls = {'n': 0}
        t0 = pd.Timestamp('2021-01-04', tz=ET)

        def _fetch(s, e):
            i = calls['n']
            calls['n'] += 1
            if i == 0:
                idx = pd.date_range(t0, periods=PAGE_LIMIT, freq='15min')
                return pd.DataFrame({'time_key': idx, 'open': 1.0, 'high': 1.0,
                                     'low': 1.0, 'close': 1.0, 'volume': 1.0})
            raise RuntimeError('频率限制')
        df, info = fetch_15m_range(_fetch, '2021-01-04', '2030-01-01', retries=2, backoff=0)
        self.assertEqual(len(df), PAGE_LIMIT)          # 只拿到第一页
        self.assertTrue(info['truncated'])
        self.assertIn('频率限制', info['last_error'])

    def test_error_then_retry_succeeds(self):
        calls = {'n': 0}
        t0 = pd.Timestamp('2021-01-04', tz=ET)

        def _fetch(s, e):
            calls['n'] += 1
            if calls['n'] == 2:                        # 第二次调用失败一次
                raise RuntimeError('transient')
            i = calls['n'] - 1 if calls['n'] < 2 else calls['n'] - 2
            idx = pd.date_range(t0 + pd.Timedelta(minutes=15 * PAGE_LIMIT * i),
                                periods=PAGE_LIMIT, freq='15min')
            return pd.DataFrame({'time_key': idx, 'open': 1.0, 'high': 1.0,
                                 'low': 1.0, 'close': 1.0, 'volume': 1.0})
        df, info = fetch_15m_range(_fetch, '2021-01-04', '2030-01-01', retries=3, backoff=0)
        self.assertFalse(info['truncated'])            # 重试成功 → 不算截断
        self.assertGreaterEqual(len(df), PAGE_LIMIT)

    def test_stops_when_cursor_does_not_advance(self):
        # 永远返回同一页 → 必须靠「未前进」保护终止，不能死循环
        fetch, calls = self._pager(pages=999, constant=True)
        df, info = fetch_15m_range(fetch, '2021-01-04', '2030-01-01',
                                   max_pages=50, retries=1, backoff=0)
        self.assertLessEqual(calls['n'], 3)
        self.assertLessEqual(len(df), PAGE_LIMIT)
        self.assertFalse(info['truncated'])

    def test_clamps_to_requested_range(self):
        fetch, _ = self._pager(pages=3)
        df, _info = fetch_15m_range(fetch, '2021-02-01', '2021-03-01', retries=1, backoff=0)
        self.assertTrue((df['time_key'] >= pd.Timestamp('2021-02-01', tz=ET)).all())
        self.assertTrue((df['time_key'] < pd.Timestamp('2021-03-02', tz=ET)).all())

    def test_empty_returns_none(self):
        df, info = fetch_15m_range(lambda s, e: None, '2021-01-04', '2021-02-01',
                                   retries=1, backoff=0)
        self.assertIsNone(df)
        self.assertFalse(info['truncated'])


class ProbeReportTests(unittest.TestCase):
    def test_probe_flags_truncated(self):
        from scripts.run_dip_buy_backtest import probe
        data = {'US.MU': pd.DataFrame({'time_key': pd.to_datetime(['2021-01-04'], utc=True)})}
        coverage = {'US.MU': {'truncated': True, 'bars': 1000},
                    'US.AXTI': {'truncated': False, 'bars': 0, 'last_error': '频率限制'}}
        md = probe(data, coverage)
        self.assertIn('被截断', md)
        self.assertIn('频率限制', md)
        self.assertIn('分批拉取', md)


class MergeTradesTests(unittest.TestCase):
    """分段拉取的合并：去重 + 排序 + 空输入安全。"""

    @staticmethod
    def _t(stock, entry, exit_, price=100.0, pnl=10.0):
        return {'stock': stock, 'entry_date': entry, 'exit_date': exit_,
                'entry_price': price, 'net_pnl_usd': pnl, 'open': False}

    def test_dedupes_overlap(self):
        seg1 = pd.DataFrame([self._t('US.MU', '2024-04-01 10:00', '2024-04-01 12:00'),
                             self._t('US.MU', '2024-04-02 10:00', '2024-04-02 12:00')])
        seg2 = pd.DataFrame([self._t('US.MU', '2024-04-02 10:00', '2024-04-02 12:00'),  # 重复
                             self._t('US.MU', '2024-04-05 10:00', '2024-04-05 12:00')])
        out = merge_trades(seg1, seg2)
        self.assertEqual(len(out), 3)
        self.assertTrue(out['entry_date'].is_monotonic_increasing)

    def test_distinct_prices_not_deduped(self):
        a = pd.DataFrame([self._t('US.MU', '2024-04-01 10:00', '2024-04-01 12:00', price=100.0)])
        b = pd.DataFrame([self._t('US.MU', '2024-04-01 10:00', '2024-04-01 12:00', price=101.0)])
        self.assertEqual(len(merge_trades(a, b)), 2)

    def test_handles_empty(self):
        seg = pd.DataFrame([self._t('US.MU', '2024-04-01 10:00', '2024-04-01 12:00')])
        self.assertEqual(len(merge_trades(pd.DataFrame(), seg)), 1)
        self.assertEqual(len(merge_trades(seg, pd.DataFrame())), 1)
        self.assertTrue(merge_trades(pd.DataFrame(), pd.DataFrame()).empty)


class CombineTests(unittest.TestCase):
    def _dip(self, values=None):
        values = values or [100, -50, 200, -80, 150, -30, 90, -10]
        return pd.DataFrame({
            'stock': ['US.SYN'] * len(values), 'variant': ['dip_buy'] * len(values),
            'entry_date': pd.date_range('2024-01-05', periods=len(values), freq='MS'),
            'exit_date': pd.date_range('2024-01-20', periods=len(values), freq='MS'),
            'net_pnl_usd': values, 'open': [False] * len(values),
        })

    def _dc_csv(self, tmp, values=None):
        values = values or [-100, 60, -200, 90, -150, 40, -90, 20]
        dc = pd.DataFrame({
            'channel': [55] * len(values), 'atr_mult': [2.0] * len(values),
            'exit_date': pd.date_range('2024-01-20', periods=len(values), freq='MS'),
            'net_pnl_usd': values, 'open': [False] * len(values),
        })
        p = Path(tmp) / 'dc.csv'
        dc.to_csv(p, index=False)
        return p

    def test_correlation_and_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            combo = combine_with_donchian(self._dip(), self._dc_csv(tmp))
        self.assertIsNone(combo.get('error'))
        self.assertEqual(combo['months'], 8)
        self.assertLess(combo['corr'], 0)  # 构造为反向
        self.assertAlmostEqual(combo['combined_total'],
                               combo['donchian_total'] + combo['dip_buy_total'])

    def test_mostly_zero_dip_is_rejected(self):
        """只有 1 个月有交易、其余为 0：旧实现会零填充造出假相关，这里必须拒绝。"""
        dip = self._dip(values=[100] + [0] * 7)   # 仅首月有值
        with tempfile.TemporaryDirectory() as tmp:
            combo = combine_with_donchian(self._dip(values=[100, -50, 200, -80, 150, -30, 90, -10],
                                                    ), self._dc_csv(tmp))
            combo_zero = combine_with_donchian(dip, self._dc_csv(tmp))
        self.assertIsNone(combo_zero.get('corr'))
        self.assertIn('重叠月份不足', combo_zero.get('error', ''))
        self.assertEqual(combo_zero['months'], 1)
        self.assertIsNotNone(combo.get('corr'))    # 全月有值时正常给出

    def test_missing_csv_returns_error(self):
        combo = combine_with_donchian(self._dip(), '/nonexistent/x.csv')
        self.assertIn('找不到', combo.get('error', ''))

    def test_report_renders_error_case(self):
        dip = self._dip(values=[100] + [0] * 7)
        with tempfile.TemporaryDirectory() as tmp:
            combo = combine_with_donchian(dip, self._dc_csv(tmp))
        trades = pd.DataFrame({
            'stock': ['US.SYN'], 'variant': ['dip_buy'],
            'entry_date': ['2024-01-05'], 'exit_date': ['2024-01-10'],
            'net_pnl_usd': [100.0], 'open': [False],
            'reason': ['TIME_EXIT'], 'holding_days': [1.0],
        })
        rep = build_report(trades, combo=combo)
        self.assertIn('无法给出组合结论', rep)


class ReportTests(unittest.TestCase):
    def test_sections_and_empty(self):
        self.assertIn('没有产生任何交易', build_report(pd.DataFrame()))
        trades = pd.DataFrame({
            'stock': ['US.SYN'] * 3, 'variant': ['dip_buy'] * 3,
            'entry_date': pd.date_range('2024-01-05', periods=3, freq='MS').astype(str),
            'exit_date': pd.date_range('2024-01-10', periods=3, freq='MS').astype(str),
            'net_pnl_usd': [100.0, -50.0, 200.0], 'open': [False] * 3,
            'reason': ['STOP_LOSS', 'TIME_EXIT', 'TIME_EXIT'],
            'holding_days': [1.5, 2.0, 3.0],
        })
        rep = build_report(trades)
        for kw in ('汇总', '分年期望', '出场原因分布', '怎么读'):
            self.assertIn(kw, rep)


if __name__ == '__main__':
    unittest.main()
