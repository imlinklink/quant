"""symbol -> security_id 桥接与已核验主数据模式测试（技术设计 §2.2/§2.5）。"""
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.data.id_bridge import attach_security_id, bridge_run
from scripts.data.run_daily_pipeline import run_pipeline

ROOT = Path(__file__).resolve().parents[3]

SYMBOLS = pd.DataFrame([
    {'security_id': 'SEC-A', 'symbol': 'US.X', 'valid_from': '2020-01-01', 'valid_to': '2020-06-01'},
    {'security_id': 'SEC-B', 'symbol': 'US.X', 'valid_from': '2020-05-01', 'valid_to': ''},
    {'security_id': 'SEC-C', 'symbol': 'US.Y', 'valid_from': '2020-01-01', 'valid_to': ''},
])


def bars(rows, symbol_col='stock'):
    return pd.DataFrame([{symbol_col: s, 'date': d, 'open': 1.0, 'high': 1.0, 'low': 1.0,
                          'close': 1.0, 'volume': 100.0} for s, d in rows])


class IdBridgeTests(unittest.TestCase):
    def test_maps_rename_and_reuse(self):
        frame = bars([('US.X', '2020-02-01'), ('US.X', '2020-07-01'), ('US.Y', '2020-03-01')])
        mapped, unmapped, ambiguous = attach_security_id(frame, SYMBOLS,
                                                        symbol_col='stock', date_col='date')
        self.assertEqual(len(mapped), 3)
        self.assertEqual(len(unmapped), 0)
        self.assertEqual(len(ambiguous), 0)
        got = dict(zip(mapped['date'], mapped['security_id']))
        self.assertEqual(got['2020-02-01'], 'SEC-A')     # 复用前的窗口
        self.assertEqual(got['2020-07-01'], 'SEC-B')     # 复用后的窗口
        self.assertEqual(got['2020-03-01'], 'SEC-C')

    def test_ambiguous_window_and_unmapped(self):
        frame = bars([('US.X', '2020-05-15'), ('US.Z', '2020-02-01')])
        mapped, unmapped, ambiguous = attach_security_id(frame, SYMBOLS,
                                                        symbol_col='stock', date_col='date')
        self.assertEqual(len(mapped), 0)
        self.assertEqual(len(ambiguous), 1)              # US.X 窗口重叠，不得任选
        self.assertEqual(len(unmapped), 1)               # US.Z 不在 symbol_history

    def test_bridge_run_outputs_security_id_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / 'run'; run.mkdir()
            bars([('US.X', '2020-02-03'), ('US.X', '2020-07-01')]).to_csv(
                run / 'daily.csv.gz', index=False)
            pd.DataFrame([{'date': '2020-02-03', 'code': 'US.X', 'previous_close': 10.0,
                           'adv20': 1e7, 'liquidity_as_of': '2020-01-31'},
                          {'date': '2020-07-01', 'code': 'US.X', 'previous_close': 11.0,
                           'adv20': 2e7, 'liquidity_as_of': '2020-06-30'}]).to_csv(
                run / 'daily_liquidity.csv.gz', index=False)
            symbols_path = Path(tmp) / 'symbols.csv'; SYMBOLS.to_csv(symbols_path, index=False)
            report = bridge_run(run, symbols_path, Path(tmp) / 'out')
            self.assertEqual(report['daily_mapped'], 2)
            daily = pd.read_csv(Path(tmp) / 'out' / 'daily_v2.csv.gz')
            self.assertIn('security_id', daily.columns)
            self.assertIn('session', daily.columns)
            liquidity = pd.read_csv(Path(tmp) / 'out' / 'daily_liquidity_v2.csv.gz')
            self.assertIn('previous_raw_close', liquidity.columns)   # 已重命名
            self.assertIn('adv20_usd', liquidity.columns)


class VerifiedMasterModeTests(unittest.TestCase):
    def test_verified_master_preserves_listing_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / 'runs'
            master = ROOT / 'data' / 'security_master_pilot.csv'
            if not master.is_file():
                self.skipTest('缺少试点主数据')
            state = run_pipeline(master_path=str(master), start='2016-01-01', end='2016-12-31',
                                 universe_start='2016-01-01', universe_end='2016-06-30',
                                 run_id='VERIFIED-001', runs_root=str(runs),
                                 skip_download=True, verified_master=True)
            self.assertEqual(state['stages']['listing_dates']['status'], 'preserved_verified')
            written = pd.read_csv(runs / 'VERIFIED-001' / 'security_master.csv')
            original = pd.read_csv(master)
            merged = written.merge(original[['code', 'listing_date']], on='code', suffixes=('', '_orig'))
            self.assertTrue((merged['listing_date'].fillna('') ==
                             merged['listing_date_orig'].fillna('')).all(),
                            '已核验模式下不得用首根 K 线覆盖真实上市日')


if __name__ == '__main__':
    unittest.main()
