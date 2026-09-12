"""逐日 as-of 特征 / 前视对照测试（技术设计 §2.4；交接方案 §4.2）。"""
import unittest

import pandas as pd

from scripts.data.asof_features import asof_features, feature_drift, full_snapshot_features

SESSIONS = pd.bdate_range('2020-01-01', '2020-03-31')
SPLIT_DAY = pd.Timestamp('2020-03-02')          # 2:1 拆股


def bars(split=False):
    """拆股日之前 100，之后 50（原始价）。"""
    if split:
        close = [100.0 if d < SPLIT_DAY else 50.0 for d in SESSIONS]
        volume = [1000.0 if d < SPLIT_DAY else 2000.0 for d in SESSIONS]
    else:
        close = [100.0] * len(SESSIONS); volume = [1000.0] * len(SESSIONS)
    return pd.DataFrame({'security_id': 'SEC-A', 'session': SESSIONS, 'open': close,
                         'high': close, 'low': close, 'close': close, 'volume': volume})


SPLIT = pd.DataFrame([{'security_id': 'SEC-A', 'action_type': 'split', 'ex_date': '2020-03-02',
                       'ratio': 2.0, 'cash_amount': 0.0}])


class AsofFeatureTests(unittest.TestCase):
    def test_features_before_action_ignore_future_split(self):
        data = bars(split=True)
        # 拆股前一天的决策：不得把未来的 2:1 缩股算进来 → 口径仍是拆股前的价格尺度
        good = asof_features(data, SPLIT, '2020-02-28')
        naive = full_snapshot_features(data, SPLIT)
        self.assertAlmostEqual(good['close'], 100.0)          # 拆股前原始价=100
        self.assertAlmostEqual(naive['close'], 50.0)          # 错法被未来拆股缩到 50
        self.assertAlmostEqual(naive['close'] / good['close'], 0.5)
        self.assertAlmostEqual(good['ma20'], 100.0)
        self.assertAlmostEqual(naive['ma20'], 50.0)

    def test_features_converge_after_action(self):
        data = bars(split=True)
        good = asof_features(data, SPLIT, '2020-03-31')       # 拆股之后
        naive = full_snapshot_features(data, SPLIT)
        self.assertAlmostEqual(good['close'], 50.0)
        self.assertAlmostEqual(naive['close'] / good['close'], 1.0)

    def test_no_action_no_drift(self):
        data = bars(split=False)
        drift = feature_drift(data, pd.DataFrame(columns=['security_id', 'action_type',
                                                          'ex_date', 'ratio', 'cash_amount']),
                              '2020-03-31')
        self.assertTrue(all(abs(v) < 1e-9 for v in drift['relative_drift'].values()))

    def test_drift_quantifies_lookahead(self):
        data = bars(split=True)
        drift = feature_drift(data, SPLIT, '2020-02-28')
        self.assertAlmostEqual(drift['relative_drift']['close'], 1.0)   # 错法高估 100%


if __name__ == '__main__':
    unittest.main()
