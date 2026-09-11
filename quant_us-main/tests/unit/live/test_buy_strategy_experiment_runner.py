import unittest
import pandas as pd
from scripts.buy_strategy_experiment_runner import apply_universe,build_abcd_entries


class ExperimentRunnerTests(unittest.TestCase):
    def test_builds_all_groups_and_applies_universe(self):
        setups=pd.DataFrame([{'setup_id':'s1','stock':'US.X','setup_time':'2026-01-01T21:00:00Z',
          'next_open_time':'2026-01-02T14:30:00Z','next_open_price':100,'initial_stop':95,
          'signal_close':99,'atr14':3,'weekly_gate':True,'daily_confirmed':True,
          'llm_decision':'candidate'}])
        out=build_abcd_entries(setups)
        self.assertEqual(set(out.experiment),set('ABCD'))
        uni=pd.DataFrame([{'universe_date':'2026-01-02','code':'US.X','eligible':True,
                          'quality':'good','reason':'ELIGIBLE'}])
        accepted,rejected=apply_universe(out,uni)
        self.assertEqual(len(accepted),4);self.assertTrue(rejected.empty)

    def test_gap_and_same_bar_execution_are_rejected(self):
        base={'setup_id':'s','stock':'US.X','setup_time':'2026-01-01T21:00:00Z',
              'next_open_time':'2026-01-02T14:30:00Z','next_open_price':110,
              'initial_stop':95,'signal_close':100,'atr14':2,'weekly_gate':True,
              'daily_confirmed':True,'llm_decision':'candidate'}
        self.assertTrue(build_abcd_entries(pd.DataFrame([base])).empty)
        base['next_open_price']=100;base['next_open_time']=base['setup_time']
        with self.assertRaisesRegex(ValueError,'LOOKAHEAD'):build_abcd_entries(pd.DataFrame([base]))


if __name__=='__main__':unittest.main()
