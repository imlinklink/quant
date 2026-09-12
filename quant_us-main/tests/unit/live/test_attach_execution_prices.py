"""执行日原始价尺度换算测试（M2）。"""
import unittest

import pandas as pd

from scripts.data.attach_execution_prices import attach_execution_prices

SPLIT_DAY = pd.Timestamp('2020-03-02')
DAYS = pd.bdate_range('2020-02-24', '2020-03-06')


def qfq():
    # QFQ 锚定最新：拆股使拆股前价格减半 → 全期 50
    return pd.DataFrame({'stock': 'US.A', 'date': DAYS, 'close': 50.0})


def raw():
    close = [100.0 if d < SPLIT_DAY else 50.0 for d in DAYS]
    return pd.DataFrame({'stock': 'US.A', 'date': DAYS, 'open': close, 'close': close})


def setups(entry):
    return pd.DataFrame([{'stock': 'US.A', 'entry_session': entry, 'signal_close': 50.0,
                          'initial_stop': 45.0, 'atr14': 2.0}])


class ExecutionPriceTests(unittest.TestCase):
    def test_conv_before_split_doubles_levels(self):
        out = attach_execution_prices(setups('2020-02-28'), qfq(), raw())
        row = out.iloc[0]
        self.assertAlmostEqual(row['exec_conv'], 2.0)          # raw=100, qfq=50
        self.assertAlmostEqual(row['initial_stop_raw'], 90.0)  # 45 × 2
        self.assertAlmostEqual(row['signal_close_raw'], 100.0)

    def test_conv_on_split_day_is_one(self):
        out = attach_execution_prices(setups('2020-03-02'), qfq(), raw())
        row = out.iloc[0]
        self.assertAlmostEqual(row['exec_conv'], 1.0)
        self.assertAlmostEqual(row['initial_stop_raw'], 45.0)
        self.assertAlmostEqual(row['entry_price_raw'], 50.0)

    def test_same_relative_level_across_split(self):
        before = attach_execution_prices(setups('2020-02-28'), qfq(), raw()).iloc[0]
        on = attach_execution_prices(setups('2020-03-02'), qfq(), raw()).iloc[0]
        # 两个口径都表示"收盘下方 10%"的止损
        self.assertAlmostEqual(before['initial_stop_raw'] / before['signal_close_raw'], 0.9)
        self.assertAlmostEqual(on['initial_stop_raw'] / on['signal_close_raw'], 0.9)

    def test_missing_price_gives_none(self):
        out = attach_execution_prices(setups('2020-01-02'), qfq(), raw())   # 窗口外
        self.assertTrue(pd.isna(out.iloc[0]['exec_conv']))


if __name__ == '__main__':
    unittest.main()
