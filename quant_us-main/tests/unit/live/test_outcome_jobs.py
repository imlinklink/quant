import tempfile
import unittest
from pathlib import Path

from scripts.live_trading.decision_ledger.outcome_jobs import (
    OutcomeSettlement, selection_outcomes_for_code, simulate_trade,
)
from scripts.live_trading.position_registry import PositionRegistry


class OutcomeJobsTests(unittest.TestCase):
    def test_gap_through_stop_uses_observed_price(self):
        out = simulate_trade(100.0, 95.0, None, [80.0, 90.0])
        self.assertEqual(out['exit_reason'], 'stop')
        self.assertEqual(out['exit_price'], 80.0)

    def test_short_history_does_not_publish_unobserved_horizons(self):
        out = selection_outcomes_for_code('A', [100.0, 101.0])
        # 基准 + 1 个未来交易日，只能产生 1d。
        self.assertEqual([row['horizon'] for row in out], ['1d'])
        self.assertAlmostEqual(out[0]['return_pct'], 0.01)

    def test_horizon_uses_exact_future_close(self):
        closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        benchmark = [200.0, 202.0, 204.0, 206.0, 208.0, 210.0]
        out = {row['horizon']: row for row in
               selection_outcomes_for_code('A', closes, benchmark)}
        self.assertAlmostEqual(out['1d']['return_pct'], 0.01)
        self.assertAlmostEqual(out['3d']['return_pct'], 0.03)
        self.assertAlmostEqual(out['5d']['return_pct'], 0.05)
        self.assertAlmostEqual(out['3d']['benchmark_return_pct'], 0.03)

    def test_two_codes_are_persisted_independently(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = PositionRegistry(Path(tmp.name) / 'state.db', 'DRY-RUN')
        settlement = OutcomeSettlement(registry)
        settlement.settle_selection('d1', 'A', list(range(100, 121)))
        settlement.settle_selection('d1', 'B', list(range(200, 221)))
        with settlement.events.transaction() as con:
            count = con.execute(
                'SELECT COUNT(*) FROM decision_outcomes_v2 WHERE decision_id=?',
                ('d1',)).fetchone()[0]
        self.assertEqual(count, 10)


if __name__ == '__main__':
    unittest.main()
