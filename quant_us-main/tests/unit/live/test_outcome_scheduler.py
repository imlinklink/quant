"""outcome_scheduler 子进程捕获与调度解耦的确定性测试。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.live_trading.outcome_scheduler import OutcomeSchedulerThread, _run_subprocess


class RunSubprocessTests(unittest.TestCase):
    def test_failing_script_returns_code_and_logs_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'failing.py'
            script.write_text("import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n")
            with self.assertLogs('scripts.live_trading.outcome_scheduler', level='ERROR') as logs:
                code = _run_subprocess([sys.executable, str(script)])
        self.assertEqual(code, 3)
        self.assertTrue(any('boom' in line for line in logs.output))

    def test_success_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'ok.py'
            script.write_text("print('done')\n")
            code = _run_subprocess([sys.executable, str(script)])
        self.assertEqual(code, 0)

    def test_timeout_returns_minus_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'slow.py'
            script.write_text("import time\ntime.sleep(5)\n")
            code = _run_subprocess([sys.executable, str(script)], timeout=1)
        self.assertEqual(code, -1)


class SelectionReasonTests(unittest.TestCase):
    def _worker(self):
        worker = OutcomeSchedulerThread.__new__(OutcomeSchedulerThread)
        worker.config_path = 'config.yaml'
        return worker

    def test_selection_failure_still_runs_reconcile_and_reports_reason(self):
        worker = self._worker()
        calls = []

        def fake_run(cmd):
            calls.append(Path(cmd[1]).name)
            return 1 if Path(cmd[1]).name == 'run_daily_selection.py' else 0

        with patch('scripts.live_trading.outcome_scheduler._run_subprocess', side_effect=fake_run):
            code, reason = worker._selection('2026-09-14')
        self.assertEqual((code, reason), (1, 'selection_failed'))
        self.assertEqual(calls, ['run_daily_selection.py', 'reconcile_selection_decision.py'])

    def test_reconcile_failure_reports_reason(self):
        worker = self._worker()

        def fake_run(cmd):
            return 0 if Path(cmd[1]).name == 'run_daily_selection.py' else 1

        with patch('scripts.live_trading.outcome_scheduler._run_subprocess', side_effect=fake_run):
            code, reason = worker._selection('2026-09-14')
        self.assertEqual((code, reason), (1, 'reconcile_failed'))

    def test_success_returns_ok(self):
        worker = self._worker()
        with patch('scripts.live_trading.outcome_scheduler._run_subprocess', return_value=0):
            code, reason = worker._selection('2026-09-14')
        self.assertEqual((code, reason), (0, 'ok'))


if __name__ == '__main__':
    unittest.main()
