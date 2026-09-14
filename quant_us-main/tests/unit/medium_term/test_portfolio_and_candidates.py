import unittest

import pandas as pd

from scripts.medium_term.entry_risk import (attach_initial_stops, medium_initial_stop,
                                            risk_sized_shares)
from scripts.medium_term.portfolio_engine import (simulate_multi_asset_portfolio,
                                                  simulate_single_asset_rotation)
from scripts.medium_term.performance import performance_metrics
from scripts.medium_term.stock_cross_section import generate_monthly_candidates


class RotationPortfolioTests(unittest.TestCase):
    def test_rotation_uses_next_open_costs_and_daily_close_equity(self):
        prices = pd.DataFrame([
            {'security_id': asset, 'session': day, 'raw_open': op, 'raw_close': close}
            for day, values in [
                ('2026-01-02', {'A': (10, 11), 'B': (20, 20)}),
                ('2026-02-02', {'A': (12, 12), 'B': (20, 22)}),
            ] for asset, (op, close) in values.items()
        ])
        targets = pd.DataFrame([
            {'decision_session': '2026-01-01', 'execution_session': '2026-01-02',
             'target_security_id': 'A', 'rebalance_required': True},
            {'decision_session': '2026-01-30', 'execution_session': '2026-02-02',
             'target_security_id': 'B', 'rebalance_required': True},
        ])
        result = simulate_single_asset_rotation(prices, targets, initial_cash=1000,
                                                round_trip_cost=.002)
        self.assertEqual(list(result.trades.side), ['BUY', 'SELL', 'BUY'])
        first_shares = 1000 / 10.01
        self.assertAlmostEqual(result.equity.iloc[0].equity, first_shares * 11)
        sell_cash = first_shares * 12 * .999
        expected_shares = sell_cash / (20 * 1.001)
        self.assertAlmostEqual(result.equity.iloc[-1].equity, expected_shares * 22)

    def test_split_and_dividend_preserve_accounting(self):
        prices = pd.DataFrame([
            {'security_id': 'A', 'session': '2026-01-02', 'raw_open': 100, 'raw_close': 100},
            {'security_id': 'A', 'session': '2026-01-05', 'raw_open': 49, 'raw_close': 49},
        ])
        targets = pd.DataFrame([{'decision_session': '2026-01-01',
            'execution_session': '2026-01-02', 'target_security_id': 'A',
            'rebalance_required': True}])
        actions = pd.DataFrame([
            {'security_id': 'A', 'ex_date': '2026-01-05', 'action_type': 'split', 'ratio': 2},
            {'security_id': 'A', 'ex_date': '2026-01-05', 'action_type': 'cash_dividend',
             'cash_amount': 1},
        ])
        result = simulate_single_asset_rotation(prices, targets, initial_cash=1000,
                                                round_trip_cost=0, actions=actions)
        self.assertEqual(result.equity.iloc[-1].shares, 20)
        self.assertAlmostEqual(result.equity.iloc[-1].cash, 20)
        self.assertAlmostEqual(result.equity.iloc[-1].equity, 1000)

    def test_target_execution_must_be_after_decision(self):
        prices = pd.DataFrame([{'security_id': 'A', 'session': '2026-01-02',
                               'raw_open': 10, 'raw_close': 10}])
        targets = pd.DataFrame([{'decision_session': '2026-01-02',
            'execution_session': '2026-01-02', 'target_security_id': 'A'}])
        with self.assertRaisesRegex(ValueError, 'TARGET_EXECUTION_NOT_AFTER_DECISION'):
            simulate_single_asset_rotation(prices, targets)

    def test_warmup_prices_are_not_counted_as_portfolio_period(self):
        prices = pd.DataFrame([
            {'security_id': 'A', 'session': '2025-12-31', 'raw_open': 9, 'raw_close': 9},
            {'security_id': 'A', 'session': '2026-01-02', 'raw_open': 10, 'raw_close': 10},
        ])
        targets = pd.DataFrame([{'decision_session': '2026-01-01',
            'execution_session': '2026-01-02', 'target_security_id': 'A'}])
        result = simulate_single_asset_rotation(prices, targets)
        self.assertEqual(list(result.equity.session), [pd.Timestamp('2026-01-02')])


