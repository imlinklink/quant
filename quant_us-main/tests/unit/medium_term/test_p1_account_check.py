"""P1 固定期限退出与样本筛选的确定性测试。"""
import math
import unittest

import pandas as pd

from scripts.medium_term.action_coverage import audit_action_coverage, blocked_sessions
from scripts.medium_term.entry_risk import risk_sized_shares
from scripts.medium_term.exit_matrix import simulate_fixed_horizon_exits
from scripts.medium_term.p1_account_check import prepare_entries, build_matrix, _run_window
from unittest.mock import patch


def bars(periods=130, price=100.):
    sessions = pd.bdate_range('2025-01-02', periods=periods)
    return pd.DataFrame({'security_id': 'SEC-A', 'session': sessions,
                         'raw_open': price, 'raw_high': price + 1,
                         'raw_low': price - 1, 'raw_close': price})


def entry(frame, stop=90.):
    return {'security_id': 'SEC-A', 'entry_session': frame.session.iloc[0],
            'entry_price': float(frame.raw_open.iloc[0]), 'initial_stop': stop}


class FixedHorizonTests(unittest.TestCase):
    def test_all_horizons_resolve_in_one_pass_without_stop(self):
        frame = bars()
        results = simulate_fixed_horizon_exits(entry(frame), frame, (20, 40, 60, 90, 120))
        self.assertEqual(sorted(results), [20, 40, 60, 90, 120])
        for horizon in (20, 40, 60, 90, 120):
            res = results[horizon]
            self.assertEqual(res['exit_phase'], 'CLOSE')
            self.assertEqual(res['exit_reason'], 'TIME_EXIT')
            self.assertEqual(res['holding_sessions'], horizon)
            self.assertEqual(res['exit_session'], frame.session.iloc[horizon - 1])

    def test_stop_truncates_only_horizons_still_open(self):
        frame = bars()
        frame.loc[24, 'raw_low'] = 89  # 第 25 个交易日跌破 90
        results = simulate_fixed_horizon_exits(entry(frame), frame, (20, 40, 60, 90, 120))
        self.assertEqual(results[20]['exit_reason'], 'TIME_EXIT')
        self.assertEqual(results[20]['exit_session'], frame.session.iloc[19])
        for horizon in (40, 60, 90, 120):
            self.assertEqual(results[horizon]['exit_reason'], 'STOP')
            self.assertEqual(results[horizon]['holding_sessions'], 25)
            self.assertEqual(results[horizon]['exit_price'], 90)
            self.assertEqual(results[horizon]['exit_phase'], 'INTRADAY')

    def test_entry_day_stop_is_active_via_intraday_low(self):
        frame = bars()
        frame.loc[0, 'raw_low'] = 89  # 入场日盘中即跌破止损
        results = simulate_fixed_horizon_exits(entry(frame), frame, (20,))
        self.assertEqual(results[20]['exit_reason'], 'STOP')
        self.assertEqual(results[20]['exit_phase'], 'INTRADAY')
        self.assertEqual(results[20]['holding_sessions'], 1)
        self.assertEqual(results[20]['exit_price'], 90)

    def test_gap_below_stop_after_entry_fills_at_open(self):
        frame = bars()
        frame.loc[1, ['raw_open', 'raw_high', 'raw_low', 'raw_close']] = [85, 86, 84, 85]
        results = simulate_fixed_horizon_exits(entry(frame), frame, (20,))
        self.assertEqual(results[20]['exit_reason'], 'GAP_STOP')
        self.assertEqual(results[20]['exit_phase'], 'OPEN')
        self.assertEqual(results[20]['holding_sessions'], 2)
        self.assertEqual(results[20]['exit_price'], 85)


