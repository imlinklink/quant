"""富途财报 -> 诊断层证据适配器测试。"""
import unittest

import pandas as pd

from scripts.evidence.import_earnings_from_futu import evidence_rows_from_price_move


def frame():
    return pd.DataFrame([
        {'fiscal_year': 2027, 'financial_type': 'Q2', 'period_text': '2027/Q2',
         'pub_trading_day': 1787716800, 'pub_trading_day_str': '2026-08-26',
         'pub_type': 'AFTER_MARKET', 'day_offset': 0},
        {'fiscal_year': 2027, 'financial_type': 'Q2', 'period_text': '2027/Q2',
         'pub_trading_day': 1787716800, 'pub_trading_day_str': '2026-08-26',
         'pub_type': 'AFTER_MARKET', 'day_offset': 1},      # 同期重复行应去重
        {'fiscal_year': 2027, 'financial_type': 'Q1', 'period_text': '2027/Q1',
         'pub_trading_day': 1774627200, 'pub_trading_day_str': '2026-03-25',
         'pub_type': 'BEFORE', 'day_offset': 0},
    ])


class EarningsEvidenceTests(unittest.TestCase):
    def test_rows_are_diagnostic_layer_only(self):
        rows = evidence_rows_from_price_move('US.NVDA', frame())
        self.assertEqual(len(rows), 2)                      # 去重后每期一条
        self.assertTrue(all(r['observed_at'] is None for r in rows))   # 不伪造首次可见时间
        self.assertTrue(all(r['quality_status'] == 'unverified' for r in rows))
        self.assertEqual(rows[0]['security_id'], 'SEC-US-NVDA')
        self.assertEqual(rows[0]['kind'], 'earnings')

    def test_publication_time_is_conservative(self):
        rows = {r['source_record_id']: r for r in evidence_rows_from_price_move('US.NVDA', frame())}
        # 盘后 → 当日收盘 16:00 ET；盘前 → 当日开盘 09:30 ET
        self.assertEqual(rows['US.NVDA|2026-08-26']['published_at'], '2026-08-26T20:00:00+00:00')
        self.assertEqual(rows['US.NVDA|2026-03-25']['published_at'], '2026-03-25T13:30:00+00:00')

    def test_empty_input(self):
        self.assertEqual(evidence_rows_from_price_move('US.X', pd.DataFrame()), [])


if __name__ == '__main__':
    unittest.main()
