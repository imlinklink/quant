import unittest

import pandas as pd

from scripts.medium_term.exit_matrix import (apply_five_position_limit, run_exit_matrix,
                                             simulate_exit)


def bars(periods=130, price=100.):
    sessions = pd.bdate_range('2025-01-02', periods=periods)
    return pd.DataFrame({'session': sessions, 'raw_open': price, 'raw_high': price + 1,
                         'raw_low': price - 1, 'raw_close': price,
                         'asof_atr': 2., 'weekly_ma20': 90.,
                         'week_complete': sessions.weekday == 4})


def entry(frame, stop=90.):
    return {'security_id': 'SEC-A', 'entry_session': frame.session.iloc[0],
            'entry_price': float(frame.raw_open.iloc[0]), 'initial_stop': stop}


class MediumExitTests(unittest.TestCase):
    def test_fixed_holds_exit_on_exact_60_90_120_session_close(self):
        frame = bars()
        for exit_id, expected in [('M1', 60), ('M2', 90), ('M3', 120)]:
            result = simulate_exit(entry(frame), frame, exit_id)
            self.assertEqual(result['holding_sessions'], expected)
            self.assertEqual(result['exit_session'], frame.session.iloc[expected - 1])
            self.assertEqual(result['exit_phase'], 'CLOSE')

    def test_hard_stop_is_active_before_minimum_observation(self):
        frame = bars()
        frame.loc[4, 'raw_low'] = 89
        result = simulate_exit(entry(frame), frame, 'M4')
        self.assertEqual(result['holding_sessions'], 5)
        self.assertEqual(result['exit_reason'], 'STOP')
        self.assertEqual(result['exit_price'], 90)

    def test_weekly_break_waits_until_20_sessions_then_exits_next_open(self):
        frame = bars()
        frame.loc[9, ['raw_open', 'raw_high', 'raw_low', 'raw_close',
                      'weekly_ma20', 'week_complete']] = [100, 101, 94, 95, 100, True]
        frame.loc[19, ['raw_open', 'raw_high', 'raw_low', 'raw_close',
                       'weekly_ma20', 'week_complete']] = [100, 101, 94, 95, 100, True]
        frame.loc[20, ['raw_open', 'raw_high', 'raw_low', 'raw_close']] = [96, 97, 95, 96]
        result = simulate_exit(entry(frame), frame, 'M4')
        self.assertEqual(result['holding_sessions'], 21)
        self.assertEqual(result['exit_reason'], 'WEEKLY_TREND_EXIT')
        self.assertEqual(result['exit_price'], 96)
        self.assertEqual(result['exit_phase'], 'OPEN')

    def test_m5_atr_stop_activates_after_day_20_and_is_checked_next_day(self):
        frame = bars()
        frame.loc[19, ['raw_high', 'raw_close']] = [120, 110]
        frame.loc[20, ['raw_open', 'raw_high', 'raw_low', 'raw_close']] = [114, 115, 112, 113]
        result = simulate_exit(entry(frame), frame, 'M5')
        self.assertEqual(result['holding_sessions'], 21)
        self.assertEqual(result['exit_reason'], 'STOP')
        self.assertEqual(result['exit_price'], 113)

    def test_data_end_is_right_censored(self):
        frame = bars(periods=30)
        result = simulate_exit(entry(frame), frame, 'M3')
        self.assertEqual(result['data_quality'], 'right_censored')
        self.assertIsNone(result['gross_pnl_pct'])
        self.assertEqual(result['unrealized_pnl_pct'], 0.)

    def test_split_rescales_shares_and_stop_without_false_exit(self):
        frame = bars(periods=70)
        split_day = frame.session.iloc[10]
        frame.loc[10:, ['raw_open', 'raw_high', 'raw_low', 'raw_close',
                        'asof_atr', 'weekly_ma20']] /= 2
        actions = pd.DataFrame([{'security_id': 'SEC-A', 'ex_date': split_day,
                                 'action_type': 'split', 'ratio': 2}])
        result = simulate_exit(entry(frame), frame, 'M1', actions=actions)
        self.assertEqual(result['exit_reason'], 'TIME_EXIT')
        self.assertAlmostEqual(result['gross_pnl_pct'], 0.)
        self.assertEqual(result['shares_at_exit'], 2.)


