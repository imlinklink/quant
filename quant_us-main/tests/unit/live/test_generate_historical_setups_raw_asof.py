"""Historical setup prices must use decision-day features and executable raw opens."""
import unittest
from unittest.mock import patch

import pandas as pd

from scripts.data.generate_historical_setups import _raw_asof_snapshots, generate


def bars():
    days = pd.bdate_range('2020-01-01', periods=280)
    prices = [100.0 if i < 210 else 50.0 for i in range(len(days))]
    return pd.DataFrame({
        'stock': 'US.A', 'security_id': 'SEC-A', 'date': days,
        'open': prices, 'high': [p * 1.01 for p in prices],
        'low': [p * .99 for p in prices], 'close': prices,
        'volume': 1000.0,
    })


def split(data):
    return pd.DataFrame([{'security_id': 'SEC-A', 'action_type': 'split',
                          'ex_date': data.date.iloc[210], 'ratio': 2.0, 'cash_amount': 0.0}])


class RawAsofGeneratorTests(unittest.TestCase):
    def test_snapshots_keep_pre_split_scale_then_adjust(self):
        data = bars()
        snapshots, panel = _raw_asof_snapshots(data, split(data), {})
        self.assertAlmostEqual(snapshots[209]['features']['ma200'], 100.0)
        self.assertAlmostEqual(snapshots[210]['features']['ma200'], 50.0)
        self.assertAlmostEqual(panel.loc[209, 'scale_to_next'], .5)
        self.assertTrue(snapshots[209]['feature_version'].endswith('-raw-asof-v1'))

    def test_empty_actions_are_valid(self):
        snapshots, panel = _raw_asof_snapshots(bars(), pd.DataFrame(), {})
        self.assertEqual(len(snapshots), len(panel))
        self.assertTrue((panel.scale_to_next == 1).all())

    def test_dividend_before_raw_window_is_ignored(self):
        data=bars()
        old=pd.DataFrame([{'security_id':'SEC-A','action_type':'cash_dividend',
            'ex_date':'2010-01-01','ratio':0.,'cash_amount':1.}])
        snapshots,panel=_raw_asof_snapshots(data,old,{})
        self.assertEqual(len(snapshots),len(data))
        self.assertTrue((panel.scale_to_next==1).all())

    def test_generated_entry_uses_raw_open_and_converts_levels_across_split(self):
        data = bars()

        def candidate(code, snapshot, state, config):
            if snapshot['session'] != str(data.date.iloc[209].date()):
                return None
            return {'setup_id': 'test-setup', 'trigger_price': 101.0,
                    'invalidation_price': 95.0, 'initial_stop': 94.0,
                    'max_chase_price': 102.0, 'risk_per_share': 7.0}

        with patch('scripts.data.generate_historical_setups.build_setup_candidate', side_effect=candidate):
            out = generate(data, {'buy_strategy_v2': {'min_daily_bars': 200}},
                           start=data.date.iloc[209], end=data.date.iloc[209],
                           price_basis='raw_asof', actions=split(data))
        self.assertEqual(len(out), 1)
        row = out.iloc[0]
        self.assertEqual(row.next_open_price, 50.0)
        self.assertEqual(row.trigger_price, 50.5)
        self.assertEqual(row.initial_stop, 47.0)
        self.assertEqual(row.risk_per_share, 3.5)
        self.assertEqual(row.signal_close, 50.0)
        self.assertEqual(row.price_basis, 'raw_asof')

    def test_merger_blocks_generation(self):
        data = bars()
        actions = split(data).assign(action_type='merger')
        with self.assertRaisesRegex(ValueError, 'ACTION_TYPE_UNSUPPORTED'):
            generate(data, {}, price_basis='raw_asof', actions=actions)


if __name__ == '__main__':
    unittest.main()
