"""run_outcomes 结算 runner 测试（T2，§16）。"""
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.live_trading.decision_ledger.outcome_jobs import OutcomeSettlement
from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.run_outcomes import (
    code_close_series, position_future_bars, run, settle_entry_signal,
    settle_entry_counterfactuals, settle_position_counterfactuals,
    settle_position_decision, settle_selection_counterfactuals,
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
        result = run(self.registry, [batch], self.bars)
        self.assertEqual(result, {'settled': 10, 'pending': 0})  # 2 codes × 5 horizons
        with settlement.events.transaction() as con:
            rows = con.execute(
                'SELECT subject_key FROM decision_outcomes_v2 WHERE account_scope=? AND decision_id=? AND horizon=?',
                (self.registry.namespace, 'batch1', '1d')).fetchall()
        self.assertEqual(sorted(r[0] for r in rows), ['US.A', 'US.B'])

    def test_rerun_is_idempotent(self):
        batch = {'universe': ['US.A', 'US.B'], 'as_of': self.as_of,
                 'research_batch_id': 'batch1'}
        first = run(self.registry, [batch], self.bars)
        second = run(self.registry, [batch], self.bars)
        # 首次结算 2 只 × 5 期限；重跑**一条也不写**。
        # `settled` 的语义是"真写入了多少"，不是"算出了多少" —— 旧口径下重跑也报 10，
        # 于是"每天报 10 条、实际一条没写"看起来正常，把已结算不再重算这件事盖住了。
        self.assertEqual(first, {'settled': 10, 'pending': 0})
        self.assertEqual(second, {'settled': 0, 'pending': 0})
        settlement = OutcomeSettlement(self.registry)
        with settlement.events.transaction() as con:
            count = con.execute(
                "SELECT COUNT(*) FROM decision_outcomes_v2 WHERE decision_id='batch1'"
            ).fetchone()[0]
            observed = con.execute(
                "SELECT COUNT(*) FROM decision_events WHERE event_type='outcome_observed'"
            ).fetchone()[0]
        self.assertEqual(count, 10)      # 重复运行不重复记账
        self.assertEqual(observed, 10)   # 也不重复写事件（重跑不新增结算事件）

    def test_v2_decision_id_and_pending_horizons(self):
        short = self.bars[self.bars['date'] <= pd.Timestamp('2026-01-12', tz='UTC')]
        batch = {'universe': ['US.A'], 'as_of': self.as_of,
                 'research_batch_id': 'batch1', 'decision_id': 'decision1'}
        self.assertEqual(run(self.registry, [batch], short),
                         {'settled': 2, 'pending': 3})
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

    def test_position_counterfactual_starts_after_decision_market_day(self):
        frozen = {'code': 'US.A', 'as_of': '2026-01-10T14:00:00+00:00'}
        future = position_future_bars(self.bars, frozen)
        # Jan 10 当日即使尚未收盘，也不能拿已经发生的日线开盘作为成交价。
        self.assertEqual(pd.Timestamp(future[0]['date']).date().isoformat(), '2026-01-11')

    def test_position_counterfactual_batch_settlement(self):
        frozen = {
            'counterfactual_id': 'cf1', 'experiment_version': 'position-counterfactual-v1',
            'decision_id': 'd1', 'trade_id': 't1', 'code': 'US.A',
            'as_of': '2026-01-10T14:00:00+00:00', 'starting_quantity': 10,
            'reference_price': 109, 'active_stop': 90, 'hard_exit_authoritative': True,
            'fee_rate': 0, 'l_path': {'action': 'reduce', 'quantity': 5},
        }
        result = settle_position_counterfactuals(self.registry, [frozen], self.bars)
        self.assertEqual(result, {'experiments': 1, 'settled_horizons': 4, 'pending': 1})
        # 同一批数据重复结算保持幂等；补足第 20 根后只新增 20d 投影。
        self.assertEqual(settle_position_counterfactuals(
            self.registry, [frozen], self.bars), result)
        extra = _bars('US.A', ['2026-01-30'], [129])
        completed = settle_position_counterfactuals(
            self.registry, [frozen], pd.concat([self.bars, extra], ignore_index=True))
        self.assertEqual(completed, {'experiments': 1, 'settled_horizons': 5, 'pending': 0})
        with OutcomeSettlement(self.registry).events.transaction() as con:
            count = con.execute(
                "SELECT COUNT(*) FROM decision_outcomes_v2 WHERE subject_key='t1:position_cf'"
            ).fetchone()[0]
        self.assertEqual(count, 5)

    def test_entry_counterfactual_batch_settlement(self):
        frozen = {
            'counterfactual_id': 'ecf1', 'experiment_version': 'entry-counterfactual-v1',
            'decision_id': 'ed1', 'signal_id': 's1', 'code': 'US.A',
            'as_of': '2026-01-10T14:00:00+00:00',
            'rule_path': {'action': 'execute_now', 'template': {
                'kind': 'standard', 'quantity': 10, 'initial_stop': 90}},
            'llm_path': {'action': 'reject', 'template': {
                'kind': 'reject', 'quantity': 0, 'initial_stop': 90}},
        }
        result = settle_entry_counterfactuals(self.registry, [frozen], self.bars)
        self.assertEqual(result, {'experiments': 1, 'settled_horizons': 4, 'pending': 1})
        with OutcomeSettlement(self.registry).events.transaction() as con:
            count = con.execute(
                "SELECT COUNT(*) FROM decision_outcomes_v2 WHERE subject_key='s1:entry_cf'"
            ).fetchone()[0]
        self.assertEqual(count, 4)

    def test_selection_counterfactual_batch_settlement(self):
        frozen = {
            'counterfactual_id': 'scf1', 'experiment_version': 'selection-capacity-counterfactual-v1',
            'decision_id': 'sd1', 'batch_id': 'b1', 'as_of': '2026-01-10T14:00:00+00:00',
            'rule_selected': ['US.A'], 'llm_selected': ['US.B'],
            'replaced_out': ['US.A'], 'replaced_in': ['US.B'],
        }
        result = settle_selection_counterfactuals(self.registry, [frozen], self.bars)
        self.assertEqual(result, {'experiments': 1, 'settled_horizons': 4, 'pending': 1})
        with OutcomeSettlement(self.registry).events.transaction() as con:
            row = con.execute(
                "SELECT excess_return_pct FROM decision_outcomes_v2 WHERE subject_key='b1:selection_cf' "
                "AND horizon='3d'").fetchone()
        self.assertLess(row[0], 0.0)


if __name__ == '__main__':
    unittest.main()
