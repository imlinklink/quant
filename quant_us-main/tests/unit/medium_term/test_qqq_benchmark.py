"""同口径 QQQ 基准（原始价 + 官方分红表 + 逐腿成本）的确定性测试。"""
import unittest

import pandas as pd

from scripts.medium_term.performance import performance_metrics
from scripts.medium_term.qqq_benchmark import build_qqq_benchmark


def _prices():
    return pd.DataFrame({
        'session': pd.to_datetime(['2024-01-02', '2024-01-03', '2024-01-04']),
        'open': [100., 101., 102.],
        'close': [101., 102., 103.],
    })


class QQQBenchmarkTests(unittest.TestCase):
    def test_buys_at_open_marks_at_close_with_fee(self):
        out = build_qqq_benchmark(_prices(),
                                  pd.DataFrame({'ex_date': [], 'amount': []}),
                                  '2024-01-02', '2024-01-04')
        shares = 100_000. / (100. * 1.001)
        self.assertEqual(len(out), 3)
        self.assertTrue((out.equity > 0).all())
        self.assertAlmostEqual(out.equity.iloc[0], shares * 101., places=6)
        self.assertAlmostEqual(out.equity.iloc[-1], shares * 103., places=6)

    def test_dividend_is_cash_not_reinvested(self):
        div = pd.DataFrame({'ex_date': pd.to_datetime(['2024-01-03']), 'amount': [1.0]})
        out = build_qqq_benchmark(_prices(), div, '2024-01-02', '2024-01-04')
        shares = 100_000. / (100. * 1.001)
        # 除息日红利进现金：equity = shares*1.0 + shares*close；最后一日红利不再滚入。
        self.assertAlmostEqual(out.equity.iloc[1], shares * (1.0 + 102.), places=6)
        self.assertAlmostEqual(out.equity.iloc[2], shares * (1.0 + 103.), places=6)

    def test_rejects_empty_window_and_duplicate_dividends(self):
        prices = _prices()
        with self.assertRaises(ValueError):
            build_qqq_benchmark(prices, pd.DataFrame(columns=['ex_date', 'amount']),
                                '2020-01-01', '2020-01-05')
        dup = pd.DataFrame({'ex_date': pd.to_datetime(['2024-01-03', '2024-01-03']),
                            'amount': [1., 1.]})
        with self.assertRaises(ValueError):
            build_qqq_benchmark(prices, dup, '2024-01-02', '2024-01-04')

    def test_flat_price_reflects_buy_cost_not_zero(self):
        prices = pd.DataFrame({
            'session': pd.to_datetime(['2024-01-02', '2024-01-03']),
            'open': [100., 100.],
            'close': [100., 100.],
        })
        out = build_qqq_benchmark(prices, pd.DataFrame({'ex_date': [], 'amount': []}),
                                  '2024-01-02', '2024-01-03')
        self.assertEqual(out.initial_equity.iloc[0], 100_000.)
        metrics = performance_metrics(out)
        # 平价格 + 0.1% 买入费 → 总收益 ≈ -0.1%（不是 0%）
        self.assertAlmostEqual(metrics['total_return'], 1.0 / 1.001 - 1, places=9)

    def test_initial_equity_captures_first_day_gain(self):
        prices = pd.DataFrame({
            'session': pd.to_datetime(['2024-01-02', '2024-01-03']),
            'open': [100., 110.],
            'close': [110., 110.],
        })
        out = build_qqq_benchmark(prices, pd.DataFrame({'ex_date': [], 'amount': []}),
                                  '2024-01-02', '2024-01-03')
        metrics = performance_metrics(out)
        # 首日 open100→close110 = +10%，减 0.1% 费 → 总收益 = 1.10/1.001 - 1
        self.assertAlmostEqual(metrics['total_return'], 1.10 / 1.001 - 1, places=9)

    def test_buys_on_ex_div_date_skips_first_day_dividend(self):
        prices = pd.DataFrame({
            'session': pd.to_datetime(['2024-01-02', '2024-01-03']),
            'open': [100., 100.],
            'close': [100., 100.],
        })
        div = pd.DataFrame({'ex_date': pd.to_datetime(['2024-01-02']), 'amount': [1.0]})
        out = build_qqq_benchmark(prices, div, '2024-01-02', '2024-01-03')
        shares = 100_000. / (100. * 1.001)
        # 首日除息：开盘买入不享分红 → 首日净值 = shares*100（无每股+1美元）
        self.assertAlmostEqual(out.equity.iloc[0], shares * 100., places=6)


if __name__ == '__main__':
    unittest.main()
