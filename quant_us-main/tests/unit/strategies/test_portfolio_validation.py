import unittest
import pandas as pd
from scripts.run_strategy_validation import run_portfolio


class PortfolioValidation(unittest.TestCase):
    def test_approval_delay_gap_cost_and_risk(self):
        bars=pd.DataFrame([dict(code='US.A',date=f'2026-03-10T{t}:00Z',open=o,high=h,low=l,close=c)
            for t,o,h,l,c in [('14:00',100,101,99,100),('14:15',100,100,94,94),('14:30',90,91,89,90)]])
        signals=pd.DataFrame([dict(code='US.A',strategy='dip_buy',signal_time='2026-03-10T13:45Z',
            approved_at='2026-03-10T13:59Z',price=100,initial_stop=95,risk_group='semis')])
        cfg=dict(per_trade=.0025,total=.015,group_limits={'semis':.0075},cost_per_share=.05)
        result=run_portfolio(bars,signals,cfg)
        self.assertEqual(len(result['trades']),1)
        self.assertEqual(result['trades'][0]['exit_time'],'2026-03-10T14:30:00+00:00')
        self.assertLess(result['trades'][0]['R'],-1)

    def test_missing_approval_never_fills(self):
        bars=pd.DataFrame([dict(code='US.A',date='2026-03-10T14:00Z',open=100,high=101,low=99,close=100)])
        signals=pd.DataFrame([dict(code='US.A',strategy='dip_buy',signal_time='2026-03-10T13:45Z',
            approved_at=None,price=100,initial_stop=95,risk_group='semis')])
        self.assertEqual(run_portfolio(bars,signals,{'group_limits':{'semis':.0075}})['open_positions'],0)
