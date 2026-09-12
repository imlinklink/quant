import unittest

import pandas as pd

from scripts.data.audit_futu_survivor_provenance import audit_provenance


class ProvenanceAuditTests(unittest.TestCase):
    def fixture(self):
        return [
            pd.DataFrame([{'code':'US.X','security_id':'SEC-X','quality_status':'verified'}]),
            pd.DataFrame([{'code':'US.X','stock_id':123,'stock_type':'STOCK','listing_date':'2020-01-02'}]),
            pd.DataFrame([{'security_id':'SEC-X','source_record_id':'US.X','listed_at':'2020-01-02',
                           'asset_type':'stock','source_observed_at':''}]),
            pd.DataFrame([{'security_id':'SEC-X','symbol':'US.X','source_record_id':'US.X',
                           'valid_from':'2020-01-02'}]),
            pd.DataFrame([{'security_id':'SEC-X','date':'2020-01-02'}]),
            pd.DataFrame([{'security_id':'SEC-X','ex_date':'2020-01-02','source_observed_at':None}]),
            pd.DataFrame([{'code':'US.X','matched':1,'mismatched':0,'no_both_sides':0}]),
        ]

    def test_consistent_source_still_does_not_prove_historical_observation(self):
        result = audit_provenance(*self.fixture()).iloc[0]
        self.assertTrue(result.source_consistent)
        self.assertTrue(result.master_observed_at_missing)
        self.assertEqual(result.action_observed_at_missing, 1)

    def test_prelisting_bar_and_action_gap_are_reported(self):
        frames = self.fixture()
        frames[4].loc[0, 'date'] = '2019-12-31'
        frames[5].loc[0, 'ex_date'] = '2019-12-31'
        frames[6].loc[0, 'no_both_sides'] = 1
        result = audit_provenance(*frames).iloc[0]
        self.assertFalse(result.source_consistent)
        self.assertIn('LISTING_DATE_CONFLICT', result.issues)
        self.assertIn('ACTION_PRICE_CHECK_INCOMPLETE', result.issues)

    def test_future_action_without_bars_is_not_a_gap_in_history(self):
        frames = self.fixture()
        frames[5].loc[0, 'ex_date'] = '2020-01-09'
        frames[6].loc[0, 'no_both_sides'] = 1
        result = audit_provenance(*frames).iloc[0]
        self.assertTrue(result.source_consistent)
        self.assertEqual(result.future_actions_without_bars, 1)


if __name__ == '__main__':
    unittest.main()
