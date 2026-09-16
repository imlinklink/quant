import unittest

import pandas as pd

from scripts.medium_term.etf_dual_momentum import generate_targets
from scripts.medium_term.momentum_features import (momentum_snapshot, point_in_time_momentum_snapshot,
                                                   rank_cross_section)
from scripts.medium_term.monthly_calendar import month_end_sessions, next_session


def prices(ids=('A', 'B'), periods=260, start='2025-01-02'):
    sessions = pd.bdate_range(start, periods=periods)
    rows = []
    for offset, security_id in enumerate(ids):
        for i, session in enumerate(sessions):
            rows.append({'security_id': security_id, 'session': session,
                         'asof_close': 100 + (offset + 1) * i})
    return pd.DataFrame(rows)


class CalendarTests(unittest.TestCase):
    def test_month_end_uses_last_observed_session_and_next_is_strict(self):
        sessions = pd.to_datetime(['2026-01-29', '2026-01-30', '2026-02-02', '2026-02-27'])
        self.assertEqual(list(month_end_sessions(sessions)),
                         [pd.Timestamp('2026-01-30'), pd.Timestamp('2026-02-27')])
        self.assertEqual(next_session(sessions, '2026-01-30'), pd.Timestamp('2026-02-02'))
        self.assertIsNone(next_session(sessions, '2026-02-27'))


class MomentumTests(unittest.TestCase):
    def test_boundaries_use_t_minus_126_and_t_minus_252_to_t_minus_21(self):
        data = prices(ids=('A',), periods=253)
        snap = momentum_snapshot(data, data.session.max()).iloc[0]
        values = data.asof_close.to_numpy(float)
        self.assertAlmostEqual(snap.mom_6m, values[-1] / values[-127] - 1)
        self.assertAlmostEqual(snap.mom_12_1, values[-22] / values[-253] - 1)

    def test_future_price_does_not_change_snapshot(self):
        data = prices(ids=('A',), periods=254)
        decision = data.session.sort_values().unique()[-2]
        before = momentum_snapshot(data, decision).iloc[0].mom_6m
        data.loc[data.session > decision, 'asof_close'] = 1_000_000
        after = momentum_snapshot(data, decision).iloc[0].mom_6m
        self.assertEqual(before, after)

    def test_incomplete_and_missing_decision_prices_are_audited(self):
        data = prices(ids=('A', 'B'), periods=253)
        decision = data.session.max()
        data = data[~((data.security_id == 'B') & (data.session == decision))]
        snap = momentum_snapshot(data, decision).set_index('security_id')
        self.assertTrue(snap.loc['A', 'eligible'])
        self.assertEqual(snap.loc['B', 'reject_reason'], 'DECISION_PRICE_MISSING')

    def test_rank_is_stable_for_ties(self):
        snap = pd.DataFrame([
            {'security_id': 'B', 'eligible': True, 'mom_6m': .2, 'mom_12_1': .1},
            {'security_id': 'A', 'eligible': True, 'mom_6m': .2, 'mom_12_1': .1},
        ])
        ranked = rank_cross_section(snap).set_index('security_id')
        self.assertEqual(ranked.loc['A', 'rank'], 1)
        self.assertEqual(ranked.loc['B', 'rank'], 2)


class ETFDualMomentumTests(unittest.TestCase):
    def _etf_prices(self, slopes, periods=260):
        sessions = pd.bdate_range('2025-01-02', periods=periods)
        return pd.DataFrame([
            {'security_id': security_id, 'session': session,
             'asof_close': 100 + slope * i}
            for security_id, slope in slopes.items()
            for i, session in enumerate(sessions)
        ])

    def test_selects_strongest_positive_asset_and_uses_t_plus_one(self):
        slopes = {'SPY': .1, 'QQQ': .3, 'IWM': .05, 'XLK': .2,
                  'SMH': .25, 'SOXX': .15, 'IGV': .12, 'SHY': .01}
        data = self._etf_prices(slopes)
        targets = generate_targets(data)
        valid = targets[targets.target_security_id.notna()]
        self.assertTrue((valid.target_security_id == 'QQQ').all())
        self.assertTrue((valid.execution_session > valid.decision_session).all())
        self.assertTrue(valid.iloc[0].rebalance_required)
        self.assertFalse(valid.iloc[1:].rebalance_required.any())

    def test_all_negative_risk_assets_switch_to_shy(self):
        slopes = {key: -.1 for key in ('SPY', 'QQQ', 'IWM', 'XLK', 'SMH', 'SOXX', 'IGV')}
        slopes['SHY'] = .01
        data = self._etf_prices(slopes)
        valid = generate_targets(data)
        valid = valid[valid.target_security_id.notna()]
        self.assertTrue((valid.target_security_id == 'SHY').all())

    def test_missing_risk_asset_blocks_month_instead_of_changing_universe(self):
        data = self._etf_prices({'SPY': .1, 'QQQ': .2, 'IWM': .1, 'XLK': .1,
                                 'SMH': .1, 'SOXX': .1, 'IGV': .1, 'SHY': .01})
        last = data.session.max()
        data = data[~((data.security_id == 'IGV') & (data.session == last))]
        row = generate_targets(data).iloc[-1]
        self.assertEqual(row.reject_reason, 'RISK_ASSET_LOOKBACK_INCOMPLETE')
        self.assertIsNone(row.target_security_id)


class PointInTimeMomentumTests(unittest.TestCase):
    def _bars(self, close, split_at=None):
        sessions = pd.bdate_range('2025-01-02', periods=260)
        rows = []
        for i, s in enumerate(sessions):
            c = close(i)
            rows.append({'security_id': 'SEC-A', 'session': s, 'open': c,
                         'high': c + 1., 'low': c - 1., 'close': c, 'volume': 1000.})
        bars = pd.DataFrame(rows)
        actions = pd.DataFrame(columns=['security_id', 'action_type', 'ex_date',
                                        'ratio', 'cash_amount'])
        if split_at is not None:
            actions = pd.DataFrame([{'security_id': 'SEC-A', 'action_type': 'split',
                                     'ex_date': str(sessions[split_at].date()),
                                     'ratio': 2., 'cash_amount': 0.}])
        return bars, actions

    def test_out_of_range_dividend_is_ignored(self):
        bars, _ = self._bars(lambda i: 100. + i)
        actions = pd.DataFrame([{'security_id': 'SEC-A', 'action_type': 'cash_dividend',
                                 'ex_date': '2019-06-03', 'ratio': 0., 'cash_amount': 1.0}])
        snap = point_in_time_momentum_snapshot(bars, actions, bars.session.max())
        self.assertTrue(snap.eligible.iloc[0])

    def test_split_adjustment_removes_fake_crash(self):
        bars, actions = self._bars(lambda i: 200. if i < 200 else 100., split_at=200)
        snap = point_in_time_momentum_snapshot(bars, actions, bars.session.max())
        # 复权后连续：mom_6m 应为 0（原始价会是 -50% 假崩）。
        self.assertAlmostEqual(snap.mom_6m.iloc[0], 0.0, places=6)


if __name__ == '__main__':
    unittest.main()
