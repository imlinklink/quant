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


if __name__ == '__main__':
    unittest.main()
