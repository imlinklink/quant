import unittest
import numpy as np
import pandas as pd
from scripts.exit_matrix import EXIT_IDS,run_exit_matrix,simulate_daily


def daily():
    dates=pd.bdate_range('2026-01-01',periods=60,tz='UTC');close=np.arange(100.,160.)
    return pd.DataFrame({'stock':'US.X','date':dates,'open':close,'high':close+1,
                         'low':close-.5,'close':close,'volume':1000})


class ExitMatrixTests(unittest.TestCase):
    def test_all_exit_and_cost_cells_exist(self):
        entries=pd.DataFrame([{'experiment':'C','setup_id':'s1','stock':'US.X',
          'entry_time':'2026-01-02T00:00:00Z','entry_price':101,'initial_stop':95,
          'portfolio_rank':1}])
        out=run_exit_matrix(entries,daily())
        self.assertEqual(len(out),48)
        self.assertEqual(set(out.exit_method),set(EXIT_IDS))
        self.assertEqual(set(out.cost_scenario),{.001,.002,.005,.01})
        self.assertEqual(set(out[out.exit_method=='E12'].data_quality),{'missing_intraday_bars'})

    def test_gap_stop_fills_at_open(self):
        b=daily();b.loc[1,['open','low','close']]=[90,89,91]
        row={'entry_time':'2026-01-02T00:00:00Z','entry_price':101,'initial_stop':95}
        result=simulate_daily(row,b,'E5')
        self.assertEqual(result['exit_price'],90)
        self.assertEqual(result['exit_reason'],'GAP_STOP')


if __name__=='__main__':unittest.main()
