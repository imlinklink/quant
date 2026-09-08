"""离线验收：审批、部分成交、未知结果、风险占用、重启。无券商连接。"""
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from scripts.live_trading.approval.proposal_store import ProposalStore
from scripts.live_trading.execution import ExecutionService, risk_quantity
from scripts.live_trading.position_registry import PositionRegistry

CFG={'risk_budget':{'per_trade':.0025,'total':.015,'group_limits':{'semis':.0075},
                    'code_groups':{'US.A':'semis','US.B':'semis'},'dry_run_equity':100000}}
REVIEW={'verdict':'allow','reason':'测试评估已完成'}


class ExecutionContract(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger_patch=patch.object(ProposalStore,'_record_ledger')
        self.ledger_patch.start();self.addCleanup(self.ledger_patch.stop)
        self.store=ProposalStore(log_dir=self.tmp.name)
        self.registry=PositionRegistry(Path(self.tmp.name)/'state.db')
        self.service=ExecutionService(None,CFG,self.store,True,self.registry)

    def proposal(self,side='buy',code='US.A'):
        p=self.store.create(stock_code=code,side=side,price=100,quantity=50,
            llm=REVIEW,entry_mode='donchian',trade_plan={'initial_stop':95})
        self.assertTrue(self.store.approve(p['id']))
        self.assertTrue(self.store.mark(p['id'],'executing'))
        return self.store.get(p['id'])

    def pending_order(self):
        p=self.proposal()
        with self.registry.transaction() as b:
            b['orders'][p['id']]=dict(id=p['id'],code='US.A',side='buy',qty=50,price=100,
                status='submitted',risk=250,filled_qty=0,cost_per_share=.05,
                metadata={'initial_stop':95,'risk_group':'semis','entry_mode':'donchian'})
        return p['id']

    def test_no_llm_no_approval(self):
        p=self.store.create(stock_code='US.A',llm=None)
        self.assertFalse(self.store.approve(p['id']))
        self.assertFalse(self.store.mark(p['id'],'executing'))

    def test_expired_sell_never_executes(self):
        p=self.store.create(stock_code='US.A',side='sell',llm=REVIEW,expires_at=time.time()-1)
        self.store.expire_old()
        self.assertEqual(self.store.get(p['id'])['status'],'expired')
        self.assertFalse(self.store.approve(p['id']))

    def test_expiry_race_at_execution(self):
        p=self.proposal()
        with self.store._lock:
            self.store._items[p['id']]['expires_at']=time.time()-1
        with self.assertRaises(ValueError):self.service.submit(p,100)
        self.assertEqual(self.registry.count(),0)

    def test_acceptance_does_not_open_position(self):
        pid=self.pending_order()
        self.service.apply_report(pid,{'order_id':'1','order_status':'SUBMITTED','dealt_qty':0})
        self.assertIsNone(self.registry.get('US.A'))
        self.assertEqual(self.store.get(pid)['status'],'submitted')

    def test_partial_fill_idempotent_and_restart(self):
        pid=self.pending_order()
        report={'order_id':'1','order_status':'FILLED_PART','dealt_qty':20,'dealt_avg_price':101}
        self.service.apply_report(pid,report);self.service.apply_report(pid,report)
        second=PositionRegistry(self.registry.path,'DRY-RUN')
        self.assertEqual(second.get('US.A')['qty'],20)
        self.assertEqual(second.mode('US.A'),'donchian')
        self.service.apply_report(pid,dict(report,order_status='FILLED_ALL',dealt_qty=50,dealt_avg_price=102))
        self.assertEqual(second.get('US.A')['qty'],50)
        self.assertEqual(second.get('US.A')['entry_price'],102)

    def test_partial_sell_retains_protection(self):
        self.registry.open('US.A','donchian',50,100,initial_risk=250,exit_state={'stop_line':98})
        pid=self.pending_order()
        with self.registry.transaction() as b:b['orders'][pid]['side']='sell'
        self.service.apply_report(pid,dict(order_status='FILLED_PART',dealt_qty=20,dealt_avg_price=99))
        self.assertEqual(self.registry.get('US.A')['qty'],30)
        self.assertEqual(self.registry.get('US.A')['exit_state']['stop_line'],98)
        self.service.apply_report(pid,dict(order_status='CANCELLED_PART',dealt_qty=20,dealt_avg_price=99))
        self.assertEqual(self.registry.get('US.A')['qty'],30)

    def test_unknown_prevents_duplicate_order(self):
        pid=self.pending_order()
        with self.registry.transaction() as b:b['orders'][pid]['status']='unknown'
        with self.assertRaises(ValueError):self.service.submit(self.proposal(code='US.B'),100)

    def test_two_buyers_cannot_open_same_code(self):
        proposals=[self.proposal(),self.proposal()]
        def submit(p):
            try:return self.service.submit(p,100)
            except ValueError:return 'blocked'
        with ThreadPoolExecutor(2) as ex:results=list(ex.map(submit,proposals))
        self.assertEqual(sorted(results),['blocked','filled'])
        self.assertLessEqual(self.registry.get('US.A')['initial_risk'],250)

    def test_unknown_position_blocks_budget(self):
        with self.assertRaises(ValueError):
            risk_quantity(100,95,100000,100000,[{'qty':10}],[],CFG['risk_budget'],'semis',5000)

    def test_pending_orders_reserve_risk(self):
        order=dict(side='buy',status='submitted',qty=100,price=100,risk=750,metadata={'risk_group':'semis'})
        with self.assertRaises(ValueError):
            risk_quantity(100,95,100000,100000,[],[order],CFG['risk_budget'],'semis',5000)

    def test_account_namespaces_do_not_mix(self):
        self.registry.open('US.A','donchian',10,100)
        real=PositionRegistry(self.registry.path,'REAL:123')
        self.assertEqual(real.count(),0)

    def test_two_codes_cannot_exceed_position_limit(self):
        import copy
        config=copy.deepcopy(CFG);config['risk_budget']['max_positions']=1
        service=ExecutionService(None,config,self.store,True,self.registry)
        proposals=[self.proposal(code='US.A'),self.proposal(code='US.B')]
        def submit(p):
            try:return service.submit(p,100)
            except ValueError:return 'blocked'
        with ThreadPoolExecutor(2) as ex:results=list(ex.map(submit,proposals))
        self.assertEqual(sorted(results),['blocked','filled'])
        self.assertEqual(self.registry.count(),1)
