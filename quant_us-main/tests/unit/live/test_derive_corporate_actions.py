"""公司行动反推测试（富途路线：QFQ 与不复权价差）。"""
import unittest

import numpy as np
import pandas as pd

from scripts.data.derive_corporate_actions import apply_adjustments, derive_actions

SESSIONS = pd.bdate_range('2020-01-06', periods=5)


def frame(closes):
    return pd.DataFrame({'session': SESSIONS, 'close': closes})


class DeriveActionsTests(unittest.TestCase):
    def test_split_detected(self):
        raw = frame([100, 100, 100, 50, 50])          # 2:1 拆股在第 4 日
        adj = frame([50, 50, 50, 50, 50])             # 前复权
        actions = derive_actions(raw, adj, 'SEC-A')
        self.assertEqual(len(actions), 1)
        row = actions.iloc[0]
        self.assertEqual(row['action_type'], 'split')
        self.assertAlmostEqual(row['ratio'], 2.0)
        self.assertEqual(row['ex_date'], SESSIONS[3].strftime('%Y-%m-%d'))
        self.assertEqual(row['quality_status'], 'unverified')

    def test_reverse_split_detected(self):
        raw = frame([50, 50, 50, 100, 100])           # 1:2 反向拆股
        adj = frame([100, 100, 100, 100, 100])
        actions = derive_actions(raw, adj, 'SEC-A')
        self.assertEqual(actions.iloc[0]['action_type'], 'reverse_split')
        self.assertAlmostEqual(actions.iloc[0]['ratio'], 0.5)

    def test_cash_dividend_detected(self):
        raw = frame([100, 100, 100, 100, 100])
        adj = frame([98, 98, 98, 100, 100])           # 除息 2 美元
        actions = derive_actions(raw, adj, 'SEC-A')
        row = actions.iloc[0]
        self.assertEqual(row['action_type'], 'cash_dividend')
        self.assertAlmostEqual(row['cash_amount'], 2.0, places=4)

    def test_no_action_returns_empty(self):
        flat = frame([100, 100, 100, 100, 100])
        self.assertTrue(derive_actions(flat, flat, 'SEC-A').empty)

    def test_apply_adjustments_round_trip(self):
        raw = frame([100, 100, 100, 50, 50])
        adj = frame([50, 50, 50, 50, 50])
        actions = derive_actions(raw, adj, 'SEC-A')
        rebuilt = apply_adjustments(raw, actions)
        self.assertTrue(np.allclose(rebuilt['close'].to_numpy(float), adj['close'].to_numpy(float)))


if __name__ == '__main__':
    unittest.main()
