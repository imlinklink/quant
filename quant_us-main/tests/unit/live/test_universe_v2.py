"""历史时点 universe v2 契约测试（技术设计 §2.5/§2.6；范围：固定存续普通股）。"""
import unittest

import pandas as pd

from scripts.historical_universe import (build_point_in_time_universe_v2,
                                         universe_reason_summary)

SESSIONS = pd.bdate_range('2016-01-04', periods=4)   # 04,05,06,07

MASTER = pd.DataFrame([
    {'security_id': 'SEC-000001', 'asset_type': 'stock', 'listed_at': '2016-01-04',
     'quality_status': 'verified'},                                   # 改名
    {'security_id': 'SEC-000002', 'asset_type': 'stock', 'listed_at': '2016-01-04',
     'quality_status': 'verified'},
    {'security_id': 'SEC-000003', 'asset_type': 'stock', 'listed_at': '2016-01-04',
     'quality_status': 'conflict'},                                   # 主数据冲突
    {'security_id': 'SEC-000004', 'asset_type': 'leveraged_etf', 'listed_at': '2016-01-04',
     'quality_status': 'verified'},
    {'security_id': 'SEC-000005', 'asset_type': 'etf', 'listed_at': '2016-01-04',
     'quality_status': 'verified'},
    {'security_id': 'SEC-000006', 'asset_type': 'stock', 'listed_at': '2016-01-04',
     'quality_status': 'verified'},                                   # 无 symbol
    {'security_id': 'SEC-000007', 'asset_type': 'stock', 'listed_at': '2016-01-06',
     'quality_status': 'verified'},                                   # 晚上市
    {'security_id': 'SEC-000008', 'asset_type': 'stock', 'listed_at': '2016-01-04',
     'quality_status': 'verified'},                                   # 低价
    {'security_id': 'SEC-000009', 'asset_type': 'stock', 'listed_at': '2016-01-04',
     'quality_status': 'verified'},                                   # 低流动性
    {'security_id': 'SEC-000010', 'asset_type': 'stock', 'listed_at': '2016-01-04',
     'quality_status': 'verified'},                                   # 缺日线
    {'security_id': 'SEC-000011', 'asset_type': 'stock', 'listed_at': '2016-01-04',
     'quality_status': 'verified'},                                   # 公司行动未解析
])
MASTER['valid_from'] = '2016-01-01'
MASTER['valid_to'] = ''

SYMBOLS = pd.DataFrame([
    {'security_id': 'SEC-000001', 'symbol': 'US.OLD', 'valid_from': '2016-01-01', 'valid_to': '2016-01-06'},
    {'security_id': 'SEC-000001', 'symbol': 'US.NEW', 'valid_from': '2016-01-06', 'valid_to': ''},
    {'security_id': 'SEC-000002', 'symbol': 'US.TWO', 'valid_from': '2016-01-01', 'valid_to': ''},
    {'security_id': 'SEC-000003', 'symbol': 'US.CONF', 'valid_from': '2016-01-01', 'valid_to': ''},
    {'security_id': 'SEC-000004', 'symbol': 'US.LEV', 'valid_from': '2016-01-01', 'valid_to': ''},
    {'security_id': 'SEC-000005', 'symbol': 'US.ETF', 'valid_from': '2016-01-01', 'valid_to': ''},
    {'security_id': 'SEC-000007', 'symbol': 'US.LATE', 'valid_from': '2016-01-01', 'valid_to': ''},
    {'security_id': 'SEC-000008', 'symbol': 'US.LOW', 'valid_from': '2016-01-01', 'valid_to': ''},
    {'security_id': 'SEC-000009', 'symbol': 'US.THIN', 'valid_from': '2016-01-01', 'valid_to': ''},
    {'security_id': 'SEC-000010', 'symbol': 'US.NOBAR', 'valid_from': '2016-01-01', 'valid_to': ''},
    {'security_id': 'SEC-000011', 'symbol': 'US.ACT', 'valid_from': '2016-01-01', 'valid_to': ''},
])


