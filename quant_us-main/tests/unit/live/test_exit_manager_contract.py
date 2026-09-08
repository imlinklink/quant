import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch,Mock
from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.chandelier_exit_manager import PositionStateManager,ChandelierExitManager


class ExitManagerContract(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.reg=PositionRegistry(Path(self.tmp.name)/'state.db','DRY-RUN')
        p=patch('scripts.live_trading.chandelier_exit_manager.REGISTRY',self.reg);p.start();self.addCleanup(p.stop)
        self.manager=ChandelierExitManager.__new__(ChandelierExitManager)
        self.manager._running=True;self.manager._stop_event=threading.Event()
        self.manager.position_mgr=PositionStateManager({})
        self.manager.atr_cache=Mock();self.manager.atr_cache.get_atr.return_value=None
        self.manager._request_exit=Mock(return_value=False)

    def test_hard_stop_without_atr_only_proposes_and_keeps_state(self):
        self.reg.open('US.A','dip_buy',10,100,initial_stop=95,initial_risk=50,target=110)
        self.manager.position_mgr.sync_positions([dict(code='US.A',average_cost=100,qty=10,position_side='LONG')])
        self.manager._on_tick('US.A',90)
        self.manager._request_exit.assert_called_once_with('US.A',95,'HARD_STOP')
        self.assertEqual(self.manager.position_mgr.strategy.positions['US.A'].stop_line,95)
        self.assertEqual(self.reg.get('US.A')['qty'],10)

    def test_restart_restores_ratchet(self):
        self.reg.open('US.A','manual',10,100)
        positions=[dict(code='US.A',average_cost=100,qty=10,position_side='LONG')]
        self.manager.position_mgr.sync_positions(positions)
        self.manager.position_mgr.register('US.A',2,100)
        self.reg.update('US.A',exit_state=dict(vars(self.manager.position_mgr.strategy.positions['US.A']),stop_line=108,highest_price=115))
        fresh=PositionStateManager({});fresh.sync_positions(positions);fresh.register('US.A',2,105)
        self.assertEqual(fresh.strategy.positions['US.A'].stop_line,108)
        self.assertEqual(fresh.strategy.positions['US.A'].highest_price,115)

    def test_no_approval_store_does_not_close(self):
        self.manager.sell_approval_enabled_flag=True;self.manager.approval_store=None
        self.assertFalse(ChandelierExitManager._request_exit(self.manager,'US.A',95,'HARD_STOP'))
