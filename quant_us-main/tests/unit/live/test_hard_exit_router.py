"""PR1 Hard Exit Router 回归（无券商/无 LLM 依赖，DRY-RUN）。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.live_trading.execution import ExecutionService
from scripts.live_trading.hard_exit_router import HardExitRouter, classify_exit_reason
from scripts.live_trading.position_registry import PositionRegistry

CFG = {'risk_budget': {'per_trade': .0025, 'total': .015,
                       'group_limits': {'semis': .0075},
                       'code_groups': {'US.A': 'semis'}, 'dry_run_equity': 100000}}


class _Store:
    def __init__(self):
        self.store = None


class HardExitRouterContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch('scripts.live_trading.approval.proposal_store.ProposalStore._record_ledger').start()
        self.addCleanup(patch.stopall)
        self.registry = PositionRegistry(Path(self.tmp.name) / 'state.db', 'DRY-RUN')
        self.execution = ExecutionService(None, CFG, None, True, self.registry)
        self.router = HardExitRouter(registry=self.registry, execution=self.execution)

    def test_classify_reasons(self):
        self.assertEqual(classify_exit_reason('fixed_stop'), 'hard_risk')
        self.assertEqual(classify_exit_reason('trailing_stop'), 'hard_risk')
        self.assertEqual(classify_exit_reason('卖出|fixed_stop'), 'hard_risk')  # 包装前缀
        self.assertEqual(classify_exit_reason('portfolio_breaker'), 'hard_risk')
        self.assertEqual(classify_exit_reason('broker_risk'), 'hard_risk')
        # 监控器同义码（验收项③）
        self.assertEqual(classify_exit_reason('HARD_STOP'), 'hard_risk')
        self.assertEqual(classify_exit_reason('TRAILING_EXIT'), 'hard_risk')
        self.assertEqual(classify_exit_reason('thesis_invalidated'), 'thesis')
        self.assertEqual(classify_exit_reason('time_exit'), 'scheduled_review')
        self.assertEqual(classify_exit_reason('unknown_reason'), 'thesis')  # 未知 → thesis 保守

    def test_time_exit_can_be_hard_via_config(self):
        cfg = {'hard_exit': {'time_exit_is_hard': True}}
        self.assertEqual(classify_exit_reason('time_exit', cfg), 'hard_risk')

    def test_hard_exit_fills_position_dry_run(self):
        self.registry.open('US.A', 'dip_buy', 10, 100,
                           trade_id='t1', initial_stop=95)
        res = self.router.submit(trade_id='t1', code='US.A', reason='fixed_stop',
                                 market_price=80, dry_run=True)
        self.assertEqual(res['status'], 'filled')
        self.assertEqual(res['fill_price'], 80.0)  # 用可成交价，不虚拟按止损价
        # 持仓已被冲正
        self.assertIsNone(self.registry.get('US.A'))

    def test_hard_exit_idempotent(self):
        self.registry.open('US.A', 'dip_buy', 10, 100, trade_id='t1', initial_stop=95)
        r1 = self.router.submit(trade_id='t1', code='US.A', reason='fixed_stop',
                                market_price=80, dry_run=True)
        self.assertEqual(r1['status'], 'filled')
        # 第二次（持仓已无）：no_position
        r2 = self.router.submit(trade_id='t1', code='US.A', reason='fixed_stop',
                                market_price=80, dry_run=True)
        self.assertEqual(r2['status'], 'no_position')

    def test_active_sell_blocks_oversell(self):
        self.registry.open('US.A', 'dip_buy', 10, 100, trade_id='t1', initial_stop=95)
        # 先人工造一个活跃卖单
        with self.registry.transaction() as book:
            book['orders']['sell1'] = dict(id='sell1', code='US.A', side='sell', qty=10,
                                           status='submitted', filled_qty=0,
                                           trade_id='t1', price=95,
                                           metadata={'direction': 'long'})
        res = self.router.submit(trade_id='t1', code='US.A', reason='fixed_stop',
                                 market_price=80, dry_run=True)
        self.assertEqual(res['status'], 'active_sell_exists')

    def test_no_position_returns_early(self):
        res = self.router.submit(trade_id='t_x', code='US.NOPE', reason='fixed_stop',
                                 market_price=80, dry_run=True)
        self.assertEqual(res['status'], 'no_position')

    def test_empty_trade_id_is_risk_anomaly(self):
        self.registry.open('US.A', 'dip_buy', 10, 100, trade_id='t1', initial_stop=95)
        res = self.router.submit(trade_id='', code='US.A', reason='fixed_stop',
                                 market_price=80, dry_run=True)
        self.assertEqual(res['status'], 'risk_anomaly')
        self.assertEqual(res['reason'], 'empty_trade_id')
        self.assertIsNotNone(self.registry.get('US.A'))  # 未用空值冲正

    def test_trade_id_mismatch_is_risk_anomaly(self):
        self.registry.open('US.A', 'dip_buy', 10, 100, trade_id='t1', initial_stop=95)
        res = self.router.submit(trade_id='t_wrong', code='US.A', reason='fixed_stop',
                                 market_price=80, dry_run=True)
        self.assertEqual(res['status'], 'risk_anomaly')
        self.assertEqual(res['reason'], 'trade_id_mismatch')
        self.assertIsNotNone(self.registry.get('US.A'))


class ExecutionStateMachineTests(unittest.TestCase):
    """硬退出「止损触发 → 成交/拒绝」状态机的确定性测试（DRY-RUN，无 LLM 依赖）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch('scripts.live_trading.approval.proposal_store.ProposalStore._record_ledger').start()
        self.addCleanup(patch.stopall)
        self.registry = PositionRegistry(Path(self.tmp.name) / 'state.db', 'DRY-RUN')
        self.execution = ExecutionService(None, CFG, None, True, self.registry)
        self.router = HardExitRouter(registry=self.registry, execution=self.execution)

    def _submit_without_fill(self, code='US.A'):
        """提交硬退出但拦截自动全额成交，让订单停留在 submitting，便于逐报驱动状态机。"""
        self.registry.open(code, 'dip_buy', 10, 100, trade_id='t1', initial_stop=95)
        with patch.object(self.execution, 'apply_report'):
            self.router.submit(trade_id='t1', code=code, reason='fixed_stop',
                               market_price=80, dry_run=True)
        with self.registry.transaction() as book:
            orders = [o for o in book['orders'].values() if o.get('status') == 'submitting']
        self.assertEqual(len(orders), 1)
        return orders[0]

    def test_stop_trigger_does_not_immediately_fill(self):
        order = self._submit_without_fill()
        self.assertEqual(order['status'], 'submitting')
        self.assertEqual(order['filled_qty'], 0)
        self.assertEqual(self.registry.get('US.A')['qty'], 10)  # 触发了止损 ≠ 已成交

    def test_partial_fill_then_complete_reconciles_position(self):
        order = self._submit_without_fill()
        # 部分成交 5/10
        self.execution.apply_report(order['id'], dict(
            order_id='b1', order_status='PARTIALLY_FILLED',
            dealt_qty=5, dealt_avg_price=80, cumulative_fee=0))
        with self.registry.transaction() as book:
            self.assertEqual(book['orders'][order['id']]['status'], 'partially_filled')
            self.assertEqual(book['orders'][order['id']]['filled_qty'], 5)
        self.assertEqual(self.registry.get('US.A')['qty'], 5)  # 持仓减到 5
        # 补齐成交 10/10
        self.execution.apply_report(order['id'], dict(
            order_id='b1', order_status='FILLED_ALL',
            dealt_qty=10, dealt_avg_price=80, cumulative_fee=0))
        self.assertIsNone(self.registry.get('US.A'))  # 持仓归零

    def test_rejected_preserves_position(self):
        order = self._submit_without_fill()
        self.execution.apply_report(order['id'], dict(
            order_id='b1', order_status='FAILED',
            dealt_qty=0, dealt_avg_price=0, cumulative_fee=0))
        with self.registry.transaction() as book:
            self.assertEqual(book['orders'][order['id']]['status'], 'rejected')
        self.assertEqual(self.registry.get('US.A')['qty'], 10)  # 拒绝保留持仓

    def test_rebind_trade_id_avoids_duplicate_on_rebuy(self):
        # 同一 code 两次不同 trade：exit_id 应绑定 trade_id，第二次不误判 duplicate
        self.registry.open('US.A', 'dip_buy', 10, 100, trade_id='t1', initial_stop=95)
        r1 = self.router.submit(trade_id='t1', code='US.A', reason='fixed_stop',
                                market_price=80, dry_run=True)
        self.assertEqual(r1['status'], 'filled')
        # 重新买入（新 trade_id）
        self.registry.open('US.A', 'dip_buy', 10, 90, trade_id='t2', initial_stop=85)
        r2 = self.router.submit(trade_id='t2', code='US.A', reason='fixed_stop',
                                market_price=70, dry_run=True)
        self.assertEqual(r2['status'], 'filled')  # 不被 t1 的旧订单误判 duplicate


if __name__ == '__main__':
    unittest.main()
