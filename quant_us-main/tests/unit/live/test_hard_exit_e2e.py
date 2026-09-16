"""验收项③：监控器端到端 DRY-RUN（tick → 路由 → 执行器 → 回报）。

验证真实链路：ChandelierExitManager._on_tick 命中硬止损 → HardExitRouter.submit →
ExecutionService.submit_system_exit → apply_report → 持仓冲正；硬止损不经确认台。
"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.live_trading.chandelier_exit_manager import ChandelierExitManager, PositionStateManager
from scripts.live_trading.execution import ExecutionService
from scripts.live_trading.position_registry import PositionRegistry

CFG = {'risk_budget': {'per_trade': .0025, 'total': .015,
                       'group_limits': {'semis': .0075},
                       'code_groups': {'US.A': 'semis'}, 'dry_run_equity': 100000},
       'hard_exit': {'enabled': True}}


class HardExitEndToEndTests(unittest.TestCase):
    """tick → 硬止损 → 成交/对账 的端到端 DRY-RUN。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.reg = PositionRegistry(Path(self.tmp.name) / 'state.db', 'DRY-RUN')
        patch('scripts.live_trading.chandelier_exit_manager.REGISTRY', self.reg).start()
        self.addCleanup(patch.stopall)
        patch('scripts.live_trading.approval.proposal_store.ProposalStore._record_ledger').start()
        self.execution = ExecutionService(None, CFG, None, True, self.reg)
        # 轻量构造 ChandelierExitManager（复用 __new__，绕过重量 __init__）
        self.manager = ChandelierExitManager.__new__(ChandelierExitManager)
        self.manager._running = True
        self.manager._stop_event = threading.Event()
        self.manager.config = CFG
        self.manager.dry_run = True
        self.manager._execution_service = self.execution
        self.manager.position_mgr = PositionStateManager({})
        self.manager.atr_cache = Mock()
        self.manager.atr_cache.get_atr.return_value = None
        self.manager._request_exit = Mock(return_value=False)

    def _open_and_sync(self, code='US.A', trade_id='t1', initial_stop=95):
        self.reg.open(code, 'dip_buy', 10, 100, trade_id=trade_id, initial_stop=initial_stop,
                      initial_risk=50, target=110)
        self.manager.position_mgr.sync_positions(
            [dict(code=code, average_cost=100, qty=10, position_side='LONG')])

    def test_hard_stop_tick_fills_via_hard_exit_not_confirm_desk(self):
        self._open_and_sync()
        self.manager._on_tick('US.A', 90)  # 触发硬止损（initial_stop=95）
        # 不经确认台
        self.manager._request_exit.assert_not_called()
        # 持仓已被冲正
        self.assertIsNone(self.reg.get('US.A'))

    def test_partial_fill_then_complete_reconciles(self):
        self._open_and_sync()
        with patch.object(self.execution, 'apply_report'):
            self.manager._on_tick('US.A', 90)
        # 拦截自动成交后，订单应停在 submitting（触发止损 ≠ 已成交）
        with self.reg.transaction() as book:
            orders = [o for o in book['orders'].values() if o.get('status') == 'submitting']
        self.assertEqual(len(orders), 1)
        self.assertEqual(self.reg.get('US.A')['qty'], 10)
        oid = orders[0]['id']
        # 部分成交 5/10 → 持仓减到 5
        self.execution.apply_report(oid, dict(order_id='b1', order_status='PARTIALLY_FILLED',
                                              dealt_qty=5, dealt_avg_price=90, cumulative_fee=0))
        self.assertEqual(self.reg.get('US.A')['qty'], 5)
        # 补齐 10/10 → 持仓归零
        self.execution.apply_report(oid, dict(order_id='b1', order_status='FILLED_ALL',
                                              dealt_qty=10, dealt_avg_price=90, cumulative_fee=0))
        self.assertIsNone(self.reg.get('US.A'))

    def test_rejected_preserves_position(self):
        self._open_and_sync()
        with patch.object(self.execution, 'apply_report'):
            self.manager._on_tick('US.A', 90)
        with self.reg.transaction() as book:
            orders = [o for o in book['orders'].values() if o.get('status') == 'submitting']
        oid = orders[0]['id']
        self.execution.apply_report(oid, dict(order_id='b1', order_status='FAILED',
                                              dealt_qty=0, dealt_avg_price=0, cumulative_fee=0))
        self.assertEqual(self.reg.get('US.A')['qty'], 10)  # 拒绝保留持仓


if __name__ == '__main__':
    unittest.main()
