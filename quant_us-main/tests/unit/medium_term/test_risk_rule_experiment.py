"""P1 同风险规则实验：网格、筛选、冻结与附加指标的确定性测试。"""
import unittest
from collections import Counter

import numpy as np
import pandas as pd

from scripts.medium_term.risk_rule_experiment import (
    RISK_FRACTIONS, _annual_stability, _concentration, _grid, _pareto_frontier,
    _run_config, _select_frozen)


class GridCompositionTests(unittest.TestCase):
    def test_grid_has_19_configs_with_single_p1_reference(self):
        configs = _grid()
        self.assertEqual(len(configs), 19)
        refs = [c for c in configs if c[4]]
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0][:4], ('P1', 20, '1.0pct', 0.01))
        self.assertEqual(Counter(c[0] for c in configs), {'P1': 7, 'B2': 6, 'B3': 6})

    def test_risk_fractions_map_to_account_initial_risk(self):
        self.assertEqual(dict(RISK_FRACTIONS), {'0.5pct': 0.005, '0.75pct': 0.0075,
                                                '1.0pct': 0.01})
        # 网格中非参照配置只用 0.5%/0.75%/1.0% 三档
        rfs = {c[3] for c in _grid()}
        self.assertEqual(rfs, {0.005, 0.0075, 0.01})


class AnnualStabilityTests(unittest.TestCase):
    def test_rising_equity_has_no_down_years(self):
        sessions = pd.bdate_range('2020-01-02', periods=520)
        equity = pd.DataFrame({'session': sessions,
                               'equity': np.linspace(100., 200., len(sessions))})
        s = _annual_stability(equity)
        self.assertGreaterEqual(s['n_years'], 2)
        self.assertGreater(s['worst_year_return'], 0.)
        self.assertEqual(s['down_years'], 0)
        self.assertEqual(s['down_year_ratio'], 0.0)

    def test_equity_with_a_down_year_is_detected(self):
        sessions = pd.bdate_range('2020-01-02', periods=520)
        values = []
        for s in sessions:
            # 2020 上涨，2021 下跌
            values.append(200. if s.year == 2020 else 200. - (s.dayofyear / 365.) * 50.)
        equity = pd.DataFrame({'session': sessions, 'equity': values})
        s = _annual_stability(equity)
        self.assertEqual(s['down_years'], 1)
        self.assertLess(s['worst_year_return'], 0.)


class ConcentrationTests(unittest.TestCase):
    def test_negative_total_gives_no_winner_share(self):
        trades = pd.DataFrame({
            'trade_id': [0, 0, 1, 1, 2, 2],
            'side': ['BUY', 'SELL', 'BUY', 'SELL', 'BUY', 'SELL'],
            'gross_notional': [1000., 1100., 1000., 900., 1000., 1000.],
            'fee': [1., 1., 1., 1., 1., 1.],
        })
        c = _concentration(trades)
        self.assertEqual(c['closed_trades'], 3)
        self.assertAlmostEqual(c['total_realized_pnl'], 98. - 102. - 2.)
        self.assertAlmostEqual(c['win_rate'], 1 / 3)
        self.assertIsNone(c['top1_winner_share'])

    def test_positive_total_reports_winner_share_and_sensitivity(self):
        trades = pd.DataFrame({
            'trade_id': [0, 0, 1, 1, 2, 2, 3, 3],
            'side': ['BUY', 'SELL', 'BUY', 'SELL', 'BUY', 'SELL', 'BUY', 'SELL'],
            'gross_notional': [1000., 1500., 1000., 1100., 1000., 1050., 1000., 1000.],
            'fee': [0., 0., 0., 0., 0., 0., 0., 0.],
        })
        c = _concentration(trades)
        # pnl: +500, +100, +50, 0 → total 650；top1 = 500/650
        self.assertAlmostEqual(c['total_realized_pnl'], 650.)
        self.assertAlmostEqual(c['top1_winner_share'], 500. / 650.)
        self.assertAlmostEqual(c['top5_winner_share'], 1.0)
        self.assertAlmostEqual(c['pnl_ex_top1'], 150.)
        self.assertAlmostEqual(c['win_rate'], 3 / 4)


