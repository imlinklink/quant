"""逐日 as-of 特征面板与原始执行价测试（§2.4；M2）。"""
import unittest

import pandas as pd

from scripts.data.asof_feature_panel import build_asof_panel
from scripts.data.asof_features import asof_features

SESSIONS = pd.bdate_range('2020-01-01', '2020-06-30')
SPLIT_DAY = pd.Timestamp('2020-03-02')


def raw_bars(split=True):
    close = [100.0 if (not split or d < SPLIT_DAY) else 50.0 for d in SESSIONS]
    return pd.DataFrame({'security_id': 'SEC-A', 'session': SESSIONS, 'open': close,
                         'high': close, 'low': close, 'close': close, 'volume': 1000.0})


SPLIT = pd.DataFrame([{'security_id': 'SEC-A', 'action_type': 'split', 'ex_date': '2020-03-02',
                       'ratio': 2.0, 'cash_amount': 0.0}])


class AsofPanelTests(unittest.TestCase):
    def test_asof_close_equals_raw_close_every_day(self):
        panel = build_asof_panel(raw_bars(), SPLIT)
        self.assertTrue((panel.asof_close == panel.raw_close).all())   # 锚定当日 → 同尺度

    def test_features_match_single_day_builder(self):
        """面板在任意决策日的特征，应等于逐日构造器 asof_features 的结果。"""
        data = raw_bars(); panel = build_asof_panel(data, SPLIT)
        for day in ('2020-02-28', '2020-04-01'):
            row = panel[panel.session == pd.Timestamp(day)].iloc[0]
            ref = asof_features(data, SPLIT, day)
            self.assertAlmostEqual(row.asof_close, ref['close'])
            self.assertAlmostEqual(row.asof_ma20, ref['ma20'])
            self.assertAlmostEqual(row.asof_atr, ref['atr'])

    def test_split_day_scales_prior_window_only(self):
        panel = build_asof_panel(raw_bars(), SPLIT)
        before = panel[panel.session == pd.Timestamp('2020-02-28')].iloc[0]
        after = panel[panel.session == pd.Timestamp('2020-03-02')].iloc[0]
        # 拆股前：窗口全在拆股前 → 原始尺度 100
        self.assertAlmostEqual(before.asof_ma20, 100.0)
        # 拆股当日：当日 bar 之后的窗口含 50 与(被缩放的)更早值 → 不再是 100
        self.assertNotAlmostEqual(after.asof_ma20, 100.0)

    def test_scale_to_next_flags_action_on_next_session(self):
        panel = build_asof_panel(raw_bars(), SPLIT)
        prev = panel[panel.session == pd.Timestamp('2020-02-28')].iloc[0]
        # 下一交易日是 2:1 拆股日：T 日水平(拆股前尺度) × 0.5 才能与 T+1 原始价比较
        self.assertAlmostEqual(prev.scale_to_next, 0.5)
        other = panel[panel.session == pd.Timestamp('2020-02-26')].iloc[0]
        self.assertAlmostEqual(other.scale_to_next, 1.0)

    def test_level_converts_to_next_session_scale(self):
        panel = build_asof_panel(raw_bars(), SPLIT)
        t = panel[panel.session == pd.Timestamp('2020-02-28')].iloc[0]
        nxt = panel[panel.session == SPLIT_DAY].iloc[0]
        # 拆股前沿用的价格水平(100)换算到拆股日尺度应等于当日原始价(50)
        self.assertAlmostEqual(t.asof_close * t.scale_to_next, nxt.raw_open)

    def test_no_action_means_no_scaling(self):
        panel = build_asof_panel(raw_bars(split=False), pd.DataFrame(
            columns=['security_id', 'action_type', 'ex_date', 'ratio', 'cash_amount']))
        self.assertTrue((panel.scale_to_next == 1.0).all())
        self.assertTrue((panel.asof_close == panel.raw_close).all())

    def test_unresolved_dividend_factor_blocks_panel(self):
        data = raw_bars().iloc[10:].reset_index(drop=True)
        action = pd.DataFrame([{'security_id': 'SEC-A', 'action_type': 'cash_dividend',
                                'ex_date': str(data.session.iloc[0].date()),
                                'ratio': 0.0, 'cash_amount': 1.0}])
        with self.assertRaisesRegex(ValueError, 'ACTION_FACTOR_UNRESOLVED'):
            build_asof_panel(data, action)

    def test_other_security_actions_do_not_change_panel(self):
        other = SPLIT.assign(security_id='SEC-B')
        panel = build_asof_panel(raw_bars(split=False), other)
        self.assertTrue((panel.asof_close == 100.0).all())
        self.assertTrue((panel.scale_to_next == 1.0).all())

    def test_merger_in_window_blocks_panel(self):
        merger = pd.DataFrame([{'security_id': 'SEC-A', 'action_type': 'merger',
                                'ex_date': '2020-03-02', 'ratio': None, 'cash_amount': None}])
        with self.assertRaisesRegex(ValueError, 'ACTION_TYPE_UNSUPPORTED'):
            build_asof_panel(raw_bars(), merger)


if __name__ == '__main__':
    unittest.main()
