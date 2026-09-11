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
        self.assertEqual(len(out),44)
        self.assertEqual(set(out.exit_method),set(EXIT_IDS))
        self.assertEqual(set(out.cost_scenario),{.001,.002,.005,.01})

    def test_gap_stop_fills_at_open(self):
        b=daily();b.loc[1,['open','low','close']]=[90,89,91]
        row={'entry_time':'2026-01-02T00:00:00Z','entry_price':101,'initial_stop':95}
        result=simulate_daily(row,b,'E5')
        self.assertEqual(result['exit_price'],90)
        self.assertEqual(result['exit_reason'],'GAP_STOP')


    def test_run_exit_matrix_handles_selected_groups(self):
        entries=pd.DataFrame([{'experiment':g,'setup_id':f's{g}','stock':'US.X',
          'entry_time':'2026-01-02T00:00:00Z','entry_price':101,'initial_stop':95,
          'portfolio_rank':1} for g in 'ABC'])
        out=run_exit_matrix(entries,daily())
        self.assertEqual(set(out.experiment),{'A','B','C'})
        self.assertEqual(len(out),3*11*4)


    def test_entry_day_session_is_included(self):
        # 实盘约定：entry_time 为成交日 09:30 ET(14:30 UTC)，日线 date 为该日 00:00。
        dates=pd.bdate_range('2026-01-01',periods=10,tz='UTC')
        b=pd.DataFrame({'stock':'US.X','date':dates,'open':100.,'high':101.,'low':99.,
                        'close':100.,'volume':1000})
        # 成交当日(1/5)开盘跳空低于止损，必须在当日以开盘价 GAP_STOP 出场。
        b.loc[b.date==pd.Timestamp('2026-01-05',tz='UTC'),['open','low','close']]=[90.,89.,91.]
        row={'entry_time':'2026-01-05T14:30:00Z','entry_price':100,'initial_stop':95}
        r=simulate_daily(row,b,'E5')
        self.assertEqual(r['exit_reason'],'GAP_STOP')
        self.assertEqual(r['exit_price'],90.)
        self.assertEqual(pd.Timestamp(r['exit_time']).date(),pd.Timestamp('2026-01-05').date())


if __name__=='__main__':unittest.main()
