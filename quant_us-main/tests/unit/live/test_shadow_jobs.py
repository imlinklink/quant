import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.decision_ledger.event_store import EventStore
from scripts.live_trading.shadow_jobs import ShadowJobs, incomplete_jobs


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
             patch('scripts.live_trading.llm_suggestions.store.load_research_batch_for_session', return_value=None):
            run(config, fetcher, '2026-09-12T23:00:00+00:00')
            first = fetcher.fetch_multiple_stocks.call_args
            run(config, fetcher, '2026-09-13T23:00:00+00:00')
            self.assertEqual(first, fetcher.fetch_multiple_stocks.call_args)
            self.assertEqual(first.args[-1], '2026-09-11')

    def test_scheduler_runs_setup_after_selection_failure(self):
        from datetime import datetime
        from scripts.live_trading.outcome_scheduler import OutcomeSchedulerThread
        from scripts.live_trading.protocol_review import ProtocolReviewScheduler
        from scripts.live_trading.review_scheduler import ReviewScheduler
        registry = self.events.registry
        config = {'buy_strategy_v2': {'enabled': True, 'mode': 'shadow'},
                  'llm_decision': {'outcomes': {'enabled': True}}}
        worker = OutcomeSchedulerThread.__new__(OutcomeSchedulerThread)
        worker.scheduler = ReviewScheduler(registry, config)
        worker.jobs = self.jobs
        # `__new__` 绕过了 `__init__`，协作者要显式给全 —— 缺一个就会在 tick 里
        # AttributeError，而不是"安静地跳过"（后者更糟）。
        worker.config = config
        worker.protocol_reviewer = ProtocolReviewScheduler(registry, config)
        calls = []
        worker._selection = lambda day: calls.append('selection') or 1
        worker.setup_runner = lambda: calls.append('setup') or 0
        worker.runner = lambda: calls.append('outcome') or 0
        worker._integration_tick(datetime.fromisoformat('2026-09-14T18:00:00-04:00'))
        # selection 失败（返回 1）时 setup 仍应运行，不再被 success 闸门阻断
        self.assertEqual(calls, ['selection', 'setup', 'outcome'])
        calls.clear()
        worker._selection = lambda day: calls.append('selection') or 0
        worker._integration_tick(datetime.fromisoformat('2026-09-15T18:00:00-04:00'))
        self.assertEqual(calls, ['selection', 'setup', 'outcome'])
        calls.clear()
        worker._integration_tick(datetime.fromisoformat('2026-09-15T18:01:00-04:00'))
        worker._integration_tick(datetime.fromisoformat('2026-12-25T18:00:00-05:00'))
        self.assertEqual(calls, [])

    def test_scheduler_isolates_selection_exception(self):
        from datetime import datetime
        from scripts.live_trading.outcome_scheduler import OutcomeSchedulerThread
        from scripts.live_trading.protocol_review import ProtocolReviewScheduler
        from scripts.live_trading.review_scheduler import ReviewScheduler
        registry = self.events.registry
        config = {'buy_strategy_v2': {'enabled': True, 'mode': 'shadow'},
                  'llm_decision': {'outcomes': {'enabled': True}}}
        worker = OutcomeSchedulerThread.__new__(OutcomeSchedulerThread)
        worker.scheduler = ReviewScheduler(registry, config)
        worker.jobs = self.jobs
        # `__new__` 绕过了 `__init__`，协作者要显式给全 —— 缺一个就会在 tick 里
        # AttributeError，而不是"安静地跳过"（后者更糟）。
        worker.config = config
        worker.protocol_reviewer = ProtocolReviewScheduler(registry, config)
        calls = []
        worker._selection = lambda day: (_ for _ in ()).throw(RuntimeError('boom'))
        worker.setup_runner = lambda: calls.append('setup') or 0
        worker.runner = lambda: calls.append('outcome') or 0
        worker._integration_tick(datetime.fromisoformat('2026-09-14T18:00:00-04:00'))
        self.assertEqual(calls, ['setup', 'outcome'])

    def test_execute_records_reason_from_tuple(self):
        import json
        self.jobs.execute('selection', '2026-09-14', lambda: (1, 'selection_failed'))
        with self.events.transaction() as con:
            rows = con.execute(
                "SELECT body FROM decision_events WHERE event_type='shadow_job_finished'"
            ).fetchall()
        payload = json.loads(rows[0][0])['payload']
        self.assertEqual(payload['reason'], 'selection_failed')
        self.assertEqual(payload['exit_code'], 1)
        self.assertEqual(payload['status'], 'failed')


