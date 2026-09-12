import unittest

import pandas as pd

from scripts.data.build_research_quality_intervals import build_intervals


class ResearchQualityIntervalTests(unittest.TestCase):
    def test_builds_verified_and_rejected_intervals(self):
        audit=pd.DataFrame([
            {'code':'US.A','asset_type_audited':'stock','listing_date':'2010-01-01','verified':True},
            {'code':'US.B','asset_type_audited':'stock','listing_date':'1970-01-01','verified':False},
            {'code':'US.C','asset_type_audited':'etf','listing_date':'2012-01-01','verified':True},
        ])
        symbols=pd.DataFrame({'security_id':['A','B','C'],'symbol':['US.A','US.B','US.C'],
                              'valid_from':['2010-01-01']*3,'valid_to':[None]*3})
        out=build_intervals(audit,symbols,'2015-01-01','2026-08-31',unresolved=['A'])
        rows=out.set_index('security_id')
        self.assertEqual(rows.loc['A','reason'],'CORPORATE_ACTION_UNRESOLVED')
        self.assertEqual(rows.loc['B','reason'],'UNKNOWN_LISTING_DATE')
        self.assertEqual(rows.loc['C','reason'],'ASSET_TYPE_NOT_STOCK')
        self.assertTrue((out.quality_status=='unverified').all())

    def test_inferred_listing_date_is_distinct_from_missing_date(self):
        audit=pd.DataFrame([{'code':'US.A','asset_type_audited':'stock',
                            'listing_date':'2025-02-13','verified':False}])
        symbols=pd.DataFrame([{'security_id':'A','symbol':'US.A','valid_from':'2025-02-13','valid_to':None}])
        out=build_intervals(audit,symbols,'2015-01-01','2026-08-31')
        self.assertEqual(out.iloc[0].reason,'LISTING_DATE_UNVERIFIED')

    def test_verified_listing_after_window_start_moves_interval_start(self):
        audit=pd.DataFrame([{'code':'US.A','asset_type_audited':'stock',
                            'listing_date':'2020-06-01','verified':True}])
        symbols=pd.DataFrame([{'security_id':'A','symbol':'US.A','valid_from':'2020-06-01','valid_to':None}])
        out=build_intervals(audit,symbols,'2015-01-01','2026-08-31')
        self.assertEqual(out.iloc[0].from_session,'2020-06-01')
        self.assertEqual(out.iloc[0].quality_status,'verified')

    def test_symbol_mapping_must_be_unique(self):
        audit=pd.DataFrame([{'code':'US.A','asset_type_audited':'stock',
                            'listing_date':'2020-06-01','verified':True}])
        symbols=pd.DataFrame(columns=['security_id','symbol','valid_from','valid_to'])
        with self.assertRaisesRegex(ValueError,'SYMBOL_MAPPING_NOT_UNIQUE'):
            build_intervals(audit,symbols,'2015-01-01','2026-08-31')

    def test_incomplete_action_history_is_rejected(self):
        audit=pd.DataFrame([{'code':'US.A','asset_type_audited':'stock',
                            'listing_date':'2020-06-01','verified':True}])
        symbols=pd.DataFrame([{'security_id':'A','symbol':'US.A','valid_from':'2020-06-01','valid_to':None}])
        out=build_intervals(audit,symbols,'2015-01-01','2026-08-31',incomplete_actions=['A'])
        self.assertEqual(out.iloc[0].reason,'ACTION_HISTORY_INCOMPLETE')


if __name__=='__main__':unittest.main()