class ParetoAndSelectionTests(unittest.TestCase):
    def test_pareto_drops_dominated_and_over_bound(self):
        summary = pd.DataFrame({
            'strategy': ['A', 'B', 'C', 'D'],
            'CAGR': [0.10, 0.12, 0.20, 0.08],
            'mdd_magnitude': [0.10, 0.10, 0.30, 0.10],
        })
        pf = _pareto_frontier(summary)
        self.assertEqual(set(pf.strategy), {'B'})

    def test_select_frozen_prefers_buffer_then_cagr(self):
        summary = pd.DataFrame({
            'strategy': ['A', 'B', 'C', 'D'],
            'holding_sessions': [60, 60, 60, 60],
            'risk_fraction_label': ['0.5pct', '0.75pct', '1.0pct', '1.0pct'],
            'CAGR': [0.20, 0.18, 0.16, 0.14],
            'mdd_magnitude': [0.17, 0.16, 0.13, 0.10],
            'is_reference': [False, False, False, False],
        })
        frozen = _select_frozen(summary)
        self.assertEqual(frozen['strategy'], 'A')

    def test_select_frozen_does_not_penalize_below_buffer(self):
        # MDD 低于 15% 不降权：A（0.13 MDD）比 B（0.16 MDD）更低且 CAGR 更高，应胜出。
        summary = pd.DataFrame({
            'strategy': ['A', 'B'],
            'holding_sessions': [60, 60],
            'risk_fraction_label': ['1.0pct', '1.0pct'],
            'CAGR': [0.15, 0.08],
            'mdd_magnitude': [0.13, 0.16],
            'is_reference': [False, False],
        })
        self.assertEqual(_select_frozen(summary)['strategy'], 'A')

    def test_select_frozen_prefers_under_buffer_over_near_boundary(self):
        # 贴近 20% 边界（>18%）降权：A（0.19 MDD）虽 CAGR 更高，仍被 B（0.12 MDD）取代。
        summary = pd.DataFrame({
            'strategy': ['A', 'B'],
            'holding_sessions': [90, 60],
            'risk_fraction_label': ['1.0pct', '1.0pct'],
            'CAGR': [0.20, 0.14],
            'mdd_magnitude': [0.19, 0.12],
            'is_reference': [False, False],
        })
        self.assertEqual(_select_frozen(summary)['strategy'], 'B')

    def test_select_frozen_returns_none_when_nothing_qualifies(self):
        summary = pd.DataFrame({
            'strategy': ['A', 'B'],
            'holding_sessions': [60, 90],
            'risk_fraction_label': ['1.0pct', '1.0pct'],
            'CAGR': [0.25, 0.22],
            'mdd_magnitude': [0.25, 0.28],
            'is_reference': [False, False],
        })
        self.assertIsNone(_select_frozen(summary))

    def test_select_frozen_excludes_reference_row(self):
        summary = pd.DataFrame({
            'strategy': ['P1', 'B2'],
            'holding_sessions': [20, 60],
            'risk_fraction_label': ['1.0pct', '1.0pct'],
            'CAGR': [0.30, 0.10],
            'mdd_magnitude': [0.12, 0.18],
            'is_reference': [True, False],
        })
        self.assertEqual(_select_frozen(summary)['strategy'], 'B2')


class RunConfigSmokeTests(unittest.TestCase):
    def test_run_config_executes_one_grid_cell(self):
        sessions = pd.bdate_range('2020-01-02', periods=60)
        prices = pd.DataFrame({
            'security_id': 'SEC-A', 'session': sessions,
            'raw_open': 100., 'raw_close': 100.,
        })
        matrix = pd.DataFrame([{
            'security_id': 'SEC-A', 'entry_session': sessions[10], 'entry_price': 100.,
            'initial_stop': 90., 'exit_session': sessions[40], 'exit_price': 100.,
            'exit_phase': 'CLOSE', 'exit_reason': 'TIME_EXIT', 'portfolio_accepted': True,
        }])
        benchmark = pd.DataFrame({'session': sessions, 'equity': 100., 'initial_equity': 100.})
        row, equity, trades, rejected = _run_config(
            'P1', 60, '1.0pct', 0.01, matrix, prices, None, benchmark,
            sessions[0], sessions[-1])
        self.assertEqual(row['strategy'], 'P1')
        self.assertEqual(row['accepted_entries'], 1)
        self.assertEqual(row['round_trips'], 1)
        self.assertEqual(row['risk_fraction'], 0.01)
        self.assertIn('max_drawdown', row)
        self.assertIn('mdd_magnitude', row)
        self.assertGreaterEqual(len(equity), 1)


if __name__ == '__main__':
    unittest.main()
