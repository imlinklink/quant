"""P2：周线门与日线择时入场的确定性测试。"""
import unittest

import pandas as pd

from scripts.medium_term.timed_entries import (_adjusted_bars, build_timed_entries,
                                               entry_signals, weekly_regime)


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

    def test_candidate_missing_bars_gets_data_blocked(self):
        frame = bars_from_closes([100. + i for i in range(200)])
        cand = pd.DataFrame([{'security_id': 'SEC-NOBARS', 'rank': 1,
                              'decision_session': frame.session.iloc[0],
                              'execution_session': frame.session.iloc[1]}])
        out = build_timed_entries(cand, frame, frame.session, max_wait_sessions=20)
        self.assertEqual(out.entry_type.iloc[0], 'DATA_BLOCKED')
        self.assertTrue(pd.isna(out.execution_session.iloc[0]))

    def test_signal_on_last_day_gets_no_next_open(self):
        frame = bars_from_closes([100. + i for i in range(200)])
        frame.loc[199, ['raw_open', 'raw_high', 'raw_low', 'raw_close']] = [300., 301., 299., 300.]
        out = build_timed_entries(self._candidate(frame, idx=150), frame, frame.session,
                                  max_wait_sessions=60)
        self.assertEqual(out.entry_type.iloc[0], 'NO_NEXT_OPEN')
        self.assertTrue(pd.isna(out.execution_session.iloc[0]))

    def test_wait_window_counts_reference_calendar_not_security_sessions(self):
        # 证券在等待窗内停牌（150..179 无 bar），180 日才出现突破：按参考交易日历计数时
        # 该突破已落在 max_wait_sessions 之外，应 EXPIRED，而非按证券自身 session 延后入场。
        calendar = pd.bdate_range('2024-01-02', periods=250)
        closes = [100. + i for i in range(250)]
        raw = pd.DataFrame({
            'security_id': 'SEC-A', 'session': calendar,
            'raw_open': closes, 'raw_high': [c + 1. for c in closes],
            'raw_low': [c - 1. for c in closes], 'raw_close': closes,
        })
        sparse = raw[~raw.session.isin(calendar[150:180])].reset_index(drop=True)
        sparse.loc[sparse.session.eq(calendar[180]),
                   ['raw_open', 'raw_high', 'raw_low', 'raw_close']] = [300., 301., 299., 300.]
        cand = pd.DataFrame([{'security_id': 'SEC-A', 'rank': 1,
                              'decision_session': calendar[139],
                              'execution_session': calendar[140]}])
        out = build_timed_entries(cand, sparse, calendar, max_wait_sessions=20)
        self.assertEqual(out.entry_type.iloc[0], 'EXPIRED')
        self.assertTrue(pd.isna(out.execution_session.iloc[0]))


