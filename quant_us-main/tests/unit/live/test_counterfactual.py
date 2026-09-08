"""反事实记录（阶段 H3）+ 稳定原因码（H1）回归。"""
import unittest

import pandas as pd

from scripts.live_trading.decision_ledger.counterfactual import compare, freeze


def _bars(code='US.A', slope=0.01):
    dates = pd.date_range('2026-08-24', periods=20, freq='B', tz='UTC')
    rows = []
    for i, d in enumerate(dates):
        price = 100.0 * (1.0 + slope * i)
        rows.append({'code': code, 'date': d, 'open': price, 'high': price * 1.02,
                     'low': price * 0.98, 'close': price})
    return pd.DataFrame(rows)


class CounterfactualContracts(unittest.TestCase):
    def test_freeze_captures_outcomes(self):
        proposal = {'stock_code': 'US.A', 'entry_mode': 'dip_buy', 'price': 100,
                    'initial_stop': 95, 'target': 110}
        review = {'recommendation': 'support_execute', 'reasons': []}
        rec = freeze(proposal, review, 'approved',
                     {'price': 100, 'observed_at': '2026-09-04'}, _bars(), horizons=(5,))
        self.assertEqual(rec['stock_code'], 'US.A')
        self.assertEqual(rec['human_action'], 'approved')
        self.assertEqual(rec['recommendation'], 'support_execute')
        self.assertTrue(rec['outcomes'][5]['complete'])

    def test_compare_groups(self):
        bars = _bars()
        proposal = {'stock_code': 'US.A', 'entry_mode': 'dip_buy', 'price': 100,
                    'initial_stop': 95, 'target': 110}
        r1 = freeze(proposal, {'recommendation': 'support_execute'}, 'approved',
                    {'price': 100, 'observed_at': '2026-09-04'}, bars, horizons=(5,))
        r2 = freeze(proposal, {'recommendation': 'defer', 'reasons': ['weak_confirmation']},
                    'rejected', {'price': 100, 'observed_at': '2026-09-04'}, bars, horizons=(5,))
        report = compare([r1, r2], horizon=5)
        self.assertIn('approved|support_execute', report)
        self.assertIn('rejected|defer', report)
        self.assertEqual(report['approved|support_execute']['n'], 1)

    def test_compare_skips_incomplete(self):
        bars = _bars()
        rec = freeze({'stock_code': 'US.A', 'entry_mode': 'dip_buy', 'price': 100},
                     None, 'unhandled', {'price': 100, 'observed_at': '2026-09-30'},
                     bars, horizons=(10,))
        self.assertFalse(rec['outcomes'][10]['complete'])
        self.assertEqual(compare([rec], horizon=10), {})

    def test_reasons_field_in_schema(self):
        from jsonschema import validate
        from mutifactor.llm.trade_review import REASONS, REVIEW_SCHEMA
        self.assertEqual(len(REASONS), 6)
        # reasons 是 optional 字段，带 reasons 能过 schema
        raw = dict(status='complete', recommendation='defer', proposed_action='hold',
                   thesis_state='unchanged', reasons=['weak_confirmation'],
                   facts=[], inferences=[], counterevidence=[],
                   missing_information=['缺财报'], plan_change_requested=False,
                   next_review_conditions=[])
        validate(raw, REVIEW_SCHEMA)  # 不抛异常即通过


if __name__ == '__main__':
    unittest.main()
