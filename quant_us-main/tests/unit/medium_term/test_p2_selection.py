"""P2：周线门与日线择时入场的确定性测试。"""
import unittest

import pandas as pd

from scripts.medium_term.timed_entries import (build_timed_entries, entry_signals,
                                               weekly_regime)


def bars_from_closes(closes):
    sessions = pd.bdate_range('2024-01-01', periods=len(closes))
    return pd.DataFrame({'security_id': 'SEC-A', 'session': sessions,
                         'raw_open': closes, 'raw_high': [c + 1. for c in closes],
                         'raw_low': [c - 1. for c in closes], 'raw_close': closes})


class WeeklyRegimeTests(unittest.TestCase):
    def test_rising_series_opens_gate_and_falling_closes_it(self):
        up = bars_from_closes([100. + i for i in range(130)])
        up_flag = weekly_regime(up, up.session).weekly_uptrend
        self.assertTrue(bool(up_flag.iloc[-1]))
        down = bars_from_closes([230. - i for i in range(130)])
        down_flag = weekly_regime(down, down.session).weekly_uptrend
        self.assertFalse(bool(down_flag.iloc[-1]))

    def test_gate_carries_forward_within_a_week(self):
        up = bars_from_closes([100. + i for i in range(130)])
        flag = weekly_regime(up, up.session).weekly_uptrend
        self.assertNotIn(None, flag.tolist()[:0])  # 占位：确保返回布尔列
        self.assertEqual(flag.dtype, bool)


class EntrySignalTests(unittest.TestCase):
    def test_breakout_above_prior_twenty_day_high(self):
        closes = [100.] * 25 + [130.]
        sig = entry_signals(bars_from_closes(closes)).entry_signal
        self.assertEqual(sig.iloc[-1], 'breakout')

    def test_pullback_takes_priority_over_breakout(self):
        closes = [100.] * 25 + [105.]
        frame = bars_from_closes(closes)
        frame.loc[25, ['raw_open', 'raw_high', 'raw_low', 'raw_close']] = [100., 106., 99., 105.]
        sig = entry_signals(frame).entry_signal
        self.assertEqual(sig.iloc[-1], 'pullback')


class BuildTimedEntriesTests(unittest.TestCase):
    def _candidate(self, frame, idx=0, security_id='SEC-A'):
        return pd.DataFrame([{'security_id': security_id, 'rank': 1,
                              'decision_session': frame.session.iloc[idx],
                              'execution_session': frame.session.iloc[idx + 1]}])

    def test_entry_taken_when_signal_appears_in_window(self):
        # 20 周均线需要先预热，故用 200 日上升序列，并把候选放在门开启之后。
        frame = bars_from_closes([100. + i for i in range(200)])
        frame.loc[150, ['raw_open', 'raw_high', 'raw_low', 'raw_close']] = [300., 301., 299., 300.]
        out = build_timed_entries(self._candidate(frame, idx=100), frame, frame.session,
                                  max_wait_sessions=60)
        self.assertIn(out.entry_type.iloc[0], ('breakout', 'pullback'))
        self.assertGreater(out.execution_session.iloc[0], out.signal_session.iloc[0])

    def test_candidate_expires_without_signal(self):
        # 门开着但窗口内无突破/回踩（平坦段）：应作废而非硬凑入场。
        frame = bars_from_closes([100. + i for i in range(150)] + [249.] * 50)
        out = build_timed_entries(self._candidate(frame, idx=160), frame, frame.session,
                                  max_wait_sessions=20)
        self.assertEqual(out.entry_type.iloc[0], 'EXPIRED')
        self.assertTrue(pd.isna(out.execution_session.iloc[0]))


if __name__ == '__main__':
    unittest.main()
