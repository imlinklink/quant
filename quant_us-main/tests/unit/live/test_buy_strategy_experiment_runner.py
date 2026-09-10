import unittest
import pandas as pd
from scripts.buy_strategy_experiment_runner import apply_universe,build_abcd_entries


class ExperimentRunnerTests(unittest.TestCase):
    def test_builds_all_groups_and_applies_universe(self):
        setups=pd.DataFrame([{'setup_id':'s1','stock':'US.X','valid_from':'2026-01-02T00:00:00Z',
          'expires_at':'2026-01-05T23:00:00Z','next_open_time':'2026-01-02T14:30:00Z',
          'next_open_price':100,'initial_stop':95}])
        baseline=pd.DataFrame([{'signal_id':'a1','stock':'US.X','entry_time':'2026-01-02T14:30:00Z',
                               'entry_price':99,'initial_stop':94}])
        legacy=pd.DataFrame([{'signal_id':'b1','stock':'US.X','entry_time':'2026-01-02T15:00:00Z',
                             'entry_price':100}])
        timing=pd.DataFrame([{'setup_id':'s1','stock':'US.X','entry_time':'2026-01-02T15:15:00Z',
                             'entry_price':101,'triggered':True}])
        out=build_abcd_entries(baseline,setups,legacy,timing)
        self.assertEqual(set(out.experiment),set('ABCD'))
        uni=pd.DataFrame([{'universe_date':'2026-01-02','code':'US.X','eligible':True,
                          'quality':'good','reason':'ELIGIBLE'}])
        accepted,rejected=apply_universe(out,uni)
        self.assertEqual(len(accepted),4);self.assertTrue(rejected.empty)


if __name__=='__main__':unittest.main()