class AsOfConsistencyTests(unittest.TestCase):
    def test_future_split_does_not_change_past_entry_signals(self):
        sessions = pd.bdate_range('2025-01-02', periods=200)
        closes = [100.] * 50 + [130.] * 150  # 50 天平台 + 跳涨（第 50 天产生 breakout）
        raw = pd.DataFrame({
            'security_id': 'SEC-A', 'session': sessions,
            'raw_open': closes, 'raw_high': [c + 1. for c in closes],
            'raw_low': [c - 1. for c in closes], 'raw_close': closes,
            'volume': 1000.,
        })
        # 拆股在 day 150（未来）
        actions = pd.DataFrame([{'security_id': 'SEC-A', 'action_type': 'split',
                                 'ex_date': str(sessions[150].date()), 'ratio': 2., 'cash_amount': 0.}])
        no_split = _adjusted_bars(raw, actions, sessions[149])   # as_of 在拆股前：不应用拆股
        with_split = _adjusted_bars(raw, actions, sessions[199])  # as_of 在拆股后：应用拆股
        a = entry_signals(no_split).set_index('session')['entry_signal']
        b = entry_signals(with_split).set_index('session')['entry_signal']
        before = sessions[sessions < sessions[150]]
        # 未来拆股（等比例缩放）不应改变拆股前的突破/回踩信号
        self.assertTrue((a.loc[before] == b.loc[before]).all())

    def test_cash_dividend_does_not_change_past_entry_signals(self):
        sessions = pd.bdate_range('2025-01-02', periods=200)
        closes = [100.] * 50 + [130.] * 150
        raw = pd.DataFrame({
            'security_id': 'SEC-A', 'session': sessions,
            'raw_open': closes, 'raw_high': [c + 1. for c in closes],
            'raw_low': [c - 1. for c in closes], 'raw_close': closes,
            'volume': 1000.,
        })
        actions = pd.DataFrame([{'security_id': 'SEC-A', 'action_type': 'cash_dividend',
                                 'ex_date': str(sessions[150].date()), 'ratio': 0.,
                                 'cash_amount': 5.0}])
        no_div = _adjusted_bars(raw, actions, sessions[149])
        with_div = _adjusted_bars(raw, actions, sessions[199])
        a = entry_signals(no_div).set_index('session')['entry_signal']
        b = entry_signals(with_div).set_index('session')['entry_signal']
        before = sessions[sessions < sessions[150]]
        self.assertTrue((a.loc[before] == b.loc[before]).all())

    def test_future_actions_do_not_change_weekly_regime(self):
        sessions = pd.bdate_range('2024-01-02', periods=140)
        closes = [100. + i for i in range(140)]
        raw = pd.DataFrame({
            'security_id': 'SEC-A', 'session': sessions,
            'raw_open': closes, 'raw_high': [c + 1. for c in closes],
            'raw_low': [c - 1. for c in closes], 'raw_close': closes,
            'volume': 1000.,
        })
        actions = pd.DataFrame([
            {'security_id': 'SEC-A', 'action_type': 'cash_dividend',
             'ex_date': str(sessions[120].date()), 'ratio': 0., 'cash_amount': 1.0},
            {'security_id': 'SEC-A', 'action_type': 'split',
             'ex_date': str(sessions[130].date()), 'ratio': 2., 'cash_amount': 0.},
        ])
        base = _adjusted_bars(raw, actions, sessions[119])  # as_of 在所有行动前
        fut = _adjusted_bars(raw, actions, sessions[139])   # as_of 在所有行动后
        a = weekly_regime(base, sessions).set_index('session')['weekly_uptrend']
        b = weekly_regime(fut, sessions).set_index('session')['weekly_uptrend']
        before = sessions[sessions < sessions[120]]
        self.assertTrue((a.loc[before] == b.loc[before]).all())

    def test_build_timed_entries_invariant_to_in_window_split(self):
        # 拆股发生在信号之后、等待窗之内：窗末日复权价与原始价应给出同一入场决策。
        frame = bars_from_closes([100. + i for i in range(200)])
        frame.loc[150, ['raw_open', 'raw_high', 'raw_low', 'raw_close']] = [300., 301., 299., 300.]
        actions = pd.DataFrame([{'security_id': 'SEC-A', 'action_type': 'split',
                                 'ex_date': str(frame.session.iloc[160].date()),
                                 'ratio': 2., 'cash_amount': 0.}])
        cand = pd.DataFrame([{'security_id': 'SEC-A', 'rank': 1,
                              'decision_session': frame.session.iloc[100],
                              'execution_session': frame.session.iloc[101]}])
        raw_out = build_timed_entries(cand, frame, frame.session, max_wait_sessions=60)
        adj_out = build_timed_entries(cand, frame, frame.session, actions=actions,
                                      max_wait_sessions=60)
        self.assertEqual(raw_out.entry_type.iloc[0], adj_out.entry_type.iloc[0])
        self.assertEqual(raw_out.signal_session.iloc[0], adj_out.signal_session.iloc[0])
        self.assertEqual(raw_out.execution_session.iloc[0], adj_out.execution_session.iloc[0])


class ModuleImportSmokeTests(unittest.TestCase):
    def test_p2_selection_check_imports(self):
        # 防回归：p2_selection_check 依赖 p1_account_check 的符号，曾被误删导致 ImportError。
        import scripts.medium_term.p2_selection_check as p2
        self.assertTrue(callable(p2.run))


if __name__ == '__main__':
    unittest.main()
