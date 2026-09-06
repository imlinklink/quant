# -*- coding: utf-8 -*-
"""大盘/指数门（板块代理）状态机与聚类映射单测。"""
import unittest

from scripts.live_trading.index_gate import (
    DipIndexGate,
    compute_index_state,
)


class TestComputeIndexState(unittest.TestCase):
    def _closes(self, n=60, val=100.0):
        return [float(val)] * n

    def test_above_ma20_normal(self):
        st = compute_index_state(self._closes(), 101.0)
        self.assertTrue(st['ok'])
        self.assertFalse(st['below_ma20'])
        self.assertEqual(st['action'], '')

    def test_below_ma20_stricter(self):
        # 昨收 100、MA20=100，现价 99.5 → 跌 -0.5%，弱势但未急跌 → stricter
        st = compute_index_state(self._closes(), 99.5)
        self.assertTrue(st['ok'])
        self.assertTrue(st['below_ma20'])
        self.assertEqual(st['action'], 'stricter')
        self.assertEqual(st['strict_bonus'], 2)

    def test_below_ma20_sharp_drop_pause(self):
        # 现价 98.5 → 跌 -1.5% ≤ -1% → pause
        st = compute_index_state(self._closes(), 98.5)
        self.assertEqual(st['action'], 'pause')
        self.assertEqual(st['strict_bonus'], 0)

    def test_below_action_pause_no_drop(self):
        st = compute_index_state(self._closes(), 99.9,
                                 below_ma20_action='pause')
        self.assertEqual(st['action'], 'pause')

    def test_insufficient_data_allows(self):
        st = compute_index_state([100.0] * 10, 100.0)
        self.assertFalse(st['ok'])
        self.assertIn('不足', st['reason'])


class TestClusterMapping(unittest.TestCase):
    def test_cluster_for_code(self):
        gate = DipIndexGate({
            'index_gate': {
                'enabled': True,
                'proxies': {'semis': ['US.SOXX'], 'default': ['US.SPY']},
                'code_cluster': {'US.MU': 'semis'},
            },
        })
        self.assertEqual(gate.cluster_for('US.MU'), 'semis')
        self.assertEqual(gate.cluster_for('US.YINN'), 'default')
        self.assertFalse(gate.get_state('US.MU')['ok'])  # pool=None 放行不崩溃


if __name__ == '__main__':
    unittest.main()
