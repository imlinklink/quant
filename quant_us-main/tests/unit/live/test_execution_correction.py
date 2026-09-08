"""成交更正与费用补齐（任务 D）回归：待核对、更正幂等、R0 冻结、费用修订、脱敏样例。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.live_trading.approval.proposal_store import ProposalStore
from scripts.live_trading.execution import ExecutionService, record_broker_sample
from scripts.live_trading.position_registry import PositionRegistry

CFG = {'risk_budget': {'per_trade': .0025, 'total': .015,
                       'group_limits': {'semis': .0075},
                       'code_groups': {'US.A': 'semis'}, 'dry_run_equity': 100000}}
REVIEW = {'verdict': 'allow', 'reason': '测试评估已完成'}


class ExecutionCorrection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch.object(ProposalStore, '_record_ledger').start()
        self.addCleanup(patch.object(ProposalStore, '_record_ledger').stop)
        self.store = ProposalStore(log_dir=self.tmp.name)
        self.registry = PositionRegistry(Path(self.tmp.name) / 'state.db')
        self.service = ExecutionService(None, CFG, self.store, True, self.registry)

    def pending_order(self, qty=50):
        p = self.store.create(stock_code='US.A', side='buy', price=100, quantity=qty,
                              llm=REVIEW, entry_mode='donchian', trade_plan={'initial_stop': 95})
        self.store.approve(p['id'])
        self.store.mark(p['id'], 'executing')
        with self.registry.transaction() as b:
            b['orders'][p['id']] = dict(id=p['id'], code='US.A', side='buy', qty=qty, price=100,
                                        status='submitted', risk=250, filled_qty=0, cost_per_share=.05,
                                        metadata={'initial_stop': 95, 'risk_group': 'semis', 'entry_mode': 'donchian'})
        return p['id']

    def test_quantity_regression_flags_reconciling_not_silent(self):
        pid = self.pending_order()
        self.service.apply_report(pid, {'order_id': '1', 'order_status': 'FILLED_PART',
                                        'dealt_qty': 20, 'dealt_avg_price': 101})
        result = self.service.apply_report(pid, {'order_id': '1', 'order_status': 'FILLED_PART',
                                                 'dealt_qty': 10, 'dealt_avg_price': 101})
        self.assertEqual(result, 'reconciling')
        with self.registry.transaction() as b:
            self.assertEqual(b['orders'][pid]['status'], 'reconciling')
            self.assertEqual(b['orders'][pid]['filled_qty'], 20)  # 未静默改写

    def test_terminal_new_fill_flags_then_correction_applies(self):
        pid = self.pending_order()
        self.service.apply_report(pid, {'order_id': '1', 'order_status': 'FILLED_PART',
                                        'dealt_qty': 20, 'dealt_avg_price': 101})
        self.service.apply_report(pid, {'order_id': '1', 'order_status': 'CANCELLED_PART',
                                        'dealt_qty': 20, 'dealt_avg_price': 101})
        with self.registry.transaction() as b:
            self.assertIn(b['orders'][pid]['status'], ('cancelled',))
        # 终结后券商更正新增成交（20 → 30）
        result = self.service.apply_report(pid, {'order_id': '1', 'order_status': 'FILLED_ALL',
                                                 'dealt_qty': 30, 'dealt_avg_price': 102})
        self.assertEqual(result, 'reconciling')
        status = self.service.apply_correction(pid, {
            'correction_id': 'c1', 'reason': '券商更正新增成交',
            'dealt_qty': 30, 'dealt_avg_price': 102,
        })
        self.assertEqual(status, 'partially_filled')
        with self.registry.transaction() as b:
            self.assertEqual(b['orders'][pid]['filled_qty'], 30)

    def test_fee_fill_up_down_and_refund(self):
        pid = self.pending_order()
        self.service.apply_report(pid, {'order_id': '1', 'order_status': 'FILLED_ALL',
                                        'dealt_qty': 50, 'dealt_avg_price': 100})
        with self.registry.transaction() as b:
            self.assertIsNone(b['orders'][pid].get('cumulative_fee'))  # 费用缺失保持未知
        # 补齐
        self.service.apply_correction(pid, {'correction_id': 'f1', 'reason': '补齐费用', 'cumulative_fee': 2.5})
        # 上调
        self.service.apply_correction(pid, {'correction_id': 'f2', 'reason': '费用上调', 'cumulative_fee': 3.0})
        # 退款（下调）
        self.service.apply_correction(pid, {'correction_id': 'f3', 'reason': '费用退款', 'cumulative_fee': 2.0})
        with self.registry.transaction() as b:
            self.assertEqual(b['orders'][pid]['cumulative_fee'], 2.0)
            trade = list(b['trades'].values())[0]
            self.assertEqual(trade['fees'], 2.0)

    def test_correction_idempotent(self):
        pid = self.pending_order()
        self.service.apply_report(pid, {'order_id': '1', 'order_status': 'FILLED_ALL',
                                        'dealt_qty': 50, 'dealt_avg_price': 100})
        self.service.apply_correction(pid, {'correction_id': 'f1', 'reason': '费用', 'cumulative_fee': 2.5})
        self.service.apply_correction(pid, {'correction_id': 'f1', 'reason': '费用', 'cumulative_fee': 2.5})
        with self.registry.transaction() as b:
            self.assertEqual(b['orders'][pid]['cumulative_fee'], 2.5)
            self.assertEqual(b['orders'][pid]['fee_revision'], 1)

    def test_r0_not_rewritten_by_correction(self):
        pid = self.pending_order()
        self.service.apply_report(pid, {'order_id': '1', 'order_status': 'FILLED_ALL',
                                        'dealt_qty': 50, 'dealt_avg_price': 100, 'cumulative_fee': 2.5})
        with self.registry.transaction() as b:
            r0 = list(b['trades'].values())[0]['initial_r0']
            self.assertIsNotNone(r0)
        self.service.apply_correction(pid, {'correction_id': 'f1', 'reason': '费用上调', 'cumulative_fee': 3.0})
        with self.registry.transaction() as b:
            trade = list(b['trades'].values())[0]
            self.assertEqual(trade['initial_r0'], r0)  # R0 冻结不变

    def test_broker_sample_sanitized(self):
        out = record_broker_sample(
            {'order_id': 'x', 'dealt_qty': 10, 'api_key': 'sk-secret', 'authorization': 'Bearer t'},
            path=Path(self.tmp.name) / 'samples.jsonl')
        self.assertNotIn('api_key', out)
        self.assertNotIn('authorization', out)
        self.assertEqual(out.get('order_id'), 'str')
        self.assertEqual(out.get('dealt_qty'), 'int')


if __name__ == '__main__':
    unittest.main()
