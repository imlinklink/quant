# -*- coding: utf-8 -*-
"""港股持仓重启恢复：bottom_fish 标签（entry_mode/anchor_low/structure_stop）
必须从 trading_state 恢复，否则结构止损/时间止损在重启后空转。"""
import unittest

from scripts.live_trading.hk_position_manager import HKPositionManager


class _FakeStorage:
    def __init__(self, trades=None):
        self.trades = trades or []

    def get_trades(self, env=None):
        return self.trades


class _FakeState:
    def __init__(self, state, trades=None):
        self.state = state
        self.storage = _FakeStorage(trades)

    @property
    def yaml_storage(self):
        return self.storage

    def load_state(self):
        return self.state

    def get_positions(self):
        return []


class _FakeTrader:
    def __init__(self, positions):
        self.positions = positions

    def get_positions(self):
        return self.positions


def _make_manager(state, trades, futu_positions):
    config = {
        'trading': {'env': 'SIMULATE', 'live_trading': {}},
        'strategy': {'initial_capital': 300000.0},
        'risk': {},
    }
    pm = HKPositionManager(
        config=config,
        trader=_FakeTrader(futu_positions),
        state_persistence=_FakeState(state, trades),
        price_fetcher=None,
    )
    pm._load_positions_from_db()
    return pm


class TestRestoreBottomFishTags(unittest.TestCase):
    def test_strategy_position_restores_tags(self):
        state = {
            'cooldowns': {},
            'positions': {
                'HK.00700': {
                    'quantity': 100,
                    'cost_price': 400.0,
                    'highest_price': 450.0,
                    'buy_time': '2026-02-10T10:00:00',
                    'entry_mode': 'bottom_fish',
                    'anchor_low': 402.1,
                    'structure_stop': 398.5,
                    'proposal_id': 'p123',
                    'manual': False,
                },
            },
        }
        pm = _make_manager(
            state,
            trades=[{'stock_code': 'HK.00700', 'trade_type': 'BUY'}],
            futu_positions=[{
                'stock_code': 'HK.00700', 'quantity': 100, 'cost_price': 400.0,
            }],
        )
        pos = pm.strategy_positions['HK.00700']
        self.assertFalse(pos.get('manual'))
        self.assertEqual(pos.get('entry_mode'), 'bottom_fish')
        self.assertEqual(pos.get('structure_stop'), 398.5)
        self.assertEqual(pos.get('anchor_low'), 402.1)
        self.assertEqual(pos.get('proposal_id'), 'p123')
        self.assertAlmostEqual(pm.strategy_used_capital, 100 * 400.0)

    def test_manual_position_not_tagged_as_strategy(self):
        # trading_state 无记录、trades 也无 BUY → Futu 持仓视为手动买入
        pm = _make_manager(
            {'cooldowns': {}, 'positions': {}},
            trades=[],
            futu_positions=[{
                'stock_code': 'HK.00700', 'quantity': 100, 'cost_price': 400.0,
            }],
        )
        pos = pm.strategy_positions['HK.00700']
        self.assertTrue(pos.get('manual'))
        self.assertEqual(pos.get('structure_stop'), 0.0)
        self.assertEqual(pos.get('entry_mode'), '')
        self.assertAlmostEqual(pm.strategy_used_capital, 0.0)


if __name__ == '__main__':
    unittest.main()
