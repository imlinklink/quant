import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.decision_ledger.event_store import EventStore
from scripts.live_trading.shadow_jobs import ShadowJobs


class ShadowJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = EventStore(PositionRegistry(Path(self.tmp.name) / 'test.db', 'DRY-RUN'))
        self.jobs = ShadowJobs(self.events)

    def tearDown(self):
        self.tmp.cleanup()

    def test_success_and_running_survive_restart_without_duplicate(self):
        claim = self.jobs.claim('selection', '2026-09-14')
        other = ShadowJobs(self.events)
        self.assertIsNone(other.claim('selection', '2026-09-14'))
        self.jobs.finish(claim, 0)
        self.assertTrue(other.succeeded('selection', '2026-09-14'))
        self.assertIsNone(other.claim('selection', '2026-09-14'))

    def test_failure_retries_are_bounded_and_cooled_down(self):
        for i in range(3):
            with patch('scripts.live_trading.shadow_jobs.time.time', return_value=i * 301):
                claim = self.jobs.claim('setup', '2026-09-14', max_attempts=3)
                self.assertEqual(claim['attempt'], i + 1)
                self.jobs.finish(claim, 1)
                self.assertIsNone(self.jobs.claim('setup', '2026-09-14', max_attempts=3))
        self.assertIsNone(self.jobs.claim('setup', '2026-09-14', max_attempts=3, now=10000))

    def test_model_failure_is_not_automatically_recalled(self):
        claim = self.jobs.claim('selection', '2026-09-14', now=0)
        self.jobs.finish(claim, 1)
        self.assertIsNone(self.jobs.claim('selection', '2026-09-14', now=1e20))

    def test_setup_weekend_window_is_anchored_to_completed_session(self):
        from scripts.live_trading.run_daily_setups import run
        from unittest.mock import MagicMock
        fetcher = MagicMock()
        fetcher.fetch_multiple_stocks.return_value = {}
        config = {'buy_strategy_v2': {'watch_list': ['US.X']}}
        with patch('scripts.live_trading.run_daily_setups.PositionRegistry'), \
             patch('scripts.live_trading.run_daily_setups.SetupScanner'), \
             patch('scripts.live_trading.llm_suggestions.store.load_latest_research_batch', return_value={}):
            run(config, fetcher, '2026-09-12T23:00:00+00:00')
            first = fetcher.fetch_multiple_stocks.call_args
            run(config, fetcher, '2026-09-13T23:00:00+00:00')
            self.assertEqual(first, fetcher.fetch_multiple_stocks.call_args)
            self.assertEqual(first.args[-1], '2026-09-11')

    def test_scheduler_orders_jobs_and_blocks_setup_after_selection_failure(self):
        from datetime import datetime
        from scripts.live_trading.outcome_scheduler import OutcomeSchedulerThread
        from scripts.live_trading.review_scheduler import ReviewScheduler
        registry = self.events.registry
        config = {'buy_strategy_v2': {'enabled': True, 'mode': 'shadow'},
                  'llm_decision': {'outcomes': {'enabled': True}}}
        worker = OutcomeSchedulerThread.__new__(OutcomeSchedulerThread)
        worker.scheduler = ReviewScheduler(registry, config)
        worker.jobs = self.jobs
        calls = []
        worker._selection = lambda: calls.append('selection') or 1
        worker.setup_runner = lambda: calls.append('setup') or 0
        worker.runner = lambda: calls.append('outcome') or 0
        worker._integration_tick(datetime.fromisoformat('2026-09-14T18:00:00-04:00'))
        self.assertEqual(calls, ['selection', 'outcome'])
        calls.clear()
        worker._selection = lambda: calls.append('selection') or 0
        worker._integration_tick(datetime.fromisoformat('2026-09-15T18:00:00-04:00'))
        self.assertEqual(calls, ['selection', 'setup', 'outcome'])
        calls.clear()
        worker._integration_tick(datetime.fromisoformat('2026-09-15T18:01:00-04:00'))
        worker._integration_tick(datetime.fromisoformat('2026-12-25T18:00:00-05:00'))
        self.assertEqual(calls, [])
