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


class ListingBasisTests(unittest.TestCase):
    """判据必须回答「**窗口内是否已上市**」，而不是「年份是否 > 1970」。

    原判据把两类完全不同的事判成同一件：**知道它在窗口开始前就已上市**（富途对老公司
    返回 `1970-01-01` 哨兵、管线用首根日线修正成数据起点，标
    `listed_on_or_before_history_start`）与**真的不知道**。实测（2026-09-20）：39 只
    宇宙里 18 只属于前者（XOM/KO/JNJ/PG/CAT/CVX/DIS/JPM/NEE/DUK/AMT/AVGO/COHR/SHW/
    PLD/AXTI/NBIS/SPY），它们因为**与窗口无关的理由**被整段排除。

    `listing_basis` 必须如实写出来：一条下界不能被下游读成"上市日"。
    """

    SYMBOLS = pd.DataFrame([{'security_id': 'A', 'symbol': 'US.A',
                             'valid_from': '2015-01-01', 'valid_to': None}])

    def _row(self, **kw):
        base = {'code': 'US.A', 'asset_type_audited': 'stock', 'verified': False,
                'listing_date': '1970-01-01', 'listing_placeholder': True,
                'listing_date_quality': 'listed_on_or_before_history_start'}
        base.update(kw)
        return pd.DataFrame([base])

    def _out(self, audit, start='2015-01-01', end='2026-08-31'):
        return build_intervals(audit, self.SYMBOLS, start, end).iloc[0]

    def test_lower_bound_before_window_is_accepted_and_labelled(self):
        r = self._out(self._row())
        self.assertEqual(r.quality_status, 'verified')
        self.assertEqual(r.listing_basis, 'listed_on_or_before_history_start')
        self.assertEqual(r.from_session, '2015-01-01')

    def test_lower_bound_after_window_start_is_rejected(self):
        # 下界本身晚于窗口起点 ⇒ 窗口前段是否上市**仍不知道**
        r = self._out(self._row(listing_date='2018-06-01'), start='2015-01-01')
        self.assertEqual(r.quality_status, 'unverified')
        self.assertEqual(r.reason, 'LISTING_LOWER_BOUND_AFTER_WINDOW')

    def test_real_date_is_labelled_reported(self):
        r = self._out(self._row(listing_date='2010-01-01', listing_placeholder=False,
                                listing_date_quality='reported_unverified', verified=True))
        self.assertEqual(r.quality_status, 'verified')
        self.assertEqual(r.listing_basis, 'reported')

    def test_listing_after_window_end_is_rejected(self):
        # 原实现会给出一条 from > to 的空区间，下游按 (from,to) 比对时**静默永不匹配**，
        # 看不出是"窗口内根本没上市"。现在是明确的拒绝理由。
        r = self._out(self._row(listing_date='2027-01-01', listing_placeholder=False,
                                listing_date_quality='reported_unverified', verified=True),
                      end='2026-08-31')
        self.assertEqual(r.quality_status, 'unverified')
        self.assertEqual(r.reason, 'LISTED_AFTER_WINDOW')

    def test_placeholder_without_the_label_is_still_unknown(self):
        # 没有 provenance 列（旧 schema）或标签不认识 ⇒ 行为必须与改动前一字不差
        r = self._out(pd.DataFrame([{'code': 'US.A', 'asset_type_audited': 'stock',
                                     'listing_date': '1970-01-01', 'verified': False}]))
        self.assertEqual(r.reason, 'UNKNOWN_LISTING_DATE')
        self.assertEqual(r.listing_basis, 'unknown')


if __name__=='__main__':unittest.main()
