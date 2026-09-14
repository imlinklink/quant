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
    def test_conflict_is_reported_without_dropping_later_stocks(self):
        from unittest.mock import patch
        scanner = SetupScanner.__new__(SetupScanner)
        with patch.object(scanner, 'scan_code', side_effect=[
                ValueError('不可覆盖历史快照'), {'code': 'US.B', 'quality': {'status': 'pass'}}]):
            rows = scanner.scan({'US.A': None, 'US.B': None}, {}, None, '2026-09-13')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['quality']['reason'], 'IMMUTABLE_SNAPSHOT_CONFLICT')
        self.assertEqual(rows[1]['code'], 'US.B')

    def test_same_session_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 'state.db', 'DRY-RUN')
            scanner = SetupScanner(registry, {'buy_strategy_v2': {'min_daily_bars': 250}})
            as_of = '2026-03-01T23:00:00Z'
            first = scanner.scan_code('US.X', bars(), None, None, as_of)
            second = scanner.scan_code('US.X', bars(), None, None, as_of)
            self.assertEqual(first['state_snapshot_id'], second['state_snapshot_id'])
            self.assertEqual(first['state'], second['state'])
            third = scanner.scan_code('US.X', bars(), None, None, '2026-03-02T23:00:00Z')
            self.assertEqual(first['state_snapshot_id'], third['state_snapshot_id'])


if __name__ == '__main__':
    unittest.main()
