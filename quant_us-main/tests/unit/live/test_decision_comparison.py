import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from scripts.live_trading.decision_ledger.comparison import replay
from scripts.live_trading.decision_ledger.event_store import make_event, EventStore
from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.position_review import PositionReviewScheduler


class ComparisonTests(unittest.TestCase):
    def events(self):
        events=[]
        for code in ('US.A','US.B'):
            sid='signal_'+code
            p=dict(stock_code=code,strategy='dip_buy',strategy_version='v1',timeframe='15m',
                   signal_bar_end='2026-09-04T13:30:00+00:00',passed=True,risk_group='semis',
                   details={'price':100,'initial_stop':95,'target':110})
            e=make_event('DRY-RUN','rule_candidate',sid,p,signal_id=sid)
            e.update(event_time='2026-09-04T13:30:00+00:00',observed_at='2026-09-04T13:30:00+00:00')
            events.append(e)
            r=make_event('DRY-RUN','llm_completed',sid,dict(status='complete',recommendation='support_execute' if code=='US.B' else 'oppose_execute',
                proposed_action='buy' if code=='US.B' else 'hold',expires_at=pd.Timestamp('2026-09-04T14:00:00Z').timestamp()),signal_id=sid)
            r.update(event_time='2026-09-04T13:31:00+00:00',observed_at='2026-09-04T13:31:00+00:00')
            events.append(r)
        return events

    def bars(self):
        return pd.DataFrame([dict(code=code,date=stamp,open=price,high=price+1,low=price-1,close=price)
            for stamp,price in [('2026-09-04T13:30:00Z',100),('2026-09-04T13:45:00Z',100),
                                ('2026-09-04T14:00:00Z',90),('2026-09-04T14:15:00Z',85)]
            for code in ('US.A','US.B')])

    def config(self):
        return ({'max_positions':1,'group_limits':{'semis':.0075}},
                {'experiment_id':'entry-v1','account_scope':'DRY-RUN','decision_delay_seconds':120,
                 'entry_ttl_seconds':1800,'exit_approval_delay_bars':1,'defer_policy':'observe'})

    def test_deterministic_shared_clock_and_cash_constrained_layers(self):
        cfg,exp=self.config();events=self.events();bars=self.bars()
        a=replay(events,bars,cfg,exp)
        self.assertEqual(a,replay(events+events,bars,cfg,exp))
        self.assertEqual(a['layers']['A']['included_signals'],2)
        self.assertEqual(a['layers']['B']['included_signals'],1)
        self.assertEqual(a['layers']['C']['included_signals'],0)
        self.assertEqual(len(a['layers']['A']['trades']),1)
        self.assertEqual(a['layers']['A']['trades'][0]['entry_time'],a['layers']['B']['trades'][0]['entry_time'])
        self.assertLess(a['layers']['A']['trades'][0]['net_pnl'],0)

    def test_missing_model_kept_in_groups_and_future_model_not_used(self):
        cfg,exp=self.config();events=self.events()
        events=[e for e in events if e['event_type']=='rule_candidate']
        result=replay(events,self.bars(),cfg,exp)
        self.assertEqual(len(result['cross_groups']),2)
        self.assertEqual(result['layers']['B']['included_signals'],0)
        events=self.events()
        for e in events:
            if e['event_type']=='llm_completed':e['observed_at']='2026-09-04T14:01:00+00:00'
        self.assertEqual(replay(events,self.bars(),cfg,exp)['layers']['B']['included_signals'],0)

    def test_naive_bars_and_undefined_defer_policy_rejected(self):
        cfg,exp=self.config();bars=self.bars();bars['date']=bars.date.str.replace('Z','')
        with self.assertRaises(ValueError):replay(self.events(),bars,cfg,exp)
        exp['defer_policy']='best_later_price'
        with self.assertRaises(ValueError):replay(self.events(),self.bars(),cfg,exp)


class ShadowPositionReviewTests(unittest.TestCase):
    def test_default_disabled_and_shadow_dedup_does_not_mutate_holdings(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry=PositionRegistry(Path(tmp)/'state.db','DRY-RUN')
            scheduler=PositionReviewScheduler(registry,None)
            self.assertFalse(scheduler.schedule('US.A',100))
            registry.open('US.A','dip_buy',10,100,plan_id='plan',plan_version=1,trade_id='trade',initial_stop=95)
            plan={'plan_id':'plan','plan_version':1,'exit_policy':{'initial_stop':95},'risk':{'initial_stop':95}}
            with scheduler.events.transaction() as con:scheduler.events.snapshot(con,'plan','plan',1,plan)
            scheduler.config={'enabled':True,'check_interval_seconds':1}
            before=registry.all()
            tasks=[]
            fake_queue=SimpleNamespace(put=lambda priority,task:tasks.append(task))
            with patch('scripts.live_trading.position_review.review_queue',return_value=fake_queue):
                self.assertTrue(scheduler.schedule('US.A',95.1,event_reason='new_event'))
                self.assertFalse(scheduler.schedule('US.A',95.1,event_reason='new_event'))
                tasks[0]()
            self.assertEqual(before,registry.all())
            events=scheduler.events.events()
            self.assertEqual(sum(e['event_type']=='position_reviewed' for e in events),1)
            self.assertFalse(any(e['event_type']=='order_intent_created' for e in events))
