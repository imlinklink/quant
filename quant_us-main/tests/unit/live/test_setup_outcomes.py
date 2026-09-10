import tempfile
import unittest
from pathlib import Path

from scripts.live_trading.decision_ledger.outcome_jobs import OutcomeSettlement
from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.setup_outcomes import settle_setup


class SetupOutcomeTests(unittest.TestCase):
    def test_complete_and_pending_horizons_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 'state.db', 'DRY-RUN')
            settlement = OutcomeSettlement(registry)
            n = settle_setup({'setup_id': 's1', 'code': 'US.X',
                              'strategy': 'reversal_confirmed'},
                             [100, 101, 102, 103], settlement,
                             benchmark_closes=[200, 201, 202, 203])
            self.assertEqual(n, 6)
            with settlement.events.transaction() as con:
                rows = con.execute(
                    'SELECT horizon,data_quality FROM decision_outcomes_v2 '
                    'WHERE decision_id=?', ('s1',)).fetchall()
            self.assertIn(('1d', 'good'), rows)
            self.assertIn(('5d', 'pending_future_bars'), rows)


if __name__ == '__main__':
    unittest.main()
