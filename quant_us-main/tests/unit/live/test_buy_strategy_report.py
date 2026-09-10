import unittest
import pandas as pd
from scripts.buy_strategy_report import holm_adjust,metrics


class BuyStrategyReportTests(unittest.TestCase):
    def test_paired_d_minus_c_and_holm(self):
        rows=[]
        for i in range(4):
            for exp,pnl in [('C',.01),('D',.02)]:
                rows.append({'experiment':exp,'setup_id':f's{i}','stock':f'US.{i}',
                  'strategy':'reversal_confirmed','entry_time':f'2026-01-0{i+1}T14:30:00Z',
                  'exit_method':'E7','cost_scenario':.002,'net_pnl_pct':pnl,
                  'net_pnl_usd':pnl*5000,'mfe_pct':.03,'mae_pct':-.01,
                  'portfolio_accepted':True,'data_quality':'good'})
        out=metrics(pd.DataFrame(rows))
        self.assertEqual(out['d_minus_c'][0]['pairs'],4)
        self.assertAlmostEqual(out['d_minus_c'][0]['mean_diff'],.01)
        adjusted=holm_adjust([.04,.01,.03]);self.assertTrue(all(0<=x<=1 for x in adjusted))


if __name__=='__main__':unittest.main()