class PositionLimitTests(unittest.TestCase):
    def test_duplicate_and_sixth_position_are_rejected(self):
        rows = []
        for rank, security_id in enumerate(['A', 'B', 'C', 'D', 'E', 'F'], 1):
            rows.append({'security_id': security_id, 'entry_session': '2026-01-02',
                         'exit_session': '2026-02-02', 'exit_phase': 'CLOSE', 'rank': rank})
        rows.append({'security_id': 'A', 'entry_session': '2026-01-05',
                     'exit_session': '2026-02-05', 'exit_phase': 'CLOSE', 'rank': 1})
        result = apply_five_position_limit(pd.DataFrame(rows))
        self.assertEqual(result.portfolio_accepted.sum(), 5)
        self.assertEqual(result.loc[result.security_id.eq('F'), 'portfolio_reject_reason'].iloc[0],
                         'MAX_POSITIONS')
        self.assertEqual(result.iloc[-1].portfolio_reject_reason, 'DUPLICATE_ACTIVE_SECURITY')

    def test_close_exit_does_not_release_slot_for_same_day_open(self):
        frame = pd.DataFrame([
            {'security_id': str(i), 'entry_session': '2026-01-02',
             'exit_session': '2026-02-02', 'exit_phase': 'CLOSE', 'rank': i}
            for i in range(5)
        ] + [{'security_id': 'NEW', 'entry_session': '2026-02-02',
              'exit_session': '2026-03-02', 'exit_phase': 'CLOSE', 'rank': 1}])
        result = apply_five_position_limit(frame)
        self.assertEqual(result.loc[result.security_id.eq('NEW'), 'portfolio_reject_reason'].iloc[0],
                         'MAX_POSITIONS')

    def test_open_exit_releases_slot_for_same_day_open(self):
        frame = pd.DataFrame([
            {'security_id': str(i), 'entry_session': '2026-01-02',
             'exit_session': '2026-02-02', 'exit_phase': 'OPEN' if i == 0 else 'CLOSE',
             'rank': i} for i in range(5)
        ] + [{'security_id': 'NEW', 'entry_session': '2026-02-02',
              'exit_session': '2026-03-02', 'exit_phase': 'CLOSE', 'rank': 1}])
        result = apply_five_position_limit(frame)
        self.assertTrue(result.loc[result.security_id.eq('NEW'), 'portfolio_accepted'].iloc[0])


class MatrixIntegrationTests(unittest.TestCase):
    def test_candidates_run_all_exit_and_cost_cells_at_raw_open(self):
        a = bars().assign(security_id='A')
        b = bars(price=200.).assign(security_id='B')
        entries = pd.DataFrame([
            {'security_id': 'A', 'execution_session': a.session.iloc[0],
             'initial_stop': 90., 'rank': 1, 'selected': True, 'strategy': 'B2'},
            {'security_id': 'B', 'execution_session': b.session.iloc[0],
             'initial_stop': 180., 'rank': 2, 'selected': True, 'strategy': 'B2'},
            {'security_id': 'IGNORED', 'execution_session': b.session.iloc[0],
             'initial_stop': 1., 'rank': 3, 'selected': False, 'strategy': 'B2'},
        ])
        matrix = run_exit_matrix(entries, pd.concat([a, b]), costs=(.002,))
        self.assertEqual(len(matrix), 2 * 5)
        self.assertEqual(set(matrix.exit_method), {'M1', 'M2', 'M3', 'M4', 'M5'})
        self.assertTrue(matrix.portfolio_accepted.all())
        self.assertEqual(matrix.loc[matrix.security_id.eq('B'), 'entry_price'].unique().tolist(),
                         [200.])
        realized = matrix[matrix.gross_pnl_pct.notna()]
        self.assertTrue(((realized.gross_pnl_pct - .002) - realized.net_pnl_pct).abs().lt(1e-12).all())


if __name__ == '__main__':
    unittest.main()
