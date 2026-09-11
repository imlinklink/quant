import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.data.build_daily_liquidity import build_liquidity
from scripts.data.build_security_master_from_futu import build_from_basicinfo,codes_from_config,reconcile_listing_dates
from scripts.data.build_security_master import build_master
from scripts.data.download_market_history import download, fetch_pages
from scripts.data.normalize_market_history import normalize
from scripts.data.run_daily_pipeline import propagate_quality, run_pipeline
from scripts.data.trading_calendar import sessions
from scripts.data.validate_market_history import validate_daily


class FakeHistoryContext:
    def __init__(self, pages): self.pages=list(pages);self.calls=0
    def request_history_kline(self, *args, **kwargs):
        item=self.pages[self.calls];self.calls+=1;return item


class MarketDataPipelineTests(unittest.TestCase):
    def test_builds_pilot_master_from_config_and_futu_rows(self):
        cfg={'dip_buy':{'watch_list':['US.A','SOXL']},
             'pullback':{'sector_proxies':{'semis':'US.SOXX'}}}
        codes=codes_from_config(cfg,[])
        self.assertEqual(codes,['US.A','US.SOXL','US.SOXX','US.SPY'])
        basic=pd.DataFrame([
            {'code':'US.A','name':'A Corp','stock_type':'STOCK','list_time':'2020-01-02','lot_size':1},
            {'code':'US.SOXL','name':'SOXL ETF','stock_type':'ETF','list_time':'2010-03-11','lot_size':1},
            {'code':'US.SOXX','name':'SOXX ETF','stock_type':'ETF','list_time':'2001-07-10','lot_size':1},
            {'code':'US.SPY','name':'SPY ETF','stock_type':'ETF','list_time':'1993-01-29','lot_size':1}])
        out=build_from_basicinfo(basic,codes,as_of='2026-09-11')
        self.assertEqual(out.set_index('code').loc['US.SOXL','asset_type'],'leveraged_etf')
        self.assertEqual(out.set_index('code').loc['US.SPY','asset_type'],'etf')
        self.assertTrue((out.listing_date_quality=='reported_unverified').all())

    def test_first_daily_reconciles_placeholder_but_not_history_boundary(self):
        master=pd.DataFrame([
            {'code':'US.OLD','listing_date':'1970-01-01','listing_date_quality':'unknown'},
            {'code':'US.NEW','listing_date':'1970-01-01','listing_date_quality':'unknown'}])
        daily=pd.DataFrame([{'stock':'US.OLD','date':'2015-01-02'},
                            {'stock':'US.NEW','date':'2020-06-01'}])
        out=reconcile_listing_dates(master,daily,'2015-01-01').set_index('code')
        self.assertEqual(out.loc['US.OLD','listing_date_quality'],'listed_on_or_before_history_start')
        self.assertEqual(out.loc['US.NEW','listing_date'],'2020-06-01')
        self.assertEqual(out.loc['US.NEW','listing_date_quality'],'confirmed_by_first_daily')

    def test_liquidity_is_lagged_one_session(self):
        rows=[]
        for i in range(21):
            rows.append({'stock':'US.A','date':f'2026-01-{i+1:02d}','close':10+i,
                         'volume':100,'turnover':1000+i})
        out=build_liquidity(pd.DataFrame(rows),window=20,min_observations=15)
        row=out.iloc[-1]
        self.assertEqual(row.previous_close,29)
        self.assertEqual(row.liquidity_as_of,pd.Timestamp('2026-01-20'))
        self.assertAlmostEqual(row.adv20,sum(1000+i for i in range(20))/20)

    def test_rule_calendar_excludes_known_holidays(self):
        out=sessions('2026-01-01','2026-01-20')
        dates=set(pd.to_datetime(out.session_date).dt.date.astype(str))
        self.assertNotIn('2026-01-01',dates)
        self.assertNotIn('2026-01-19',dates)
        self.assertIn('2026-01-20',dates)

    def test_security_master_rejects_conflicting_core_fields(self):
        a=pd.DataFrame([{'code':'A','listing_date':'2020-01-01','delisting_date':'','asset_type':'stock'}])
        b=pd.DataFrame([{'code':'US.A','listing_date':'2021-01-01','delisting_date':'','asset_type':'stock'}])
        with self.assertRaisesRegex(ValueError,'来源冲突'):build_master([a,b])

    def test_pagination_and_checkpoint_reuse(self):
        page=pd.DataFrame([{'code':'US.A','time_key':'2026-01-02','open':1,'high':1,
                            'low':1,'close':1,'volume':1}])
        ctx=FakeHistoryContext([(0,page,b'next'),(0,page.assign(time_key='2026-01-03'),None)])
        frame,pages=fetch_pages(ctx,'US.A','2026-01-01','2026-12-31','DAY',autype='NONE',session='RTH',request_interval=0)
        self.assertEqual((len(frame),pages),(2,2))
        with tempfile.TemporaryDirectory() as tmp:
            ctx=FakeHistoryContext([(0,page,None)])
            types={'day':'DAY','autype':'NONE','autype_name':'none','session':'RTH'}
            state=download(ctx,['US.A'],'2026-01-01','2026-12-31',Path(tmp)/'raw',
                           Path(tmp)/'state.json',kinds=('day',),futu_types=types)
            self.assertEqual(len(state['completed']),1)
            calls=ctx.calls
            state2=download(ctx,['US.A'],'2026-01-01','2026-12-31',Path(tmp)/'raw',
                            Path(tmp)/'state.json',kinds=('day',),futu_types=types)
            self.assertEqual(ctx.calls,calls)
            self.assertEqual(state,state2)

    def test_downloader_skips_years_before_reported_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx=FakeHistoryContext([]);types={'day':'DAY','autype':'NONE','autype_name':'none','session':'RTH'}
            state=download(ctx,['US.NEW'],'2020-01-01','2021-12-31',Path(tmp)/'raw',
                Path(tmp)/'state.json',kinds=('day',),futu_types=types,
                listing_dates={'US.NEW':'2022-01-01'})
            self.assertEqual(ctx.calls,0)
            self.assertEqual(len(state['unavailable']),2)

    def test_quality_reports_daily_failures(self):
        master=pd.DataFrame([{'code':'US.A','listing_date':'2026-01-02','delisting_date':''}])
        cal=pd.DataFrame({'session_date':pd.to_datetime(['2026-01-02','2026-01-05'])})
        daily=pd.DataFrame([{'stock':'US.A','date':'2026-01-02','open':2,'high':1,
                             'low':1,'close':1,'volume':100}])
        result=validate_daily(daily,master,cal)
        self.assertEqual(result.quality.iloc[0],'quality_fail')

    def test_end_to_end_pipeline_reuses_local_daily_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);raw=root/'raw'/'day'/'qfq'/'year=2026';raw.mkdir(parents=True)
            dates=pd.bdate_range('2026-01-02',periods=30)
            for code in ('US.SPY','US.A'):
                frame=pd.DataFrame({'code':code,'time_key':dates.astype(str),
                    'open':10.,'high':11.,'low':9.,'close':10.,'volume':1_000_000.,
                    'turnover':10_000_000.})
                frame.to_csv(raw/f'{code.replace(".","_")}.csv.gz',index=False,compression='gzip')
            master=root/'master.csv'
            pd.DataFrame([
                {'code':'US.SPY','listing_date':'1993-01-29','delisting_date':'','asset_type':'etf'},
                {'code':'US.A','listing_date':'2000-01-01','delisting_date':'','asset_type':'stock'},
            ]).to_csv(master,index=False)
            result=run_pipeline(master_path=master,start=str(dates[0].date()),end=str(dates[-1].date()),
                universe_start=str(dates[20].date()),universe_end=str(dates[-1].date()),
                run_id='test',raw_root=root/'raw',runs_root=root/'runs',
                checkpoint=root/'checkpoint.json',skip_download=True)
            self.assertEqual(result['status'],'complete')
            self.assertEqual(result['stages']['quality']['failed'],0)
            self.assertTrue((root/'runs'/'test'/'universe.csv').exists())
            self.assertTrue(result['files']['daily.csv.gz']['sha256'])


