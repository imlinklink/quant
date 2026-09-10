import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.setup_scanner import SetupScanner


def bars(n=280):
    dates = pd.bdate_range('2025-01-01', periods=n, tz='UTC')
    close = np.linspace(100, 150, n)
    return pd.DataFrame({'date': dates, 'open': close, 'high': close + 1,
                         'low': close - 1, 'close': close,
                         'volume': np.ones(n) * 1000})


class SetupScannerTests(unittest.TestCase):
    def test_same_session_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 'state.db', 'DRY-RUN')
            scanner = SetupScanner(registry, {'buy_strategy_v2': {'min_daily_bars': 250}})
            as_of = '2026-03-01T23:00:00Z'
            first = scanner.scan_code('US.X', bars(), None, None, as_of)
            second = scanner.scan_code('US.X', bars(), None, None, as_of)
            self.assertEqual(first['state_snapshot_id'], second['state_snapshot_id'])
            self.assertEqual(first['state'], second['state'])


if __name__ == '__main__':
    unittest.main()