class PrepareEntriesTests(unittest.TestCase):
    def _inputs(self, entry_index=0, to_offset=119):
        frame = bars(periods=200)
        row = frame.iloc[entry_index]
        entries = pd.DataFrame([{'experiment': 'A', 'security_id': 'SEC-A', 'entry_id': 'E1',
                                 'entry_time': row.session.tz_localize('UTC') + pd.Timedelta(hours=14, minutes=30),
                                 'atr14': 1.0, 'scale_to_next': 1.0}])
        quality = pd.DataFrame([{'security_id': 'SEC-A', 'quality_status': 'verified',
                                 'from_session': frame.session.iloc[0],
                                 'to_session': frame.session.iloc[entry_index + to_offset]}])
        return entries, quality, frame

    def test_window_within_quality_interval_is_included(self):
        entries, quality, frame = self._inputs(to_offset=119)
        prepared, funnel = prepare_entries(entries, quality, frame)
        self.assertEqual(len(prepared), 1)
        self.assertGreater(prepared.initial_stop.iloc[0], 0)
        self.assertLess(prepared.initial_stop.iloc[0], prepared.entry_price.iloc[0])

    def test_window_beyond_quality_end_is_excluded_regardless_of_stop(self):
        entries, quality, frame = self._inputs(to_offset=118)  # 第 120 日超出质量截止
        prepared, funnel = prepare_entries(entries, quality, frame)
        self.assertTrue(prepared.empty)
        self.assertEqual(funnel['excluded'], {'WINDOW_BEYOND_QUALITY_END': 1})

    def test_stop_uses_wider_of_eight_percent_and_two_point_five_atr(self):
        entries, quality, frame = self._inputs()
        entries.loc[0, 'atr14'] = 0.5  # 2.5*ATR=1.25 < 8%*100=8 → 取 8%
        prepared, _ = prepare_entries(entries, quality, frame)
        self.assertAlmostEqual(prepared.entry_price.iloc[0] - prepared.initial_stop.iloc[0], 8.0)
        entries.loc[0, 'atr14'] = 5.0  # 2.5*ATR=12.5 > 8 → 取 12.5
        prepared, _ = prepare_entries(entries, quality, frame)
        self.assertAlmostEqual(prepared.entry_price.iloc[0] - prepared.initial_stop.iloc[0], 12.5)


class ActionCoverageTests(unittest.TestCase):
    def test_wrong_split_ratio_on_correct_date_is_blocked(self):
        raw, adj, ex_date = self._split_inputs()
        recorded = pd.DataFrame([{'security_id': 'SEC-A', 'ex_date': ex_date,
                                  'action_type': 'split', 'ratio': 3., 'cash_amount': 0.}])
        audit = audit_action_coverage(raw, adj, recorded, 'SEC-A')
        self.assertEqual(audit['mismatched_records'], [ex_date])
        self.assertEqual(blocked_sessions(audit), [pd.Timestamp(ex_date)])

    def test_missing_adjusted_price_does_not_pass_coverage(self):
        raw, adj, ex_date = self._split_inputs()
        recorded = pd.DataFrame(columns=['security_id', 'ex_date', 'action_type', 'ratio', 'cash_amount'])
        audit = audit_action_coverage(raw, adj.iloc[0:0], recorded, 'SEC-A')
        self.assertEqual(len(audit['missing_adjusted_dates']), len(raw))
        self.assertNotEqual(audit['verdict'], 'ok')

    def _split_inputs(self):
        sessions = pd.bdate_range('2025-01-02', periods=10)
        raw = pd.DataFrame({'session': sessions, 'close': [100.] * 4 + [50.] * 6})
        adj = pd.DataFrame({'session': sessions, 'close': [50.] * 10})  # 第 5 日 2:1 拆股
        ex_date = sessions[4].strftime('%Y-%m-%d')
        return raw, adj, ex_date

    def test_unexplained_factor_jump_blocks_interval(self):
        raw, adj, _ = self._split_inputs()
        recorded = pd.DataFrame(columns=['security_id', 'ex_date', 'action_type',
                                         'ratio', 'cash_amount'])
        audit = audit_action_coverage(raw, adj, recorded, 'SEC-A')
        self.assertEqual(audit['verdict'], 'unexplained_actions')
        self.assertEqual(len(audit['unexplained_dates']), 1)

    def test_recorded_split_explains_the_jump(self):
        raw, adj, ex_date = self._split_inputs()
        recorded = pd.DataFrame([{'security_id': 'SEC-A', 'ex_date': ex_date,
                                  'action_type': 'split', 'ratio': 2., 'cash_amount': 0.}])
        audit = audit_action_coverage(raw, adj, recorded, 'SEC-A')
        self.assertEqual(audit['verdict'], 'ok')
        self.assertEqual(audit['unexplained_dates'], [])
        self.assertEqual(blocked_sessions(audit), [])

    def test_non_positive_record_is_invalid(self):
        raw, adj, ex_date = self._split_inputs()
        recorded = pd.DataFrame([{'security_id': 'SEC-A', 'ex_date': ex_date,
                                  'action_type': 'split', 'ratio': 0., 'cash_amount': 0.}])
        audit = audit_action_coverage(raw, adj, recorded, 'SEC-A')
        self.assertEqual(audit['verdict'], 'unexplained_actions')
        self.assertEqual(audit['invalid_records'], [ex_date])


    def test_duplicate_same_day_action_is_blocked(self):
        raw, adj, ex_date = self._split_inputs()
        recorded = pd.DataFrame([
            {'security_id': 'SEC-A', 'ex_date': ex_date, 'action_type': 'split',
             'ratio': 2., 'cash_amount': 0.},
            {'security_id': 'SEC-A', 'ex_date': ex_date, 'action_type': 'split',
             'ratio': 2., 'cash_amount': 0.}])
        audit = audit_action_coverage(raw, adj, recorded, 'SEC-A')
        self.assertEqual(audit['verdict'], 'unexplained_actions')
        self.assertEqual(audit['duplicate_records'], [ex_date])
        self.assertEqual([str(d.date()) for d in blocked_sessions(audit)], [ex_date])


