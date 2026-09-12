"""双价格视图与公司行动一致性测试（技术设计 §2.4/§2.6）。"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.data.price_views import (action_version, build_price_view, build_price_views,
                                      corporate_action_flags)

ROOT = Path(__file__).resolve().parents[3]

SESSION = pd.bdate_range('2020-01-01', '2020-01-10')


def bars(closes, volumes=None, security_id='SEC-A'):
    volumes = volumes if volumes is not None else [1000] * len(closes)
    return pd.DataFrame({'security_id': security_id, 'session': SESSION,
                         'open': closes, 'high': closes, 'low': closes,
                         'close': closes, 'volume': volumes})


def split_bars():
    # 2:1 拆股：ex_date 之前 100、之后 50；成交量翻倍。
    closes = [100.0 if d < pd.Timestamp('2020-01-06') else 50.0 for d in SESSION]
    volumes = [1000.0 if d < pd.Timestamp('2020-01-06') else 2000.0 for d in SESSION]
    return bars(closes, volumes)


ACTIONS = pd.DataFrame([{'security_id': 'SEC-A', 'action_type': 'split', 'ex_date': '2020-01-06',
                         'ratio': 2.0, 'cash_amount': 0.0}])


class PriceViewTests(unittest.TestCase):
    def _raw(self, frame=split_bars(), actions=ACTIONS):
        return build_price_view(frame, actions, price_basis='raw').set_index('session')

    def _adj(self, frame=split_bars(), actions=ACTIONS, as_of=None):
        return build_price_view(frame, actions, price_basis='asof_adjusted',
                                as_of=as_of).set_index('session')

    def test_split_makes_adjusted_series_continuous(self):
        adj = self._adj()
        self.assertAlmostEqual(adj.loc['2020-01-03', 'close'], 50.0)   # 100 * 1/2
        self.assertAlmostEqual(adj.loc['2020-01-07', 'close'], 50.0)   # 未调整
        self.assertEqual(adj['close'].nunique(), 1)

    def test_split_total_return_is_consistent(self):
        raw = self._raw(); adj = self._adj()
        f1, f2 = '2020-01-02', '2020-01-09'
        raw_total = raw.loc[f2, 'close'] / raw.loc[f1, 'close'] * 2.0   # 拆股使股数翻倍
        self.assertAlmostEqual(adj.loc[f2, 'close'] / adj.loc[f1, 'close'], raw_total)

    def test_volume_adjustment_preserves_dollar_volume(self):
        raw = self._raw(); adj = self._adj()
        self.assertTrue(((raw['close'] * raw['volume']) == (adj['close'] * adj['volume'])).all())

    def test_reverse_split(self):
        closes = [50.0 if d < pd.Timestamp('2020-01-06') else 100.0 for d in SESSION]
        actions = ACTIONS.assign(action_type='reverse_split', ratio=0.5)
        adj = self._adj(bars(closes), actions)
        self.assertAlmostEqual(adj.loc['2020-01-03', 'close'], 100.0)  # 50 * 1/0.5

    def test_cash_dividend_factor(self):
        closes = [100.0] * len(SESSION)
        actions = pd.DataFrame([{'security_id': 'SEC-A', 'action_type': 'cash_dividend',
                                 'ex_date': '2020-01-06', 'ratio': 0.0, 'cash_amount': 2.0}])
        adj = self._adj(bars(closes), actions)
        self.assertAlmostEqual(adj.loc['2020-01-03', 'close'], 98.0)   # 1 - 2/100
        self.assertAlmostEqual(adj.loc['2020-01-07', 'close'], 100.0)
        # 缺少除息前收盘时不得静默处理
        with self.assertRaises(ValueError):
            build_price_view(bars(closes).iloc[5:].reset_index(drop=True), actions,
                             price_basis='asof_adjusted')

    def test_spinoff_factor(self):
        closes = [100.0] * len(SESSION)
        actions = pd.DataFrame([{'security_id': 'SEC-A', 'action_type': 'spinoff',
                                 'ex_date': '2020-01-06', 'ratio': 0.2, 'cash_amount': 0.0}])
        adj = self._adj(bars(closes), actions)
        self.assertAlmostEqual(adj.loc['2020-01-03', 'close'], 80.0)   # 1 - 0.2

    def test_no_future_actions_applied(self):
        # as_of 早于 ex_date：不得应用未来行动，调整后应等于原始。
        frame = split_bars()
        adj = build_price_view(frame, ACTIONS, price_basis='asof_adjusted',
                               as_of='2020-01-02').set_index('session')
        raw = build_price_view(frame, ACTIONS, price_basis='raw').set_index('session')
        self.assertTrue((adj['close'] == raw['close']).all())
        self.assertEqual(adj['adjustment_as_of'].iloc[0], '2020-01-02')

    def test_action_version_stable_and_sensitive(self):
        self.assertEqual(action_version(ACTIONS), action_version(ACTIONS.copy()))
        self.assertNotEqual(action_version(ACTIONS), action_version(pd.DataFrame()))
        changed = ACTIONS.copy(); changed.loc[0, 'ratio'] = 3.0
        self.assertNotEqual(action_version(ACTIONS), action_version(changed))

    def test_price_view_metadata_and_both_views(self):
        both = build_price_views(split_bars(), ACTIONS, as_of='2020-01-31')
        self.assertEqual(set(both['price_basis']), {'raw', 'asof_adjusted'})
        raw = both[both.price_basis == 'raw']
        adj = both[both.price_basis == 'asof_adjusted']
        self.assertTrue(raw['adjustment_as_of'].isna().all())          # 原始价不做调整
        self.assertEqual(adj['adjustment_as_of'].iloc[0], '2020-01-31')
        self.assertEqual(adj['action_version'].nunique(), 1)

    def test_corporate_action_flags(self):
        master = pd.DataFrame([{'security_id': 'SEC-A'}])
        # 无并购/分拆 → 不标记
        flags = corporate_action_flags(split_bars(), master, ACTIONS).set_index('security_id')
        self.assertNotIn('corporate_action_unresolved', flags.loc['SEC-A', 'problems'])
        # 并购无 ratio/cash → 无法核实价格衔接 → 标记
        unresolved = pd.concat([ACTIONS, pd.DataFrame([{'security_id': 'SEC-A', 'action_type': 'merger',
                                                        'ex_date': '2020-01-10', 'ratio': None,
                                                        'cash_amount': None}])], ignore_index=True)
        flags = corporate_action_flags(split_bars(), master, unresolved).set_index('security_id')
        self.assertIn('corporate_action_unresolved', flags.loc['SEC-A', 'problems'])
        # 显式传入的样本同样标记
        flags = corporate_action_flags(split_bars(), master, ACTIONS,
                                       unresolved=('SEC-A',)).set_index('security_id')
        self.assertIn('corporate_action_unresolved', flags.loc['SEC-A', 'problems'])
        # 无日线 → NO_BARS
        flags = corporate_action_flags(split_bars().iloc[0:0], master, ACTIONS).set_index('security_id')
        self.assertIn('NO_BARS', flags.loc['SEC-A', 'problems'])


    def test_build_price_views_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            split_bars().to_csv(tmp / 'bars.csv', index=False)
            ACTIONS.to_csv(tmp / 'actions.csv', index=False)
            result = subprocess.run(
                [sys.executable, 'scripts/data/build_price_views.py',
                 '--bars', str(tmp / 'bars.csv'), '--actions', str(tmp / 'actions.csv'),
                 '--as-of', '2020-01-31', '--output-dir', str(tmp / 'out')],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            views = pd.read_csv(tmp / 'out' / 'price_views.csv.gz')
            self.assertEqual(set(views['price_basis']), {'raw', 'asof_adjusted'})
            self.assertTrue((tmp / 'out' / 'summary.json').is_file())


if __name__ == '__main__':
    unittest.main()