class ForceRecallTests(ShadowJobTests):
    """`force` 是**人工补跑出口**：只放开"重试耗尽"与退避，**不放开"已经成功过"**。

    为什么需要它：`max_attempts` 用完后 `claim` 恒返回 None ⇒ **失败的 session 永久不再补**，
    而"三次都撞上同一个瞬时故障"完全可能（2026-09-19 的 09-18 session 就是这样）。
    """

    def _exhaust(self, job='setup', session='2026-09-18'):
        for i in range(3):
            with patch('scripts.live_trading.shadow_jobs.time.time', return_value=i * 301):
                claim = self.jobs.claim(job, session, max_attempts=3)
                self.jobs.finish(claim, 1)          # 三次都失败
        return job, session

    def test_exhausted_job_cannot_be_claimed_without_force(self):
        job, session = self._exhaust()
        self.assertIsNone(self.jobs.claim(job, session, max_attempts=3, now=1e20))

    def test_force_claims_an_exhausted_job(self):
        job, session = self._exhaust()
        claim = self.jobs.claim(job, session, max_attempts=3, now=1e20, force=True)
        self.assertIsNotNone(claim)
        self.assertEqual(claim['attempt'], 4)       # 接着往下记，不重置计数
        self.assertTrue(claim['forced'])

    def test_force_does_not_reopen_a_succeeded_job(self):
        """**这条是 force 的边界**：重跑一个成功过的作业会重复写事件 —— 那不是补跑。"""
        claim = self.jobs.claim('setup', '2026-09-18', now=0)
        self.jobs.finish(claim, 0)
        self.assertIsNone(self.jobs.claim('setup', '2026-09-18', now=1e20, force=True))

    def test_force_does_not_reopen_a_running_job(self):
        """仍在跑（可能只是慢）的作业也不能被抢占 —— 那会造成两个进程同时写。"""
        self.jobs.claim('setup', '2026-09-18', now=0)
        self.assertIsNone(self.jobs.claim('setup', '2026-09-18', now=1e20, force=True))


class IncompleteJobsTests(ShadowJobTests):
    """`incomplete_jobs`：**没有记录**也要报 —— 那是最容易被漏掉的一类。"""

    SESSIONS = ['2026-09-16', '2026-09-17']
    JOBS = ('a', 'b')

    def test_missing_records_are_reported(self):
        """服务当时没在跑 ⇒ 一个事件都没有 ⇒ 而"没有事件"不会出现在任何报表里。"""
        gaps = incomplete_jobs(self.events, self.SESSIONS, jobs=self.JOBS)
        self.assertEqual(len(gaps), 4)
        self.assertTrue(all(st is None for _, _, st in gaps))

    def test_failed_and_running_are_reported_but_succeeded_is_not(self):
        c = self.jobs.claim('a', '2026-09-16', now=0)
        self.jobs.finish(c, 1)                       # failed
        self.jobs.claim('b', '2026-09-16', now=0)    # running
        c = self.jobs.claim('a', '2026-09-17', now=0)
        self.jobs.finish(c, 0)                       # succeeded
        gaps = incomplete_jobs(self.events, self.SESSIONS, jobs=self.JOBS)
        # 09-17 的 'a' 成功过 ⇒ 不出现在缺口里；其余三条分别是 failed / running / 无记录。
        self.assertEqual(gaps,
                         [('2026-09-16', 'a', 'failed'),
                          ('2026-09-16', 'b', 'running'),
                          ('2026-09-17', 'b', None)])

    def test_all_succeeded_reports_nothing(self):
        for session in self.SESSIONS:
            for job in self.JOBS:
                c = self.jobs.claim(job, session, now=0)
                self.jobs.finish(c, 0)
        self.assertEqual(incomplete_jobs(self.events, self.SESSIONS, jobs=self.JOBS), [])


class CurrentSessionTests(unittest.TestCase):
    """`current_session()`：三个 runner **现在**会算的那个 session。

    它是补跑护栏的依据 —— 补历史 session 会算出今天的东西却把那天的 claim 记成成功，
    **账本会说谎**。所以"当前 session"的判定必须准。
    """

    def _at(self, y, m, d, hh):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(y, m, d, hh, 0, tzinfo=ZoneInfo('America/New_York'))

    def test_uses_the_latest_session_whose_close_has_passed(self):
        from scripts.live_trading.retry_shadow_job import current_session
        # 周五 12:00 纽约：周五还没收盘 ⇒ 最新"已收盘"是周四
        self.assertEqual(current_session(self._at(2026, 9, 18, 12)), '2026-09-17')
        # 周五 18:00 纽约：已过 16:00 ⇒ 就是周五
        self.assertEqual(current_session(self._at(2026, 9, 18, 18)), '2026-09-18')

    def test_weekend_falls_back_to_friday(self):
        from scripts.live_trading.retry_shadow_job import current_session
        self.assertEqual(current_session(self._at(2026, 9, 19, 12)), '2026-09-18')   # 周六
        self.assertEqual(current_session(self._at(2026, 9, 20, 12)), '2026-09-18')   # 周日