class SizingRuleTests(unittest.TestCase):
    def test_account_reselects_subset_and_keeps_common_start(self):
        frame = bars(periods=3)
        matrix = pd.DataFrame([{'entry_id': 'E', 'security_id': 'SEC-A',
            'entry_session': frame.session.iloc[1], 'exit_session': frame.session.iloc[2],
            'entry_price': 100., 'initial_stop': 90., 'exit_price': 100.,
            'exit_phase': 'CLOSE', 'exit_reason': 'TIME_EXIT', 'exit_method': 'H20',
            'holding_sessions': 20, 'portfolio_accepted': False, 'net_pnl_pct': -.002}])
        benchmark = pd.DataFrame({'session': frame.session, 'equity': 1.})
        with patch('scripts.medium_term.p1_account_check.build_qqq_benchmark',
                   return_value=benchmark):
            summary, equity, trades, rejected = _run_window(
                'subset', matrix, frame, None, None, None,
                (frame.session.iloc[0], frame.session.iloc[-1]), .01)
        self.assertEqual(summary.accepted_entries.iloc[0], 1)
        self.assertEqual(len(equity), 3)
        self.assertEqual(equity.equity.iloc[0], 100_000.)
        self.assertTrue(rejected.empty)

    def test_quality_end_censors_without_looking_at_later_stop(self):
        frame = bars(periods=130)
        frame.loc[10, ['raw_open', 'raw_high', 'raw_low', 'raw_close']] = [80., 81., 79., 80.]
        prepared = pd.DataFrame([{**entry(frame), 'entry_id': 'E', 'rank': 0}])
        actions = pd.DataFrame(columns=['security_id', 'ex_date', 'action_type'])
        matrix = build_matrix(prepared, frame, actions, {'SEC-A': frame.session.iloc[4]})
        self.assertEqual(len(matrix), 5)
        self.assertTrue(matrix.data_quality.eq('right_censored').all())
        self.assertTrue(matrix.net_pnl_pct.isna().all())
        self.assertTrue(matrix.exit_session.eq(frame.session.iloc[4]).all())

    def test_equal_weight_caps_each_position_at_twenty_percent(self):
        shares = risk_sized_shares(100., 92., 100_000., 100_000., risk_fraction=1.0,
                                   max_weight=.20, fee_rate=.001, allow_fractional=False)
        self.assertEqual(shares, math.floor(20_000 / 100))  # 20% 市值上限生效

    def test_one_percent_risk_under_deploys_at_eight_percent_stop(self):
        shares = risk_sized_shares(100., 92., 100_000., 100_000., risk_fraction=.01,
                                   max_weight=.20, fee_rate=.001, allow_fractional=False)
        self.assertEqual(shares, math.floor(12_500 / 100))  # 1%/8% = 12.5%


if __name__ == '__main__':
    unittest.main()
