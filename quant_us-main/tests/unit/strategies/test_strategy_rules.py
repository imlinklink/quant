import unittest
import pandas as pd
from scripts.live_trading.strategy_rules import completed_bars, exit_reason, pullback_signal


class StrategyRules(unittest.TestCase):
    def test_unclosed_fifteen_minute_bar_excluded(self):
        frame=pd.DataFrame({'time_key':['2026-03-06 09:30','2026-03-06 09:45'], 'close':[100,1000]})
        self.assertEqual(len(completed_bars(frame,'2026-03-06 09:46',15)),1)

    def test_dst_market_timezone(self):
        frame=pd.DataFrame({'time_key':['2026-03-09 09:30'],'close':[100]})
        self.assertEqual(len(completed_bars(frame,pd.Timestamp('2026-03-09 13:44Z'),15)),0)
        self.assertEqual(len(completed_bars(frame,pd.Timestamp('2026-03-09 13:45Z'),15)),1)

    def test_fixed_target_matches_plan(self):
        rec={'entry_mode':'dip_buy','entry_price':100,'target':105,'initial_stop':95,'opened_at':0}
        self.assertEqual(exit_reason(rec,105,now='2026-03-09'),'REBOUND_TARGET')
        self.assertEqual(exit_reason(rec,94,now='2026-03-09'),'HARD_STOP')

    def test_timeout_counts_completed_bars_not_wall_clock(self):
        now='2026-03-09 09:46'
        rec={'entry_mode':'dip_buy','entry_price':100,'initial_stop':95,'time_exit_bars':2,
             'opened_at':pd.Timestamp('2026-03-06 15:45',tz='America/New_York').timestamp()}
        frame=pd.DataFrame({'time_key':['2026-03-06 15:45','2026-03-09 09:30'], 'close':[99,99]})
        self.assertEqual(exit_reason(rec,99,intraday=frame,now=now),'REBOUND_TIMEOUT')
        self.assertIsNone(exit_reason(rec,99,intraday=frame.iloc[:1],now=now))

    def test_breakout_failure_counts_sessions(self):
        rec={'entry_mode':'donchian','breakout_level':100,'initial_stop':90,'failure_sessions':2,
             'opened_at':pd.Timestamp('2026-03-06 09:30',tz='America/New_York').timestamp()}
        frame=pd.DataFrame({'date':pd.to_datetime(['2026-03-06','2026-03-09']),'close':[101,99]})
        self.assertEqual(exit_reason(rec,101,daily=frame,now='2026-03-10'),'BREAKOUT_FAILED')
        self.assertIsNone(exit_reason(rec,101,daily=frame,now='2026-03-09 15:59'))

    def test_missing_sector_is_not_zero_relative_return(self):
        self.assertIsNone(pullback_signal(None,None,None,'2026-03-10'))

    def test_pullback_requires_price_confirmation(self):
        import numpy as np
        dates=pd.bdate_range('2026-01-01',periods=60)
        close=np.linspace(100,160,60);close[-5:]=[154,154,154,154,154]
        daily=pd.DataFrame(dict(date=dates,close=close,open=close,high=close+1,low=close-1))
        daily.loc[35,'low']=120
        sector=pd.DataFrame(dict(date=dates,close=np.linspace(100,105,60)))
        day=dates[-1]+pd.Timedelta(days=1)
        minutes=pd.date_range(day+pd.Timedelta(hours=9,minutes=30),periods=6,freq='15min')
        bars=pd.DataFrame(dict(date=minutes,open=[153]*6,high=[154]*5+[156],low=[152]*6,close=[153]*5+[155]))
        now=minutes[-1]+pd.Timedelta(minutes=15)
        cfg={'min_rr':.1}
        signal=pullback_signal(daily,sector,bars,now,cfg)
        self.assertIsNotNone(signal)
        self.assertLess(signal['initial_stop'],155)
        bars.loc[5,'close']=153
        self.assertIsNone(pullback_signal(daily,sector,bars,now,cfg))
