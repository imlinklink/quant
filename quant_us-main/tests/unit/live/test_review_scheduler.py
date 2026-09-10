import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.review_scheduler import ReviewScheduler


class ReviewSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        registry = PositionRegistry(Path(self.tmp.name) / 'state.db', 'DRY-RUN')
        self.scheduler = ReviewScheduler(registry)

    def test_selection_slots_and_weekend(self):
        tz = ZoneInfo('America/New_York')
        self.assertEqual(
            self.scheduler.selection_slot(datetime(2026, 9, 9, 8, 45, tzinfo=tz)),
            'premarket')
        self.assertEqual(
            self.scheduler.selection_slot(datetime(2026, 9, 9, 16, 20, tzinfo=tz)),
            'postmarket')
        self.assertIsNone(
            self.scheduler.selection_slot(datetime(2026, 9, 12, 8, 45, tzinfo=tz)))

    def test_position_trigger_key_is_order_independent(self):
        a = self.scheduler.position_trigger_key('t1', 'new_event', ['b', 'a'], 'w1')
        b = self.scheduler.position_trigger_key('t1', 'new_event', ['a', 'b'], 'w1')
        self.assertEqual(a, b)

    def test_priority_orders_hard_exit_before_close_review(self):
        self.assertLess(self.scheduler.position_priority('hard_exit_post'),
                        self.scheduler.position_priority('scheduled_close'))


if __name__ == '__main__':
    unittest.main()