class EntryRiskTests(unittest.TestCase):
    def test_stop_uses_wider_of_eight_percent_and_two_point_five_atr(self):
        self.assertEqual(medium_initial_stop(100, 2), 92)
        self.assertEqual(medium_initial_stop(100, 5), 87.5)

    def test_position_size_obeys_risk_weight_and_cash_caps(self):
        self.assertEqual(risk_sized_shares(100, 90, 100_000, 100_000), 100)
        self.assertEqual(risk_sized_shares(100, 98, 100_000, 100_000), 200)
        self.assertEqual(risk_sized_shares(100, 90, 100_000, 5_000), 50)

    def test_stop_uses_signal_atr_scaled_to_execution_raw_price(self):
        candidates = pd.DataFrame([{'security_id': 'A', 'decision_session': '2026-01-02',
                                    'execution_session': '2026-01-05'}])
        features = pd.DataFrame([{'security_id': 'A', 'session': '2026-01-02',
                                  'asof_atr': 2., 'scale_to_next': 2.}])
        raw = pd.DataFrame([{'security_id': 'A', 'session': '2026-01-05',
                             'raw_open': 100., 'raw_close': 999.}])
        result = attach_initial_stops(candidates, features, raw).iloc[0]
        self.assertEqual(result.atr14_raw_at_execution, 4.)
        self.assertEqual(result.initial_stop, 90.)
        self.assertEqual(result.stop_feature_session, pd.Timestamp('2026-01-02'))


class MultiAssetPortfolioTests(unittest.TestCase):
    def test_dynamic_risk_sizing_and_exit_cash_flow(self):
        sessions = pd.bdate_range('2026-01-02', periods=2)
        prices = pd.DataFrame([
            {'security_id': security_id, 'session': day,
             'raw_open': 100, 'raw_close': close}
            for security_id, closes in {'A': (100, 110), 'B': (100, 100)}.items()
            for day, close in zip(sessions, closes)
        ])
        matrix = pd.DataFrame([
            {'security_id': 'A', 'entry_session': sessions[0], 'entry_price': 100,
             'initial_stop': 90, 'exit_session': sessions[1], 'exit_price': 110,
             'exit_phase': 'CLOSE', 'exit_reason': 'TIME_EXIT',
             'portfolio_accepted': True, 'rank': 1, 'exit_method': 'M1',
             'cost_scenario': 0.},
            {'security_id': 'B', 'entry_session': sessions[0], 'entry_price': 100,
             'initial_stop': 95, 'exit_session': sessions[1], 'exit_price': 100,
             'exit_phase': 'CLOSE', 'exit_reason': 'TIME_EXIT',
             'portfolio_accepted': True, 'rank': 2, 'exit_method': 'M1',
             'cost_scenario': 0.},
        ])
        result = simulate_multi_asset_portfolio(prices, matrix, round_trip_cost=0)
        buys = result.trades[result.trades.side == 'BUY'].set_index('security_id')
        self.assertEqual(buys.loc['A', 'shares'], 100)  # 1%风险约束
        self.assertEqual(buys.loc['B', 'shares'], 200)  # 20%市值约束
        self.assertAlmostEqual(result.equity.iloc[-1].equity, 101_000)
        self.assertEqual(result.equity.iloc[-1].position_count, 0)

    def test_close_exit_cash_is_not_available_to_same_day_open_entry(self):
        sessions = pd.bdate_range('2026-01-02', periods=2)
        prices = pd.DataFrame([
            {'security_id': security_id, 'session': day, 'raw_open': 100, 'raw_close': 100}
            for security_id in ('A', 'B') for day in sessions])
        matrix = pd.DataFrame([
            {'security_id': 'A', 'entry_session': sessions[0], 'entry_price': 100,
             'initial_stop': 99, 'exit_session': sessions[1], 'exit_price': 100,
             'exit_phase': 'CLOSE', 'exit_reason': 'TIME_EXIT',
             'portfolio_accepted': True, 'rank': 1, 'exit_method': 'M1', 'cost_scenario': 0.},
            {'security_id': 'B', 'entry_session': sessions[1], 'entry_price': 100,
             'initial_stop': 99, 'exit_session': sessions[1], 'exit_price': 100,
             'exit_phase': 'CLOSE', 'exit_reason': 'TIME_EXIT',
             'portfolio_accepted': True, 'rank': 1, 'exit_method': 'M1', 'cost_scenario': 0.},
        ])
        result = simulate_multi_asset_portfolio(prices, matrix, initial_cash=20_000,
                                                risk_fraction=1, max_weight=1,
                                                round_trip_cost=0)
        buys = result.trades[result.trades.side == 'BUY']
        self.assertEqual(list(buys.security_id), ['A'])
        self.assertEqual(result.rejected.iloc[0].reason, 'POSITION_SIZE_ZERO')


