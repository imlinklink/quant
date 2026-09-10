"""完整 DRY-RUN 服务中的 Outcome 日任务。任务只结算账本，不触发订单。"""
import logging
import subprocess
import sys
import threading
from pathlib import Path

from .position_registry import PositionRegistry
from .review_scheduler import ReviewScheduler

logger = logging.getLogger(__name__)


class OutcomeSchedulerThread:
    def __init__(self, config: dict, config_path: str, stop_event=None, runner=None,
                 setup_runner=None):
        engine = config.get('llm_decision', {}).get('engine_v2', {})
        self.registry = PositionRegistry(namespace=engine.get('account_scope', 'DRY-RUN'))
        self.scheduler = ReviewScheduler(self.registry, config)
        self.config_path = str(config_path)
        self.stop_event = stop_event or threading.Event()
        self.runner = runner or self._run
        self.setup_runner = setup_runner or self._run_setups
        self.thread = None

    def _run(self):
        script = Path(__file__).with_name('run_outcomes.py')
        return subprocess.run([sys.executable, str(script), '--config', self.config_path],
                              check=False, timeout=1800).returncode

    def _run_setups(self):
        script = Path(__file__).with_name('run_daily_setups.py')
        return subprocess.run([sys.executable, str(script), '--config', self.config_path,
                               '--json'], check=False, timeout=1800).returncode

    def tick(self, now=None):
        ran = False
        setup_date = self.scheduler.setup_due(now)
        if setup_date and self.scheduler.claim_daily_job('daily_setup_shadow', setup_date):
            ran = True
            try:
                code = self.setup_runner()
                logger.info('Daily setup shadow 完成: session=%s exit=%s', setup_date, code)
            except Exception:
                logger.exception('Daily setup shadow 失败: session=%s', setup_date)
        session_date = self.scheduler.outcome_due(now)
        if not session_date or not self.scheduler.claim_daily_job('selection_outcomes', session_date):
            return ran
        try:
            code = self.runner()
            logger.info('Selection Outcome 日任务完成: session=%s exit=%s', session_date, code)
        except Exception:
            logger.exception('Selection Outcome 日任务失败: session=%s', session_date)
        return True

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        def loop():
            while not self.stop_event.wait(30):
                self.tick()
        self.thread = threading.Thread(target=loop, name='selection-outcomes', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
