"""真实Flask审批路由→执行服务→独立SQLite账本的离线联调。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.live_trading.approval.proposal_store import ProposalStore
from scripts.live_trading.execution import ExecutionService
from scripts.live_trading.position_registry import PositionRegistry


class ShadowLifecycleTests(unittest.TestCase):
    def setUp(self):
        from web import app as webapp
        self.webapp = webapp
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 'lifecycle.db', 'DRY-RUN')
        # 仅隔离legacy全局评估镜像；实际审批/订单/持仓事件仍写独立账本。
        mirror = patch.object(ProposalStore, '_record_ledger')
        mirror.start(); self.addCleanup(mirror.stop)
        self.store = ProposalStore(log_dir=self.tmp.name, registry=self.registry)
        store_patch = patch.object(webapp, 'approval_store', self.store)
        store_patch.start(); self.addCleanup(store_patch.stop)
        self.client = webapp.app.test_client()
        self.config = {'risk_budget': {'dry_run_equity': 100000, 'per_trade': .0025,
                                      'total': .015, 'cost_per_share': .05,
                                      'code_groups': {'US.FIXTURE': 'fixture'},
                                      'group_limits': {'fixture': .0075}}}
        self.service = ExecutionService(None, self.config, self.store, True, self.registry)

    def proposal(self, side='buy', qty=40, price=100):
        return self.store.create(stock_code='US.FIXTURE', side=side, price=price, quantity=qty,
                                 entry_mode='donchian', trade_plan={'initial_stop': 95},
                                 llm={'verdict': 'allow', 'reason': 'offline fixture'})

    def approve_and_execute(self, proposal, price):
        route = f"/api/approvals/{proposal['id']}/approve"
        self.assertEqual(self.client.post(route, json={}).status_code, 200)
        self.assertEqual(self.client.post(route, json={}).status_code, 409)
        self.assertTrue(self.store.mark(proposal['id'], 'executing'))
        item = self.store.get(proposal['id'])
        self.assertEqual(self.service.submit(item, price), 'filled')
        with self.assertRaises(ValueError):
            self.service.submit(item, price)

    def test_approval_buy_restart_partial_sell_and_final_exit(self):
        buy = self.proposal()
        items = self.client.get('/api/approvals').get_json()['items']
        self.assertIn(buy['id'], [i['id'] for i in items])
        self.assertEqual(self.registry.count(), 0)
        self.approve_and_execute(buy, 100)
        self.assertEqual(self.registry.get('US.FIXTURE')['qty'], 40)
        fresh = PositionRegistry(self.registry.path, 'DRY-RUN')
        self.store = ProposalStore(log_dir=self.tmp.name, registry=fresh)
        self.webapp.approval_store = self.store
        self.service = ExecutionService(None, self.config, self.store, True, fresh)
        self.assertEqual(fresh.get('US.FIXTURE')['qty'], 40)
        with fresh.transaction() as book:
            self.assertEqual(book['orders'][buy['id']]['status'], 'filled')
        self.approve_and_execute(self.proposal('sell', 15, 105), 105)
        self.assertEqual(fresh.get('US.FIXTURE')['qty'], 25)
        self.approve_and_execute(self.proposal('sell', 25, 90), 90)
        self.assertIsNone(fresh.get('US.FIXTURE'))
        with fresh.transaction() as book:
            self.assertEqual(len(book['orders']), 3)
            self.assertTrue(all(o['status'] == 'filled' for o in book['orders'].values()))

    def test_rejected_and_expired_proposals_never_open_positions(self):
        rejected = self.proposal()
        self.assertEqual(self.client.post(
            f"/api/approvals/{rejected['id']}/reject", json={}).status_code, 200)
        self.assertEqual(self.client.post(
            f"/api/approvals/{rejected['id']}/approve", json={}).status_code, 409)
        expired = self.store.create(stock_code='US.FIXTURE', side='buy', expires_at=1,
                                    llm={'verdict': 'allow', 'reason': 'fixture'})
        self.assertEqual(self.client.post(
            f"/api/approvals/{expired['id']}/approve", json={}).status_code, 409)
        self.assertEqual(self.registry.count(), 0)