class PerformanceTests(unittest.TestCase):
    def test_initial_loss_counts_toward_drawdown(self):
        equity = pd.DataFrame({'session': pd.bdate_range('2025-01-02', periods=3),
                               'equity': [90., 95., 80.], 'initial_equity': 100.})
        metrics = performance_metrics(equity)
        self.assertAlmostEqual(metrics['max_drawdown'], -.2)
        self.assertIsNone(metrics['CAGR'])

    def test_missing_benchmark_session_is_not_silently_dropped(self):
        equity = pd.DataFrame({'session': pd.bdate_range('2025-01-02', periods=3),
                               'equity': [100., 101., 102.]})
        with self.assertRaisesRegex(ValueError, 'BENCHMARK_SESSIONS_MISMATCH'):
            performance_metrics(equity, equity.iloc[[0, 2]])

    def test_metrics_use_initial_capital_and_daily_high_water_mark(self):
        sessions = pd.bdate_range('2024-01-02', periods=253)
        values = [100, 120, 90] + [90 + i * (30 / 249) for i in range(250)]
        equity = pd.DataFrame({'session': sessions, 'equity': values,
                               'initial_equity': 100., 'gross_exposure': 1.,
                               'cumulative_turnover': 50.})
        benchmark = pd.DataFrame({'session': sessions, 'equity': [100.] * 253})
        metrics = performance_metrics(equity, benchmark)
        self.assertAlmostEqual(metrics['total_return'], .2)
        self.assertAlmostEqual(metrics['max_drawdown'], -.25)
        self.assertEqual(metrics['max_drawdown_start'], str(sessions[1].date()))
        self.assertEqual(metrics['max_drawdown_end'], str(sessions[2].date()))
        self.assertEqual(metrics['rolling_12m_windows'], 1)
        self.assertEqual(metrics['rolling_12m_win_rate_vs_benchmark'], 1.)

    def test_single_day_curve_does_not_fake_annualized_metrics(self):
        equity = pd.DataFrame([{'session': '2026-01-02', 'equity': 101,
                                'initial_equity': 100}])
        metrics = performance_metrics(equity)
        self.assertIsNone(metrics['CAGR'])
        self.assertIsNone(metrics['annualized_volatility'])


def cross_section_prices(periods=300):
    sessions = pd.bdate_range('2025-01-02', periods=periods)
    return pd.DataFrame([
        {'security_id': security_id, 'session': day, 'asof_close': 100 + slope * i}
        for security_id, slope in {'A': .3, 'B': .2, 'C': .1}.items()
        for i, day in enumerate(sessions)
    ])


class StockCrossSectionTests(unittest.TestCase):
    def test_selects_top_n_only_when_market_gate_is_open(self):
        prices = cross_section_prices()
        sessions = pd.DatetimeIndex(prices.session.unique())
        market = pd.DataFrame({'session': sessions, 'asof_close': 101., 'asof_ma200': 100.})
        result = generate_monthly_candidates(prices, market, top_n=2)
        valid_month = result[(result.decision_session == result.decision_session.max())]
        # 最后一月没有 T+1，取倒数第二个已经可执行月份。
        decision = sorted(result.decision_session.unique())[-2]
        valid_month = result[result.decision_session == decision]
        self.assertEqual(set(valid_month.loc[valid_month.selected, 'security_id']), {'A', 'B'})
        self.assertTrue((valid_month.execution_session > valid_month.decision_session).all())

    def test_closed_market_gate_keeps_rank_but_selects_nothing(self):
        prices = cross_section_prices()
        sessions = pd.DatetimeIndex(prices.session.unique())
        market = pd.DataFrame({'session': sessions, 'asof_close': 99., 'asof_ma200': 100.})
        result = generate_monthly_candidates(prices, market, top_n=2)
        eligible = result[result.eligible & result.execution_session.notna()]
        self.assertFalse(eligible.selected.any())
        self.assertTrue((eligible.selection_reason == 'MARKET_GATE_CLOSED').all())


if __name__ == '__main__':
    unittest.main()