class QualityIntervalTests(unittest.TestCase):
    """质量失败必须按「股票+年份」定位，不能波及全历史（设计 §8.4）。"""

    def _calendar(self):
        return pd.DataFrame({'session_date': pd.bdate_range('2019-01-02', '2020-12-31')})

    def test_validate_daily_reports_failing_years_only(self):
        master = pd.DataFrame([{'code': 'US.A', 'listing_date': '2019-01-02',
                                'delisting_date': ''}])
        # 只有 2019 有数据，2020 整段缺失
        daily = pd.DataFrame({'stock': 'US.A', 'date': pd.bdate_range('2019-01-02', '2019-12-31'),
                              'open': 10., 'high': 11., 'low': 9., 'close': 10, 'volume': 1e6})
        q = validate_daily(daily, master, self._calendar())
        self.assertEqual(q.quality.iloc[0], 'quality_fail')
        self.assertEqual(q.failed_years.iloc[0], '2020',
                         '整段 2020 缺失，只应标记 2020 而不是全历史')

    def test_clean_stock_has_no_failed_years(self):
        master = pd.DataFrame([{'code': 'US.A', 'listing_date': '2019-01-02',
                                'delisting_date': ''}])
        daily = pd.DataFrame({'stock': 'US.A', 'date': pd.bdate_range('2019-01-02', '2020-12-31'),
                              'open': 10., 'high': 11., 'low': 9., 'close': 10, 'volume': 1e6})
        q = validate_daily(daily, master, self._calendar())
        self.assertEqual(q.quality.iloc[0], 'good')
        self.assertEqual(q.failed_years.iloc[0], '')

    def test_propagate_marks_only_failing_year(self):
        liq = pd.DataFrame({
            'date': pd.to_datetime(['2019-06-03', '2020-06-01', '2020-06-02']),
            'code': ['US.A'] * 3,
            'previous_close': [10., 10., 10.], 'adv20': [1e8] * 3,
            'liquidity_as_of': pd.to_datetime(['2019-05-31', '2020-05-29', '2020-06-01']),
            'quality': ['good'] * 3})
        q = pd.DataFrame([{'stock': 'US.A', 'quality': 'quality_fail',
                           'failed_years': '2020'}])
        out = propagate_quality(liq, q)
        self.assertEqual(list(out.quality), ['good', 'quality_fail', 'quality_fail'],
                         '2019 不应被 2020 的失败波及')

    def test_propagate_falls_back_to_whole_stock_when_no_years(self):
        liq = pd.DataFrame({'date': pd.to_datetime(['2019-06-03', '2020-06-01']),
                            'code': ['US.A', 'US.A'], 'previous_close': [10., 10.],
                            'adv20': [1e8, 1e8],
                            'liquidity_as_of': pd.to_datetime(['2019-05-31', '2020-05-29']),
                            'quality': ['good', 'good']})
        q = pd.DataFrame([{'stock': 'US.A', 'quality': 'quality_fail', 'failed_years': ''}])
        out = propagate_quality(liq, q)
        self.assertEqual(list(out.quality), ['quality_fail', 'quality_fail'])


if __name__=='__main__':unittest.main()
