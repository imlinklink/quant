"""富途公司行动直取测试（技术设计 §2.2）。"""
import unittest

import pandas as pd

from scripts.data.import_corporate_actions_from_futu import (build_actions, cash_from_statement,
                                                             collect_actions,
                                                             ratio_from_rate)

SPLITS = {'US.NVDA': [
    {'dir_deci_pub_date': 1717992000, 'dir_deci_pub_date_str': '2024-06-10',
     'reform_type': 'Split', 'rate': '1→10'},
    {'dir_deci_pub_date': 1626753600, 'dir_deci_pub_date_str': '2021-07-20',
     'reform_type': 'Split', 'rate': '1→4'},
]}
DIVIDENDS = {'US.AAPL': [
    {'pub_date': '07/31/2026', 'statement': 'Cash Dividend: 0.27 USD Per Share',
     'record_date': '08/10/2026', 'ex_date': '08/10/2026', 'dividend_payable_date': '08/13/2026'},
]}


class FutuCorporateActionsTests(unittest.TestCase):
    def test_parsers(self):
        self.assertAlmostEqual(ratio_from_rate('1→10'), 10.0)
        self.assertAlmostEqual(ratio_from_rate('2→3'), 1.5)
        self.assertIsNone(ratio_from_rate('bad'))
        self.assertAlmostEqual(cash_from_statement('Cash Dividend: 0.27 USD Per Share'), 0.27)
        self.assertIsNone(cash_from_statement('no cash here'))

    def test_build_actions_from_real_shapes(self):
        actions = build_actions(SPLITS, DIVIDENDS)
        self.assertEqual(len(actions), 3)
        nvda = actions[(actions.security_id == 'SEC-US-NVDA') & (actions.ex_date == '2024-06-10')]
        self.assertEqual(nvda.iloc[0]['action_type'], 'split')
        self.assertAlmostEqual(float(nvda.iloc[0]['ratio']), 10.0)
        aapl = actions[actions.security_id == 'SEC-US-AAPL']
        self.assertEqual(aapl.iloc[0]['action_type'], 'cash_dividend')
        self.assertAlmostEqual(float(aapl.iloc[0]['cash_amount']), 0.27)
        self.assertEqual(aapl.iloc[0]['ex_date'], '2026-08-10')
        # 派发日必须落进 `effective_at`。影子引擎拿它当 `dividend_receivable` 的字典键，
        # 硬写 None 会让持有的证券**一分红就中止**（`HELD_DIVIDEND_PAY_DATE_MISSING`），
        # 而不是完成分红会计 —— 而富途本来就把这个字段给了（`dividend_payable_date`）。
        self.assertEqual(pd.Timestamp(aapl.iloc[0]['effective_at']).date().isoformat(), '2026-08-13')
        self.assertTrue(str(aapl.iloc[0]['source_published_at']).startswith('2026-07-31'))
        # 两类来源不得串号
        self.assertTrue((actions[actions.action_type == 'split']['source_id']
                         == 'futu_corporate_actions_splits').all())
        self.assertTrue((actions[actions.action_type == 'cash_dividend']['source_id']
                         == 'futu_corporate_actions_dividends').all())
        # 富途无 observed_at 证据 → 不得伪造；该列应全空
        self.assertTrue(actions['source_observed_at'].isna().all())
        self.assertTrue(actions['record_hash'].notna().all())

    def test_empty_input(self):
        self.assertTrue(build_actions({}, {}).empty)


class _FakeCtx:
    """假的 OpenQuoteContext：按 (kind, code) 决定返回成功还是失败码。

    `fail_once` 让某只证券**第一次**调用失败、之后成功，用来验重试。
    """

    def __init__(self, splits=None, dividends=None, fail=(), fail_once=()):
        self._s, self._d = splits or {}, dividends or {}
        self._fail, self._fail_once = set(fail), set(fail_once)
        self._seen = {}

    def _respond(self, kind, code, key, payload):
        self._seen[(kind, code)] = self._seen.get((kind, code), 0) + 1
        if (kind, code) in self._fail:
            return 1, 'throttled'
        if (kind, code) in self._fail_once and self._seen[(kind, code)] == 1:
            return 1, 'throttled'
        return 0, {key: payload.get(code, [])}

    def get_corporate_actions_stock_splits(self, code):
        return self._respond('splits', code, 'split_list', self._s)

    def get_corporate_actions_dividends(self, code):
        return self._respond('dividends', code, 'dividend_list', self._d)


class CollectActionsFailureTests(unittest.TestCase):
    """取数失败**不得**被写成"没有公司行动"。

    2026-09-20 实测的形态：连着打 39 只时富途限流，从第 23 只起全部返回空；修前的代码
    把非 0 返回码静默变成空列表 ⇒ 产出一张只有 23/39 只的表，而 `summary.json` 里
    只有一个正常的行数、**看不出 16 只丢了**。这条路径原先埋在 `main()` 里，
    只有真连富途才走得到，所以一直没被测到 —— 抽成 `collect_actions` 就是为了钉死它。
    """

    def _run(self, ctx, codes):
        return collect_actions(ctx, codes, retries=0, sleep=lambda s: None)

    def test_failed_fetch_is_reported_not_silently_empty(self):
        ctx = _FakeCtx(fail={('splits', 'US.XOM'), ('dividends', 'US.XOM')})
        _, _, raw, failures = self._run(ctx, ['US.XOM'])
        self.assertEqual(set(failures['US.XOM']), {'splits', 'dividends'})
        # 列表**同时**是空的 —— 所以只有"失败名单"能让它区别于"真的没有行动"，
        # 两者必须一起存在，缺一个这张表就是在说谎。
        self.assertEqual(raw['US.XOM'], {'splits': [], 'dividends': []})

    def test_genuinely_no_actions_is_not_a_failure(self):
        _, _, raw, failures = self._run(_FakeCtx(), ['US.ARM'])
        self.assertEqual(failures, {})
        self.assertEqual(raw['US.ARM'], {'splits': [], 'dividends': []})

    def test_partial_batch_keeps_the_good_codes(self):
        # 一批里只挂一只 —— 好的那些不能因为有人失败就双双丢掉
        ctx = _FakeCtx(splits={'US.NVDA': [{'dir_deci_pub_date_str': '2024-06-10', 'rate': '1→10'}]},
                       fail={('dividends', 'US.XOM')})
        splits, dividends, _, failures = self._run(ctx, ['US.NVDA', 'US.XOM'])
        self.assertEqual(len(splits['US.NVDA']), 1)
        self.assertEqual(set(failures), {'US.XOM'})
        self.assertNotIn('US.NVDA', failures)
        self.assertEqual(dividends['US.XOM'], [])

    def test_transient_failure_recovers_on_retry(self):
        ctx = _FakeCtx(fail_once={('dividends', 'US.PG')},
                       dividends={'US.PG': [{'pub_date': '07/22/2026', 'statement': 'Cash Dividend: 0.8 USD Per Share',
                                             'ex_date': '08/21/2026'}]})
        _, dividends, _, failures = collect_actions(ctx, ['US.PG'], retries=2, sleep=lambda s: None)
        self.assertEqual(failures, {})                       # 重试后成功 ⇒ 不算失败
        self.assertEqual(len(dividends['US.PG']), 1)

    def test_persistent_failure_records_the_return_code(self):
        _, _, _, failures = collect_actions(_FakeCtx(fail={('splits', 'US.ORCL')}),
                                            ['US.ORCL'], retries=2, sleep=lambda s: None)
        self.assertIn('throttled', failures['US.ORCL']['splits'])


if __name__ == '__main__':
    unittest.main()
