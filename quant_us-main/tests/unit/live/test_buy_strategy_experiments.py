import unittest

import pandas as pd

from scripts.run_buy_strategy_experiments import summarize


class BuyStrategyExperimentTests(unittest.TestCase):
    def test_summarize_applies_portfolio_limit(self):
        d = pd.DataFrame([
            {'stock': f'US.{i}', 'entry_date': '2026-01-01', 'exit_date': '2026-01-10',
             'net_pnl_usd': 10.0, 'open': False} for i in range(4)])
        out = summarize({k: d for k in 'ABCD'}, max_positions=3)
        self.assertTrue((out['trades'] == 3).all())
        self.assertTrue((out['portfolio_rejected'] == 1).all())


if __name__ == '__main__':
    unittest.main()
