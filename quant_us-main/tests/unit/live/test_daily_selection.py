"""每日基础池 + 调度 rank 回归：基础池去重/规范化、行情计算、数据不足降级。"""
import unittest

import pandas as pd

from scripts.live_trading.run_daily_selection import build_packet_from_bars, build_universe


def _bars(n=60, slope=0.01, end='2026-08-28'):
    """生成 n 根日 K，date 截止到 end（早于测试用的 now，保证已收盘）。"""
    dates = pd.date_range(pd.Timestamp(end) - pd.Timedelta(days=n * 2),
                          periods=n, freq='B')
    closes = [100.0 * (1.0 + slope * i) for i in range(n)]
    return pd.DataFrame({
        'date': dates,
        'close': closes,
        'high': [c * 1.02 for c in closes],
        'low': [c * 0.98 for c in closes],
    })


class DailySelectionContracts(unittest.TestCase):
    def test_build_universe_dedup_and_normalize(self):
        config = {
            'dip_buy': {'watch_list': ['US.MU', 'SOXL', 'US.MU']},
            'trend_breakout': {'watch_list': ['US.MU', 'YINN']},
        }
        self.assertEqual(build_universe(config), ['US.MU', 'US.SOXL', 'US.YINN'])

    def test_build_universe_skips_invalid(self):
        config = {'dip_buy': {'watch_list': ['US.MU', '', '  ']}}
        self.assertEqual(build_universe(config), ['US.MU'])

    def test_build_packet_from_bars_computes_quote(self):
        p = build_packet_from_bars('US.A', _bars(), now='2026-09-04T10:00:00+00:00')
        self.assertIsNotNone(p)
        q = p['quote']
        self.assertGreater(q['price'], 0)
        self.assertGreater(q['ret_20d'], 0)
        self.assertIsNotNone(q['atr'])
        self.assertEqual(q['trend'], 'above_ma50')

    def test_build_packet_from_bars_insufficient_returns_none(self):
        self.assertIsNone(build_packet_from_bars('US.A', _bars(n=1)))
        self.assertIsNone(build_packet_from_bars('US.A', None))

    def test_build_packet_from_bars_marks_missing(self):
        # 无事件/基本面 → data_quality 标记缺失，不伪造正常值
        p = build_packet_from_bars('US.A', _bars(), now='2026-09-04T10:00:00+00:00')
        self.assertFalse(p['data_quality']['ok'])

    def test_fetch_event_evidence(self):
        from unittest.mock import patch
        from scripts.live_trading import signal_context as sc

        fake_news = [
            {'title': '公司发布业绩', 'publisher': '富途·公告', 'kind': 'filing',
             'observed_at': '2026-09-08T10:00:00+00:00',
             'published_at': '2026-09-07T00:00:00+00:00'},
            {'title': '盘中资讯', 'publisher': '富途·资讯', 'kind': 'news',
             'observed_at': '2026-09-08T10:00:00+00:00',
             'published_at': '2026-09-08T09:00:00+00:00'},
        ]
        with patch.object(sc, 'fetch_futu_news', return_value=fake_news):
            ev = sc.fetch_event_evidence('US.A')
        self.assertTrue(all(e.get('evidence_id') for e in ev))
        self.assertTrue(any(e['kind'] == 'filing' for e in ev))  # 公告
        self.assertTrue(any(e['kind'] == 'news' for e in ev))    # 资讯

    def test_build_packet_includes_external_events(self):
        events = [{'summary': '公司发布业绩', 'source': 'filing',
                   'published_at': '2026-08-20T00:00:00+00:00', 'kind': 'filing'}]
        p = build_packet_from_bars('US.A', _bars(), events=events,
                                   now='2026-09-04T10:00:00+00:00')
        # 行情快照 + 外部事件，至少 2 条
        self.assertGreaterEqual(len(p['events']), 2)
        self.assertTrue(any(e['kind'] == 'rule' for e in p['events']))      # 行情快照
        self.assertTrue(any(e['kind'] == 'filing' for e in p['events']))    # 外部事件

    def test_observed_at_is_bar_end_not_run_time(self):
        # P1-1：observed_at 取最后一根已收盘 K 的收盘后时点，而非执行时间
        p = build_packet_from_bars('US.A', _bars(end='2026-08-28'),
                                   now='2026-09-04T10:00:00+00:00')
        q = p['quote']
        self.assertLess(q['observed_at'], '2026-09-04T10:00:00+00:00')  # 不是执行时间
        self.assertIn('bar_end', q)
        self.assertIn('data_cutoff_at', q)
        self.assertEqual(q['data_cutoff_at'], '2026-09-04T10:00:00+00:00')

    def test_intraday_drops_unclosed_today_bar(self):
        # P1-3 前置：盘中(09-04 10:00)时，09-04 当日 K 尚未收盘（22:00 才收盘）→ 不得作为基准
        # 构造含 09-04 当日 K（收盘价极高）的 bars
        import numpy as np
        closes = [100.0 + 0.1 * i for i in range(40)] + [9999.0]  # 最后一天假收盘
        dates = pd.date_range('2026-07-01', periods=41, freq='B')
        bars = pd.DataFrame({'date': dates, 'close': closes,
                             'high': [c * 1.01 for c in closes],
                             'low': [c * 0.99 for c in closes]})
        # 让最后一天恰为 09-04
        bars['date'] = bars['date'] + (pd.Timestamp('2026-09-04') - bars['date'].iloc[-1])
        p = build_packet_from_bars('US.A', bars, now='2026-09-04T10:00:00+00:00')
        self.assertIsNotNone(p)
        self.assertLess(float(p['quote']['price']), 9999.0)  # 没用未收盘的假价格


if __name__ == '__main__':
    unittest.main()
