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


class QualityGateTests(unittest.TestCase):
    """数据质量必须阻断 eligible（设计 §9 / 操作手册 §5.5/§12）。"""

    def _master(self):
        return pd.DataFrame([
            {'code':'US.OK','listing_date':'2020-01-01','delisting_date':'','asset_type':'stock'},
            {'code':'US.BADQ','listing_date':'2020-01-01','delisting_date':'','asset_type':'stock'},
            {'code':'US.NEWLY','listing_date':'2020-01-01','delisting_date':'','asset_type':'stock'},
            {'code':'US.ABSENT','listing_date':'2020-01-01','delisting_date':'','asset_type':'stock'},
            {'code':'US.CHEAP','listing_date':'2020-01-01','delisting_date':'','asset_type':'stock'},
        ])

    def _liq(self):
        # US.ABSENT 故意不给流动性记录：当日整行缺失
        return pd.DataFrame([
            {'date':'2020-01-02','code':'US.OK','previous_close':100,'adv20':1e8,
             'liquidity_as_of':'2019-12-31','quality':'good'},
            {'date':'2020-01-02','code':'US.BADQ','previous_close':100,'adv20':1e8,
             'liquidity_as_of':'2019-12-31','quality':'quality_fail'},
            {'date':'2020-01-02','code':'US.NEWLY','previous_close':None,'adv20':None,
             'liquidity_as_of':'2019-12-31','quality':'insufficient_history'},
            {'date':'2020-01-02','code':'US.CHEAP','previous_close':1,'adv20':1e8,
             'liquidity_as_of':'2019-12-31','quality':'good'},
        ])

    def setUp(self):
        self.out = build_point_in_time_universe(
            self._master(), self._liq(), pd.to_datetime(['2020-01-02']))

    def _row(self, code):
        return self.out[self.out.code == code].iloc[0]

    def test_quality_fail_blocks_eligibility(self):
        r = self._row('US.BADQ')
        self.assertEqual(r.quality, 'quality_fail')
        self.assertFalse(r.eligible, '质量失败仍可交易 → 违反设计 §9')
        self.assertEqual(r.reason, 'DATA_QUALITY_FAIL',
                         '质量失败被错标成流动性缺失')

    def test_absent_row_is_missing_liquidity(self):
        r = self._row('US.ABSENT')
        self.assertFalse(r.eligible)
        self.assertEqual(r.reason, 'MISSING_LIQUIDITY')

    def test_new_listing_is_insufficient_history(self):
        r = self._row('US.NEWLY')
        self.assertFalse(r.eligible)
        self.assertEqual(r.reason, 'INSUFFICIENT_LIQUIDITY_HISTORY')

    def test_good_and_liquid_is_eligible(self):
        r = self._row('US.OK')
        self.assertTrue(r.eligible)
        self.assertEqual(r.reason, 'ELIGIBLE')

    def test_price_gate_still_applies(self):
        r = self._row('US.CHEAP')
        self.assertFalse(r.eligible)
        self.assertEqual(r.reason, 'PRICE_TOO_LOW')

    def test_quality_fail_takes_precedence_over_price(self):
        """质量失败优先于价格原因，避免原因枚举失去区分度。"""
        m = self._master()
        liq = pd.DataFrame([
            {'date':'2020-01-02','code':'US.CHEAP','previous_close':1,'adv20':1e6,
             'liquidity_as_of':'2019-12-31','quality':'quality_fail'}])
        out = build_point_in_time_universe(m, liq, pd.to_datetime(['2020-01-02']))
        r = out[(out.code=='US.CHEAP')].iloc[0]
        self.assertFalse(r.eligible)
        self.assertEqual(r.reason, 'DATA_QUALITY_FAIL')


if __name__=='__main__':unittest.main()
