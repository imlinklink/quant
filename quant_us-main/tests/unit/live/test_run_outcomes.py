"""run_outcomes 结算 runner 测试（T2，§16）。"""
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.live_trading.decision_ledger.outcome_jobs import OutcomeSettlement
from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.run_outcomes import (
    code_close_series, run, settle_entry_signal, settle_position_decision,
)


def _bars(code, dates, closes):
    return pd.DataFrame({
        'code': [code] * len(dates),
        'date': pd.to_datetime(dates, utc=True),
        'open': closes, 'high': [c + 1 for c in closes],
        'low': [c - 1 for c in closes], 'close': closes,
    })


class RunOutcomesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 's.db', 'DRY-RUN')
        dates = [f'2026-01-{d:02d}' for d in range(1, 30)]
        closes_a = [100 + i for i in range(len(dates))]
        closes_b = [200 - i for i in range(len(dates))]
        self.bars = pd.concat([_bars('US.A', dates, closes_a),
                               _bars('US.B', dates, closes_b)], ignore_index=True)
        self.as_of = '2026-01-10T12:00:00+00:00'

    def test_code_close_series(self):
        closes = code_close_series(self.bars, 'US.A', self.as_of)
        # 基准 = 2026-01-09 收盘（range(1,30) 下 Jan 9 为第 9 根，close=108），后续 20 根
        self.assertEqual(closes[0], 108.0)
        self.assertEqual(len(closes), 21)
        # 无未来数据的 as_of → 空
        self.assertEqual(code_close_series(self.bars, 'US.A', '2026-01-30T00:00:00+00:00'), [])

    def test_settle_selection_writes_per_code(self):
        settlement = OutcomeSettlement(self.registry)
        batch = {'universe': ['US.A', 'US.B'], 'as_of': self.as_of,
                 'research_batch_id': 'batch1'}
        n = run(self.registry, [batch], self.bars)
        self.assertEqual(n, 10)  # 2 codes × 5 horizons
        with settlement.events.transaction() as con:
            rows = con.execute(
                'SELECT subject_key FROM decision_outcomes_v2 WHERE account_scope=? AND decision_id=? AND horizon=?',
                (self.registry.namespace, 'batch1', '1d')).fetchall()
        self.assertEqual(sorted(r[0] for r in rows), ['US.A', 'US.B'])

    def test_v2_decision_id_and_pending_horizons(self):
        short = self.bars[self.bars['date'] <= pd.Timestamp('2026-01-12', tz='UTC')]
        batch = {'universe': ['US.A'], 'as_of': self.as_of,
                 'research_batch_id': 'batch1', 'decision_id': 'decision1'}
        self.assertEqual(run(self.registry, [batch], short), 5)
        settlement = OutcomeSettlement(self.registry)
        with settlement.events.transaction() as con:
            rows = con.execute(
                'SELECT horizon,data_quality FROM decision_outcomes_v2 '
                'WHERE account_scope=? AND decision_id=? ORDER BY horizon',
                (self.registry.namespace, 'decision1')).fetchall()
        self.assertEqual(len(rows), 5)
        self.assertIn(('5d', 'pending_future_bars'), rows)

    def test_settle_entry_and_position(self):
        from mutifactor.llm.contracts.entry_v2 import build_entry_templates
        settlement = OutcomeSettlement(self.registry)
        closes = [100.0, 102.0, 98.0, 95.0]
        plan = {'plan_id': 'p1', 'stock_code': 'US.A',
                'risk': {'initial_stop': 90.0}, 'entry_constraints': {'price': 100.0}}
        templates = build_entry_templates(plan=plan, standard_quantity=100, entry_price=100.0,
                                          initial_stop=90.0,
                                          expires_at='2999-01-01T00:00:00+00:00')
        n = settle_entry_signal('dec_entry', 100.0, templates, closes, settlement)
        self.assertEqual(n, 4)  # 4 个模板各一条
        trade = {'trade_id': 't1', 'entry_price': 100.0, 'remaining_qty': 100.0,
                 'protection': {'active_stop': 90.0}}
        n2 = settle_position_decision('dec_pos', trade, 95.0, 92.0, 93.0, closes, settlement)
        self.assertEqual(n2, 1)


if __name__ == '__main__':
    unittest.main()
