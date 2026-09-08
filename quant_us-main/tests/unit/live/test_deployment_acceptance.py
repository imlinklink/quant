"""Deployment rehearsal: temporary databases and mock broker only."""
import json
import sqlite3
import subprocess
import sys
import time
import unittest
from datetime import datetime
from unittest.mock import patch

from scripts.live_trading.approval.proposal_store import ProposalStore
from scripts.live_trading.execution import ExecutionService
from scripts.live_trading.position_registry import PositionRegistry


class DeploymentAcceptance(unittest.TestCase):
    def setUp(self):
        from tests.unit.live.test_decision_workflow import DecisionContracts
        self.fixture = DecisionContracts()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def pending(self):
        f=self.fixture
        item=f.approve(f.review(f.proposal()))
        with f.registry.transaction() as book:
            book['orders'][item['id']]=dict(id=item['id'],proposal=item,code='US.A',side='buy',qty=20,
                price=100,status='submitted',filled_qty=0,risk=100,cost_per_share=.05,
                metadata={'initial_stop':95,'entry_mode':'dip_buy','risk_group':'semis'})
        return item

    def test_partial_progress_is_persisted_and_restart_does_not_regress(self):
        f=self.fixture;item=self.pending()
        service=ExecutionService(None,f.config,f.store,True,f.registry)
        for qty in (5,10):
            service.apply_report(item['id'],dict(order_id='broker1',order_status='FILLED_PART',dealt_qty=qty,dealt_avg_price=101))
        restored=ProposalStore(log_dir=f.path,registry=PositionRegistry(f.registry.path,'DRY-RUN'))
        self.assertEqual(restored.get(item['id'])['filled_quantity'],10)
        self.assertIn('10.0/20',restored.get(item['id'])['note'])
        self.assertFalse(restored.approve(item['id']))
        with f.registry.transaction() as book:
            self.assertEqual(len(book['fills']),2)
            self.assertEqual(book['positions']['US.A']['qty'],10)

    def test_late_fill_after_terminal_requires_reconciliation(self):
        f=self.fixture;item=self.pending()
        service=ExecutionService(None,f.config,f.store,True,f.registry)
        service.apply_report(item['id'],dict(order_status='CANCELLED_PART',dealt_qty=10,dealt_avg_price=101))
        # 终结订单新增成交 → 待核对（新契约：返回 reconciling，不静默改写）
        result = service.apply_report(item['id'],dict(order_status='FILLED_PART',dealt_qty=12,dealt_avg_price=101))
        self.assertEqual(result, 'reconciling')
        with f.registry.transaction() as book:
            self.assertEqual(book['orders'][item['id']]['status'], 'reconciling')
            self.assertEqual(book['orders'][item['id']]['filled_qty'], 10)  # 未静默改写
        # 重复差异回报不能绕过保护更新经济数据
        result2 = service.apply_report(item['id'],dict(order_status='FILLED_PART',dealt_qty=12,dealt_avg_price=101))
        self.assertEqual(result2, 'reconciling')
        with f.registry.transaction() as book:
            self.assertEqual(book['orders'][item['id']]['filled_qty'], 10)  # 仍 10
        # 只有经过校验的更正才能解除并应用
        service.apply_correction(item['id'], {'correction_id':'c1','reason':'券商更正新增成交',
                                              'dealt_qty':12,'dealt_avg_price':101})
        with f.registry.transaction() as book:
            self.assertEqual(book['orders'][item['id']]['filled_qty'], 12)

    def test_concurrent_migration_retains_pristine_backup(self):
        f=self.fixture;path=f.path/'old.db'
        original=json.dumps({'positions':{'US.A':{'qty':5,'entry_price':100}},'orders':{}})
        with sqlite3.connect(path) as con:
            con.execute('CREATE TABLE books(namespace TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            con.execute('INSERT INTO books VALUES (?,?)',('DRY-RUN',original))
        command=[sys.executable,'-c',
                 'import sys; from scripts.live_trading.position_registry import PositionRegistry; assert PositionRegistry(sys.argv[1],"DRY-RUN").count()==1',str(path)]
        processes=[subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(3)]
        for proc in processes:
            _,err=proc.communicate(timeout=30)
            self.assertEqual(proc.returncode,0,err.decode())
        with sqlite3.connect(str(path)+'.pre-decision-v1.bak') as con:
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='decision_schema'").fetchone())
            self.assertEqual(con.execute('SELECT payload FROM books').fetchone()[0],original)
        with sqlite3.connect(path) as con:
            self.assertEqual(con.execute('SELECT payload FROM books').fetchone()[0],original)
            self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0],'ok')

    def test_structured_approval_response_loss_restart_only_queries(self):
        from tests.unit.live.test_broker_execution import FakeBroker
        f=self.fixture;broker=FakeBroker();broker.fail=True
        f.registry.configure('DRY-RUN')
        # A separate explicit test account; never connect to OpenD.
        registry=PositionRegistry(f.path/'simulation.db','SIMULATE:123')
        f.registry=registry
        f.store=ProposalStore(log_dir=f.path,registry=registry)
        f.config['live_manager']={'trd_env':'SIMULATE','acc_id':123}
        f.owner.approval_store=f.store;f.owner.dry_run=False;f.owner.pool=broker
        item=f.approve(f.review(f.proposal()))
        service=ExecutionService(broker,f.config,f.store,registry=registry)
        with patch.object(service,'fresh_quote',return_value=(100,100000,time.time())), patch(
                'scripts.live_trading.execution.datetime') as clock:
            clock.now.side_effect=lambda tz: datetime(2026,9,8,10,tzinfo=tz)
            with self.assertRaises(TimeoutError):service.submit(item,100)
        restored=ProposalStore(log_dir=f.path,registry=PositionRegistry(registry.path,'SIMULATE:123'))
        restarted=ExecutionService(broker,f.config,restored,registry=restored.registry)
        restarted.reconcile();restarted.reconcile()
        self.assertEqual(broker.calls,1)
        self.assertEqual(restored.get(item['id'])['status'],'submitted')
        self.assertFalse(restored.approve(item['id']))
