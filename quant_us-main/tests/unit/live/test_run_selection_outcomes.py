"""回填固定窗口结果工具回归：merge_bars 合并、run_outcomes 回填。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts.live_trading.run_selection_outcomes import merge_bars, run_outcomes


def _df(code, n=40, slope=0.01):
    dates = pd.date_range('2026-08-20', periods=n, freq='B', tz='UTC')
    closes = [100.0 * (1.0 + slope * i) for i in range(n)]
    return pd.DataFrame({'code': code, 'date': dates, 'open': closes,
                         'high': [x * 1.02 for x in closes],
                         'low': [x * 0.98 for x in closes], 'close': closes})


class _FakeFetcher:
    def fetch_multiple_stocks(self, codes, start, end):
        # 不同 code 给不同斜率，保证收益有区分、rank IC 可计算
        return {c: _df(c, slope=0.02 if i % 2 == 0 else 0.005)
                for i, c in enumerate(codes)}


class RunSelectionOutcomesContracts(unittest.TestCase):
    def test_merge_bars(self):
        bars = merge_bars({'US.A': _df('US.A'), 'US.B': _df('US.B'), 'US.C': None})
        self.assertEqual(set(bars['code']), {'US.A', 'US.B'})
        self.assertIn('date', bars.columns)
        self.assertGreater(len(bars), 0)

    def test_merge_bars_empty(self):
        bars = merge_bars({'US.A': None})
        self.assertEqual(list(bars.columns), ['code', 'date', 'open', 'high', 'low', 'close'])
        self.assertEqual(len(bars), 0)

    def test_run_outcomes_latest(self):
        from scripts.live_trading.llm_suggestions import store as sstore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        batch = {
            'research_batch_id': 'b1',
            'as_of': '2026-09-04T00:00:00+00:00',
            'universe': ['US.A', 'US.B'],
            'candidates': [
                {'code': 'US.A', 'rank': 1, 'missing_information': []},
                {'code': 'US.B', 'rank': 2, 'missing_information': []},
            ],
        }
        with patch.object(sstore, 'RESEARCH_BATCH_PATH', Path(tmp.name) / 'batches.jsonl'):
            sstore.save_research_batch(batch)
            results = run_outcomes({}, _FakeFetcher(), latest_only=True)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r['universe_size'], 2)
        self.assertEqual(r['ranked_count'], 2)
        for h in (1, 3, 5, 10):
            self.assertIn(h, r['horizons'])
            self.assertIsNotNone(r['horizons'][h]['rank_ic'])


if __name__ == '__main__':
    unittest.main()
