"""每日基础池 + 调度 rank 回归：基础池去重/规范化、行情计算、数据不足降级。"""
import unittest

import pandas as pd

from scripts.live_trading.run_daily_selection import build_packet_from_bars, build_universe


def _bars(n=60, slope=0.01):
    closes = [100.0 * (1.0 + slope * i) for i in range(n)]
    return pd.DataFrame({
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

        fake_ctx = {
            'earnings': {'date': '2026-09-20', 'eps_forecast': '1.2'},
            'news': [
                {'title': '公司发布业绩', 'publisher': 'test',
                 'observed_at': '2026-09-08T10:00:00+00:00',
                 'published_at': '2026-09-07T00:00:00+00:00'},
            ],
        }
        with patch.object(sc, 'fetch_signal_context', return_value=fake_ctx):
            ev = sc.fetch_event_evidence('US.A')
        self.assertTrue(all(e.get('evidence_id') for e in ev))
        self.assertTrue(any(e['kind'] == 'filing' for e in ev))  # 财报
        self.assertTrue(any(e['kind'] == 'news' for e in ev))    # 新闻

    def test_build_packet_includes_external_events(self):
        events = [{'summary': '公司发布业绩', 'source': 'filing',
                   'published_at': '2026-09-03T00:00:00+00:00', 'kind': 'filing'}]
        p = build_packet_from_bars('US.A', _bars(), events=events,
                                   now='2026-09-04T10:00:00+00:00')
        # 行情快照 + 外部事件，至少 2 条
        self.assertGreaterEqual(len(p['events']), 2)
        self.assertTrue(any(e['kind'] == 'rule' for e in p['events']))      # 行情快照
        self.assertTrue(any(e['kind'] == 'filing' for e in p['events']))    # 外部事件


if __name__ == '__main__':
    unittest.main()
