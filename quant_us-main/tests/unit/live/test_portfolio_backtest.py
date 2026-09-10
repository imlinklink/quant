import unittest

import pandas as pd

from scripts.portfolio_backtest import apply_portfolio_constraints


class PortfolioBacktestTests(unittest.TestCase):
    def test_global_position_limit_and_ranking(self):
        d = pd.DataFrame([
            {'stock': f'US.{c}', 'entry_date': '2026-01-01', 'exit_date': '2026-01-10',
             'portfolio_rank': rank} for c, rank in [('D', 4), ('A', 1), ('C', 3), ('B', 2)]])
        accepted, rejected = apply_portfolio_constraints(d, 3)
        self.assertEqual(set(accepted.stock), {'US.A', 'US.B', 'US.C'})
        self.assertEqual(rejected.iloc[0].portfolio_reject_reason, 'MAX_POSITIONS')


if __name__ == '__main__':
    unittest.main()
