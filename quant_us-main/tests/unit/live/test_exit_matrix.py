import unittest
import numpy as np
import pandas as pd
from scripts.exit_matrix import (EXIT_IDS,apply_matrix_portfolio,run_exit_matrix,
                                 simulate_daily)


def daily():
    dates=pd.bdate_range('2026-01-01',periods=60,tz='UTC');close=np.arange(100.,160.)
    return pd.DataFrame({'stock':'US.X','date':dates,'open':close,'high':close+1,
                         'low':close-.5,'close':close,'volume':1000})


def verified_quality():
    return pd.DataFrame([{'security_id':'SEC-X','from_session':'2020-01-01',
        'to_session':'2030-12-31','quality_status':'verified','reason':''}])


class ExitMatrixTests(unittest.TestCase):
    def test_raw_asof_requires_actions(self):
        entries=pd.DataFrame([{'experiment':'A','setup_id':'s1','stock':'US.X',
          'entry_time':'2026-01-02T14:30:00Z','entry_price':101,'initial_stop':95,
          'price_basis':'raw_asof'}])
        with self.assertRaisesRegex(ValueError,'RAW_ASOF_ACTIONS_REQUIRED'):
            run_exit_matrix(entries,daily())

    def test_split_preserves_equity_and_does_not_trigger_false_stop(self):
        dates=pd.bdate_range('2026-01-05',periods=5,tz='UTC')
        b=pd.DataFrame({'stock':'US.X','security_id':'SEC-X','date':dates,
            'open':[100.,50.,50.,50.,50.],'high':[100.,50.,50.,50.,50.],
            'low':[100.,49.,50.,50.,50.],'close':[100.,50.,50.,50.,50.],
            'volume':1000.,'atr14':1.})
        actions=pd.DataFrame([{'security_id':'SEC-X','action_type':'split',
            'ex_date':'2026-01-06','ratio':2.,'cash_amount':0.}])
        row={'entry_time':'2026-01-05T14:30:00Z','entry_price':100.,'initial_stop':95.,
             'security_id':'SEC-X','price_basis':'raw_asof'}
        result=simulate_daily(row,b,'E1',actions=actions)
        self.assertEqual(result['exit_reason'],'TIME_EXIT')
        self.assertAlmostEqual(result['gross_pnl_pct'],0.)
        self.assertEqual(result['shares_at_exit'],2.)
        self.assertEqual(result['split_factor_cumulative'],2.)

    def test_cash_dividend_is_included_in_total_return(self):
        dates=pd.bdate_range('2026-01-05',periods=5,tz='UTC')
        b=pd.DataFrame({'stock':'US.X','security_id':'SEC-X','date':dates,
            'open':[100.,98.,98.,98.,98.],'high':[100.,98.,98.,98.,98.],
            'low':[100.,98.,98.,98.,98.],'close':[100.,98.,98.,98.,98.],
            'volume':1000.,'atr14':1.})
        actions=pd.DataFrame([{'security_id':'SEC-X','action_type':'cash_dividend',
            'ex_date':'2026-01-06','ratio':0.,'cash_amount':2.}])
        row={'entry_time':'2026-01-05T14:30:00Z','entry_price':100.,'initial_stop':95.,
             'security_id':'SEC-X','price_basis':'raw_asof'}
        result=simulate_daily(row,b,'E1',actions=actions)
        self.assertAlmostEqual(result['gross_pnl_pct'],0.)
        self.assertEqual(result['cash_dividend_per_initial_share'],2.)

    def test_structure_pivot_lows_are_rescaled_across_split(self):
        dates=pd.bdate_range('2026-01-05',periods=6,tz='UTC')
        b=pd.DataFrame({'stock':'US.X','security_id':'SEC-X','date':dates,
            'open':[100.,98.,95.,48.,49.,44.],
            'high':[101.,99.,96.,49.,50.,45.],
            'low':[98.,96.,90.,47.,48.,43.],
            'close':[99.,97.,94.,48.,49.,44.],'volume':1000.,'atr14':1.})
        actions=pd.DataFrame([{'security_id':'SEC-X','action_type':'split',
            'ex_date':str(dates[3].date()),'ratio':2.,'cash_amount':0.}])
        row={'entry_time':str(dates[0]),'entry_price':100.,'initial_stop':80.,
             'security_id':'SEC-X','price_basis':'raw_asof'}
        result=simulate_daily(row,b,'E10',actions=actions)
        self.assertEqual(result['exit_reason'],'GAP_STOP')
        self.assertEqual(result['exit_price'],44.)
        self.assertEqual(pd.Timestamp(result['exit_time']).date(),dates[5].date())

    def test_raw_entry_day_skips_pre_entry_open_stop_but_checks_intraday(self):
        dates=pd.bdate_range('2026-01-05',periods=2,tz='UTC')
        b=pd.DataFrame({'stock':'US.X','security_id':'SEC-X','date':dates,
            'open':[100.,100.],'high':[101.,101.],'low':[94.,100.],
            'close':[100.,100.],'volume':1000.,'atr14':1.})
        row={'entry_time':'2026-01-05T14:30:00Z','entry_price':100.,'initial_stop':95.,
             'security_id':'SEC-X','price_basis':'raw_asof'}
        result=simulate_daily(row,b,'E5',actions=pd.DataFrame())
        self.assertEqual(result['exit_reason'],'STOP')
        self.assertEqual(result['exit_price'],95.)

    def test_raw_matrix_runs_all_cells_with_action_accounting(self):
        dates=pd.bdate_range('2026-01-01',periods=60,tz='UTC')
        prices=np.array([100. if i < 10 else 50. for i in range(60)])
        b=pd.DataFrame({'stock':'US.X','security_id':'SEC-X','date':dates,
            'open':prices,'high':prices*1.01,'low':prices*.99,'close':prices,'volume':1000.})
        actions=pd.DataFrame([{'security_id':'SEC-X','action_type':'split',
            'ex_date':str(dates[10].date()),'ratio':2.,'cash_amount':0.}])
        entry=pd.DataFrame([{'experiment':'A','setup_id':'s1','stock':'US.X',
            'security_id':'SEC-X','entry_time':str(dates[5]),'entry_price':100.,
            'initial_stop':80.,'price_basis':'raw_asof','portfolio_rank':1}])
        out=run_exit_matrix(entry,b,actions=actions,quality=verified_quality())
        self.assertEqual(len(out),44)
        fixed=out[(out.exit_method=='E2')&(out.cost_scenario==.001)].iloc[0]
        self.assertAlmostEqual(fixed.gross_pnl_pct,0.)
        self.assertEqual(fixed.split_factor_cumulative,2.)

    def test_raw_matrix_rejects_registered_entry_that_differs_from_raw_open(self):
        dates=pd.bdate_range('2026-01-01',periods=20,tz='UTC')
        b=pd.DataFrame({'stock':'US.X','security_id':'SEC-X','date':dates,
            'open':100.,'high':101.,'low':99.,'close':100.,'volume':1000.})
        entry=pd.DataFrame([{'experiment':'A','setup_id':'s1','stock':'US.X',
            'security_id':'SEC-X','entry_time':str(dates[5]),'entry_price':101.,
            'initial_stop':90.,'price_basis':'raw_asof'}])
        with self.assertRaisesRegex(ValueError,'RAW_ASOF_ENTRY_OPEN_MISMATCH'):
            run_exit_matrix(entry,b,actions=pd.DataFrame(),quality=verified_quality())

    def test_quality_rejection_is_preserved_in_matrix_denominator(self):
        dates=pd.bdate_range('2026-01-01',periods=20,tz='UTC')
        b=pd.DataFrame({'stock':'US.X','security_id':'SEC-X','date':dates,
            'open':100.,'high':101.,'low':99.,'close':100.,'volume':1000.})
        entry=pd.DataFrame([{'experiment':'A','setup_id':'s1','stock':'US.X',
            'security_id':'SEC-X','entry_time':str(dates[5]),'entry_price':100.,
            'initial_stop':90.,'price_basis':'raw_asof'}])
        quality=verified_quality().assign(quality_status='unverified',reason='UNKNOWN_LISTING_DATE')
        out=run_exit_matrix(entry,b,actions=pd.DataFrame(),quality=quality)
        self.assertEqual(len(out),44)
        self.assertTrue((out.data_quality=='quality_rejected').all())
        self.assertTrue((out.quality_reject_reason=='UNKNOWN_LISTING_DATE').all())
        self.assertFalse(out.portfolio_accepted.any())

    def test_quality_interval_must_cover_full_observation_window(self):
        dates=pd.bdate_range('2026-01-01',periods=60,tz='UTC')
        b=pd.DataFrame({'stock':'US.X','security_id':'SEC-X','date':dates,
            'open':100.,'high':101.,'low':99.,'close':100.,'volume':1000.})
        entry=pd.DataFrame([{'experiment':'A','setup_id':'s1','stock':'US.X',
            'security_id':'SEC-X','entry_time':str(dates[5]),'entry_price':100.,
            'initial_stop':90.,'price_basis':'raw_asof'}])
        quality=verified_quality().assign(to_session=str(dates[20].date()))
        out=run_exit_matrix(entry,b,actions=pd.DataFrame(),quality=quality)
        self.assertTrue((out.quality_reject_reason=='QUALITY_INTERVAL_MISSING').all())

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


    def test_intraday_stop_fills_at_stop_line(self):
        dates=pd.bdate_range('2026-01-05',periods=2,tz='UTC')
        b=pd.DataFrame({'stock':'US.X','date':dates,'open':[100.,100.],'high':[101.,101.],
                        'low':[99.,98.],'close':[100.,100.],'volume':1000})
        row={'entry_time':'2026-01-05T14:30:00Z','entry_price':100,'initial_stop':99.5}
        r=simulate_daily(row,b,'E5')
        self.assertEqual(r['exit_reason'],'STOP')
        self.assertEqual(r['exit_price'],99.5)
        self.assertEqual(pd.Timestamp(r['exit_time']).date(),pd.Timestamp('2026-01-05').date())

    def test_protection_line_effective_next_day(self):
        # 成交日大幅冲高把吊灯保护线推到 198，但只能在下一交易日生效。
        dates=pd.bdate_range('2026-01-05',periods=3,tz='UTC')
        b=pd.DataFrame({'stock':'US.X','date':dates,'open':[100.,150.,150.],
                        'high':[200.,151.,151.],'low':[100.,150.,150.],
                        'close':[150.,150.,150.],'volume':1000,'atr14':[1.,1.,1.]})
        row={'entry_time':'2026-01-05T14:30:00Z','entry_price':100,'initial_stop':90}
        r=simulate_daily(row,b,'E7')
        self.assertEqual(r['exit_reason'],'GAP_STOP')   # 若当日生效会变成 STOP@198
        self.assertEqual(r['exit_price'],150.)
        self.assertEqual(pd.Timestamp(r['exit_time']).date(),pd.Timestamp('2026-01-06').date())

    def test_data_end_marked_when_fewer_than_40_bars(self):
        dates=pd.bdate_range('2026-01-05',periods=3,tz='UTC')
        b=pd.DataFrame({'stock':'US.X','date':dates,'open':100.,'high':101.,'low':99.,
                        'close':100.,'volume':1000})
        row={'entry_time':'2026-01-05T14:30:00Z','entry_price':100,'initial_stop':1.}
        r=simulate_daily(row,b,'E4')     # 固定持有 40 日，数据不足
        self.assertEqual(r['exit_reason'],'DATA_END')
        self.assertEqual(r['data_quality'],'right_censored')
        self.assertIsNone(r['net_pnl_pct'])
        self.assertEqual(pd.Timestamp(r['exit_time']).date(),pd.Timestamp('2026-01-07').date())

    def test_close_price_never_releases_position_at_open(self):
        dates=pd.bdate_range('2026-01-05',periods=5,tz='UTC')
        b=pd.DataFrame({'stock':'US.X','date':dates,'open':100.,'high':102.,'low':99.,
                        'close':101.,'volume':1000})
        row={'entry_time':'2026-01-05T14:30:00Z','entry_price':100,'initial_stop':90}
        r=simulate_daily(row,b,'E1')
        self.assertEqual(r['exit_reason'],'TIME_EXIT')
        self.assertEqual(pd.Timestamp(r['exit_time']).hour,21)
        # 收盘价退出当日的开盘不能释放仓位。
        trades=pd.DataFrame([{'experiment':'A','exit_method':'E1','cost_scenario':.002,
            'setup_id':f's{i}','stock':f'US.{i}',
            'entry_time':'2026-01-05T14:30:00Z' if i<3 else '2026-01-09T14:30:00Z',
            'exit_time':r['exit_time'] if i<3 else '2026-01-12T21:00:00Z',
            'data_quality':'good'} for i in range(4)])
        accepted=apply_matrix_portfolio(trades)
        self.assertFalse(bool(accepted.loc[accepted.setup_id=='s3','portfolio_accepted'].iloc[0]))

    def test_three_position_limit_is_per_cell(self):
        def row(exp,sid,rank):
            return {'experiment':exp,'exit_method':'E1','cost_scenario':.002,'setup_id':sid,
                    'stock':'US.'+sid,'entry_time':'2026-01-05T14:30:00Z',
                    'exit_time':'2026-02-05T14:30:00Z','data_quality':'good','portfolio_rank':rank}
        rows=[row('A',f's{i}',i) for i in range(5)]+[row('B','x0',0)]
        out=apply_matrix_portfolio(pd.DataFrame(rows))
        self.assertEqual(int(out[out.experiment=='A'].portfolio_accepted.sum()),3)
        self.assertEqual(int(out[out.experiment=='B'].portfolio_accepted.sum()),1)


if __name__=='__main__':unittest.main()
