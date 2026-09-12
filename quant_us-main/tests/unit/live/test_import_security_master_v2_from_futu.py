"""富途 → v2 主数据转换测试（范围：当前存续样本，不含退市）。"""
import unittest

import pandas as pd

from scripts.data.import_security_master_v2_from_futu import (actions_from_klines,
                                                              master_from_basicinfo,
                                                              security_id_for)
from scripts.data.security_master_v2 import validate_master, validate_symbols

BASIC = pd.DataFrame([
    {'code': 'US.SPY', 'name': 'SPDR S&P 500 ETF', 'stock_type': 'ETF',
     'exchange_type': 'NYSE', 'listing_date': '1993-01-29', 'lot_size': 1},
    {'code': 'US.AAPL', 'name': 'Apple Inc', 'stock_type': 'STOCK',
     'exchange_type': 'NASDAQ', 'listing_date': '1980-12-12', 'lot_size': 1},
    {'code': 'US.SOXL', 'name': 'ProShares UltraPro 3x Semiconductor', 'stock_type': 'ETF',
     'exchange_type': 'NYSE', 'listing_date': '2008-11-05', 'lot_size': 1},
    {'code': 'US.NEWCO', 'name': 'New Co', 'stock_type': 'STOCK',
     'exchange_type': 'NASDAQ', 'listing_date': '', 'lot_size': 1},
])
CODES = ['US.SPY', 'US.AAPL', 'US.SOXL', 'US.NEWCO']


class FutuMasterTests(unittest.TestCase):
    def test_security_id_is_deterministic(self):
        self.assertEqual(security_id_for('aapl'), 'SEC-US-AAPL')
        self.assertEqual(security_id_for('US.AAPL'), security_id_for('aapl'))

    def test_master_and_symbol_rows(self):
        master, symbols = master_from_basicinfo(BASIC, CODES, as_of='2026-09-12')
        self.assertEqual(len(master), 4)
        self.assertEqual(len(symbols), 4)
        self.assertEqual(validate_master(master), [])
        self.assertEqual(validate_symbols(symbols), [])
        by_id = master.set_index('security_id')
        self.assertEqual(by_id.loc['SEC-US-AAPL', 'asset_type'], 'stock')
        self.assertEqual(by_id.loc['SEC-US-SPY', 'asset_type'], 'etf')
        self.assertEqual(by_id.loc['SEC-US-SOXL', 'asset_type'], 'leveraged_etf')
        self.assertEqual(by_id.loc['SEC-US-AAPL', 'listed_at'], '1980-12-12')
        # 范围：当前存续，退市日一律留空
        self.assertTrue((master['delisted_at'].fillna('') == '').all())
        # observed_at 不可证 → unverified
        self.assertEqual(by_id.loc['SEC-US-AAPL', 'quality_status'], 'unverified')
        # symbol 映射
        self.assertEqual(set(symbols['symbol']), set(CODES))

    def test_missing_listing_marked(self):
        master, _ = master_from_basicinfo(BASIC, CODES, as_of='2026-09-12')
        row = master.set_index('security_id').loc['SEC-US-NEWCO']
        self.assertEqual(row['quality_status'], 'missing_history')
        self.assertEqual(row['valid_from'], '2026-09-12')      # 无上市日时用 as_of 兜底
        self.assertTrue(pd.isna(row['listed_at']))             # 上市日不可知，留空（规范化为 None）

    def test_missing_code_blocked(self):
        with self.assertRaises(ValueError):
            master_from_basicinfo(BASIC, CODES + ['US.GONE'])
        master, _ = master_from_basicinfo(BASIC, CODES + ['US.GONE'], allow_missing=True)
        self.assertEqual(len(master), 4)                        # 只输出富途返回的

    def test_actions_from_klines_concatenates(self):
        sessions = pd.bdate_range('2020-01-06', periods=4)
        raw = pd.DataFrame({'session': sessions, 'close': [100, 100, 50, 50]})
        adj = pd.DataFrame({'session': sessions, 'close': [50, 50, 50, 50]})
        out = actions_from_klines([('SEC-US-AAPL', raw, adj), ('SEC-US-SPY', adj, adj)])
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]['action_type'], 'split')
        self.assertEqual(out.iloc[0]['quality_status'], 'unverified')


if __name__ == '__main__':
    unittest.main()
