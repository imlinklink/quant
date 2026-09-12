"""富途公司行动直取测试（技术设计 §2.2）。"""
import unittest

from scripts.data.import_corporate_actions_from_futu import (build_actions, cash_from_statement,
                                                             ratio_from_rate)

SPLITS = {'US.NVDA': [
    {'dir_deci_pub_date': 1717992000, 'dir_deci_pub_date_str': '2024-06-10',
     'reform_type': 'Split', 'rate': '1→10'},
    {'dir_deci_pub_date': 1626753600, 'dir_deci_pub_date_str': '2021-07-20',
     'reform_type': 'Split', 'rate': '1→4'},
]}
DIVIDENDS = {'US.AAPL': [
    {'pub_date': '07/31/2026', 'statement': 'Cash Dividend: 0.27 USD Per Share',
     'record_date': '08/10/2026', 'ex_date': '08/10/2026', 'dividend_payable_date': '08/13/2026'},
]}


class FutuCorporateActionsTests(unittest.TestCase):
    def test_parsers(self):
        self.assertAlmostEqual(ratio_from_rate('1→10'), 10.0)
        self.assertAlmostEqual(ratio_from_rate('2→3'), 1.5)
        self.assertIsNone(ratio_from_rate('bad'))
        self.assertAlmostEqual(cash_from_statement('Cash Dividend: 0.27 USD Per Share'), 0.27)
        self.assertIsNone(cash_from_statement('no cash here'))

    def test_build_actions_from_real_shapes(self):
        actions = build_actions(SPLITS, DIVIDENDS)
        self.assertEqual(len(actions), 3)
        nvda = actions[(actions.security_id == 'SEC-US-NVDA') & (actions.ex_date == '2024-06-10')]
        self.assertEqual(nvda.iloc[0]['action_type'], 'split')
        self.assertAlmostEqual(float(nvda.iloc[0]['ratio']), 10.0)
        aapl = actions[actions.security_id == 'SEC-US-AAPL']
        self.assertEqual(aapl.iloc[0]['action_type'], 'cash_dividend')
        self.assertAlmostEqual(float(aapl.iloc[0]['cash_amount']), 0.27)
        self.assertEqual(aapl.iloc[0]['ex_date'], '2026-08-10')
        self.assertTrue(str(aapl.iloc[0]['source_published_at']).startswith('2026-07-31'))
        # 两类来源不得串号
        self.assertTrue((actions[actions.action_type == 'split']['source_id']
                         == 'futu_corporate_actions_splits').all())
        self.assertTrue((actions[actions.action_type == 'cash_dividend']['source_id']
                         == 'futu_corporate_actions_dividends').all())
        # 富途无 observed_at 证据 → 不得伪造；该列应全空
        self.assertTrue(actions['source_observed_at'].isna().all())
        self.assertTrue(actions['record_hash'].notna().all())

    def test_empty_input(self):
        self.assertTrue(build_actions({}, {}).empty)


if __name__ == '__main__':
    unittest.main()