def make_liquidity(extra=None):
    with_rows = ['SEC-000001', 'SEC-000002', 'SEC-000003', 'SEC-000004', 'SEC-000005',
                 'SEC-000007', 'SEC-000008', 'SEC-000009', 'SEC-000011']  # 006/010 无行情行
    rows = []
    for sec in with_rows:
        for session in SESSIONS:
            row = {'security_id': sec, 'date': session,
                   'previous_raw_close': 2.0 if sec == 'SEC-000008' else 10.0,
                   'adv20_usd': 1_000_000.0 if sec == 'SEC-000009' else 10_000_000.0,
                   'liquidity_as_of': session - pd.Timedelta(days=1)}
            if extra:
                row.update(extra(sec, session))
            rows.append(row)
    return pd.DataFrame(rows)


class UniverseV2Tests(unittest.TestCase):
    def _universe(self, liquidity=None, **kwargs):
        return build_point_in_time_universe_v2(
            MASTER, SYMBOLS, liquidity if liquidity is not None else make_liquidity(), SESSIONS,
            master_version='MV1', price_version='PV1',
            corporate_action_unresolved=('SEC-000011',), **kwargs)

    def test_rename_keeps_single_security_per_day(self):
        u = self._universe()
        sec1 = u[u.security_id == 'SEC-000001'].set_index('universe_date')
        self.assertEqual(len(sec1), len(SESSIONS))          # 改名不产生两只股票
        self.assertEqual(sec1.loc['2016-01-04', 'symbol_as_of'], 'US.OLD')
        self.assertEqual(sec1.loc['2016-01-06', 'symbol_as_of'], 'US.NEW')
        self.assertTrue(sec1['eligible'].all())

    def test_listing_window(self):
        u = self._universe().set_index(['security_id', 'universe_date'])
        self.assertEqual(u.loc[('SEC-000007', '2016-01-04'), 'reason'], 'NOT_LISTED')
        self.assertEqual(u.loc[('SEC-000007', '2016-01-06'), 'reason'], 'ELIGIBLE')

    def test_reasons(self):
        u = self._universe().set_index(['security_id', 'universe_date'])
        self.assertEqual(u.loc[('SEC-000003', '2016-01-04'), 'reason'], 'MASTER_CONFLICT')
        self.assertEqual(u.loc[('SEC-000006', '2016-01-04'), 'reason'], 'SYMBOL_UNAVAILABLE')
        self.assertEqual(u.loc[('SEC-000010', '2016-01-04'), 'reason'], 'MISSING_BARS')
        self.assertEqual(u.loc[('SEC-000008', '2016-01-04'), 'reason'], 'PRICE_TOO_LOW')
        self.assertEqual(u.loc[('SEC-000009', '2016-01-04'), 'reason'], 'DOLLAR_VOLUME_TOO_LOW')
        self.assertEqual(u.loc[('SEC-000011', '2016-01-04'), 'reason'], 'CORPORATE_ACTION_UNRESOLVED')
        self.assertFalse(bool(u.loc[('SEC-000011', '2016-01-04'), 'eligible']))

    def test_main_sample_excludes_non_stock(self):
        u = self._universe()
        main = u[u.eligible & u.asset_type.eq('stock')]
        self.assertNotIn('SEC-000004', set(main.security_id))
        self.assertNotIn('SEC-000005', set(main.security_id))
        self.assertTrue(u[u.security_id == 'SEC-000004'].eligible.iloc[0])   # 仍输出独立切片

    def test_rejected_records_retained_with_reason_counts(self):
        summary = universe_reason_summary(self._universe())
        self.assertTrue({'ELIGIBLE', 'NOT_LISTED', 'MASTER_CONFLICT', 'SYMBOL_UNAVAILABLE',
                         'MISSING_BARS', 'CORPORATE_ACTION_UNRESOLVED', 'PRICE_TOO_LOW',
                         'DOLLAR_VOLUME_TOO_LOW'} <= set(summary['reason']))

    def test_uses_t_minus_1_liquidity_only(self):
        base = self._universe()
        altered = self._universe(make_liquidity(extra=lambda s, d: {'same_day_dollar_volume': 999}))
        pd.testing.assert_frame_equal(base, altered)

    def test_lookahead_liquidity_rejected(self):
        bad = make_liquidity()
        bad.loc[0, 'liquidity_as_of'] = bad.loc[0, 'date']
        with self.assertRaises(ValueError):
            self._universe(bad)

    def test_versions_propagated(self):
        u = self._universe()
        self.assertEqual(u['master_version'].unique().tolist(), ['MV1'])
        self.assertEqual(u['price_version'].unique().tolist(), ['PV1'])


if __name__ == '__main__':
    unittest.main()
