import unittest
import pandas as pd
from scripts.historical_universe import build_point_in_time_universe


class HistoricalUniverseTests(unittest.TestCase):
    def test_listing_delisting_and_liquidity_are_point_in_time(self):
        master=pd.DataFrame([{'code':'US.A','listing_date':'2020-01-02','delisting_date':'',
                              'asset_type':'stock'},
                             {'code':'US.B','listing_date':'2020-01-03','delisting_date':'2020-01-03',
                              'asset_type':'stock'}])
        liquidity=pd.DataFrame([
            {'date':'2020-01-02','code':'US.A','previous_close':10,'adv20':6e6,'liquidity_as_of':'2020-01-01'},
            {'date':'2020-01-03','code':'US.A','previous_close':4,'adv20':6e6,'liquidity_as_of':'2020-01-02'},
            {'date':'2020-01-03','code':'US.B','previous_close':10,'adv20':6e6,'liquidity_as_of':'2020-01-02'}])
        out=build_point_in_time_universe(master,liquidity,
                                         pd.to_datetime(['2020-01-02','2020-01-03']))
        self.assertTrue(out[(out.code=='US.A')&(out.universe_date==pd.Timestamp('2020-01-02'))].eligible.iloc[0])
        self.assertFalse(out[(out.code=='US.A')&(out.universe_date==pd.Timestamp('2020-01-03'))].eligible.iloc[0])
        self.assertEqual(len(out[out.code=='US.B']),1)

    def test_rejects_same_day_or_legacy_liquidity(self):
        master=pd.DataFrame([{'code':'US.A','listing_date':'2020-01-01','delisting_date':'',
                              'asset_type':'stock'}])
        legacy=pd.DataFrame([{'date':'2020-01-02','code':'US.A','close':10,'dollar_volume':6e6}])
        with self.assertRaisesRegex(ValueError,'point-in-time'):
            build_point_in_time_universe(master,legacy,[pd.Timestamp('2020-01-02')])
        leaked=pd.DataFrame([{'date':'2020-01-02','code':'US.A','previous_close':10,
                              'adv20':6e6,'liquidity_as_of':'2020-01-02'}])
        with self.assertRaisesRegex(ValueError,'LOOKAHEAD'):
            build_point_in_time_universe(master,leaked,[pd.Timestamp('2020-01-02')])


if __name__=='__main__':unittest.main()
