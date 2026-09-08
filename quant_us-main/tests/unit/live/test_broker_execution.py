"""模拟富途响应；不初始化SDK连接。"""
import tempfile
import time
import unittest
from contextlib import nullcontext
from datetime import datetime as RealDatetime
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from scripts.live_trading.execution import ExecutionService
from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.approval.proposal_store import ProposalStore


class FakeBroker:
    def __init__(self):
        self.calls=0
        self.rows=[]
        self.fail=False
    def get_trade_ctx(self):return nullcontext(self)
    def order_list_query(self,**kwargs):return 0,pd.DataFrame(self.rows)
    def position_list_query(self,**kwargs):return 0,pd.DataFrame(columns=['code','qty'])
    def accinfo_query(self,**kwargs):return 0,pd.DataFrame([dict(total_assets=100000,cash=100000)])
    def place_order(self,**kwargs):
        self.calls+=1
        self.rows=[dict(order_id='123',order_status='SUBMITTED',dealt_qty=0,code=kwargs['code'],remark=kwargs['remark'])]
        if self.fail:raise TimeoutError('response lost')
        return 0,pd.DataFrame(self.rows)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.patch=patch.object(ProposalStore,'_record_ledger');self.patch.start();self.addCleanup(self.patch.stop)
        self.store=ProposalStore(log_dir=self.tmp.name)
        self.registry=PositionRegistry(Path(self.tmp.name)/'state.db')
        self.broker=FakeBroker()
        self.service=ExecutionService(self.broker,{'live_manager':{'trd_env':'SIMULATE','acc_id':123},
            'risk_budget':{'code_groups':{'US.A':'semis'},'group_limits':{'semis':.0075}}},self.store,registry=self.registry)
        self.quote=patch.object(self.service,'fresh_quote',return_value=(100,100000,time.time()))
        self.quote.start();self.addCleanup(self.quote.stop)
        self.clock=patch('scripts.live_trading.execution.datetime');self.clock.start().now.side_effect=lambda tz: RealDatetime(2026,3,10,10,tzinfo=tz)
        self.addCleanup(self.clock.stop)

    def proposal(self):
        p=self.store.create(stock_code='US.A',side='buy',price=100,quantity=50,
            llm={'verdict':'allow','reason':'review'},entry_mode='donchian',trade_plan={'initial_stop':95})
        self.store.approve(p['id']);self.store.mark(p['id'],'executing')
        return self.store.get(p['id'])

    def test_accepted_order_pending_then_filled(self):
        p=self.proposal()
        self.assertEqual(self.service.submit(p,100),'submitted')
        self.assertIsNone(self.registry.get('US.A'))
        self.broker.rows[0].update(order_status='FILLED_ALL',dealt_qty=49,dealt_avg_price=99.9)
        self.service.reconcile()
        self.assertEqual(self.registry.get('US.A')['qty'],49)
        self.assertEqual(self.store.get(p['id'])['status'],'executed')
        self.assertEqual(self.broker.calls,1)

    def test_timeout_discovery_does_not_resubmit(self):
        p=self.proposal();self.broker.fail=True
        with self.assertRaises(TimeoutError):self.service.submit(p,100)
        with self.registry.transaction() as b:self.assertEqual(b['orders'][p['id']]['status'],'unknown')
        self.service.reconcile()
        self.assertEqual(self.store.get(p['id'])['status'],'submitted')
        self.assertEqual(self.broker.calls,1)

    def test_restart_restores_pending_order_display(self):
        p=self.proposal();self.service.submit(p,100)
        newstore=ProposalStore(log_dir=self.tmp.name)
        newservice=ExecutionService(self.broker,self.service.config,newstore,registry=self.registry)
        newservice.reconcile()
        self.assertEqual(newstore.get(p['id'])['status'],'submitted')
        self.assertFalse(newstore.approve(p['id']))

    def test_rejected_order_keeps_no_position(self):
        p=self.proposal();self.service.submit(p,100)
        self.broker.rows[0].update(order_status='FAILED')
        self.service.reconcile()
        self.assertIsNone(self.registry.get('US.A'))
        self.assertEqual(self.store.get(p['id'])['status'],'failed')
