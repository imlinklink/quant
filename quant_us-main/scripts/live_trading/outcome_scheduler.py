"""完整 DRY-RUN 服务中的 Outcome 日任务。任务只结算账本，不触发订单。"""
import logging
import subprocess
import sys
import threading
from pathlib import Path

from .position_registry import PositionRegistry
from .review_scheduler import ReviewScheduler

logger = logging.getLogger(__name__)


def _run_subprocess(cmd, *, timeout=1800):
    """运行子进程并捕获 stdout/stderr 落日志；失败/超时可追溯，返回退出码。"""
    try:
        proc = subprocess.run(cmd, check=False, timeout=timeout,
                              capture_output=True, encoding='utf-8', errors='replace')
    except subprocess.TimeoutExpired as exc:
        logger.error('子进程超时: %s', ' '.join(cmd))
        for stream, data in (('stdout', exc.stdout), ('stderr', exc.stderr)):
            if data:
                logger.error('%s(尾):\n%s', stream, data[-4000:])
        return -1
    if proc.returncode:
        logger.error('子进程失败: %s exit=%s', ' '.join(cmd), proc.returncode)
        if proc.stdout:
            logger.error('stdout(尾):\n%s', proc.stdout[-4000:])
        if proc.stderr:
            logger.error('stderr(尾):\n%s', proc.stderr[-4000:])
    else:
        logger.info('子进程成功: %s', ' '.join(cmd))
        if proc.stdout:
            logger.info('stdout(尾):\n%s', proc.stdout[-4000:])
        if proc.stderr:
            logger.warning('stderr(尾):\n%s', proc.stderr[-4000:])
    return proc.returncode


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
        self.integration = bool(config.get('shadow_integration', {}).get('enabled', False))
        if self.integration and (engine.get('account_scope') != 'DRY-RUN' or
                                 engine.get('selection') != 'shadow' or
                                 config.get('buy_strategy_v2', {}).get('mode') != 'shadow'):
            raise ValueError('SHADOW_INTEGRATION_REQUIRES_DRY_RUN_SHADOW')
        from .shadow_jobs import ShadowJobs
        self.jobs = ShadowJobs(self.scheduler.events)

    def _selection(self, day):
        sel_code = _run_subprocess([sys.executable, str(Path(__file__).with_name('run_daily_selection.py')),
                                    '--config', self.config_path])
        rec_code = _run_subprocess([sys.executable, str(Path(__file__).with_name('reconcile_selection_decision.py')),
                                    '--config', self.config_path, '--session', day])
        if sel_code:
            return sel_code, 'selection_failed'
        if rec_code:
            return rec_code, 'reconcile_failed'
        return 0, 'ok'

    def _run(self):
        return _run_subprocess([sys.executable, str(Path(__file__).with_name('run_outcomes.py')),
                                '--config', self.config_path])

    def _run_setups(self):
        return _run_subprocess([sys.executable, str(Path(__file__).with_name('run_daily_setups.py')),
                                '--config', self.config_path, '--json'])

    def tick(self, now=None):
        if self.integration:
            return self._integration_tick(now)
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

    def _integration_tick(self, now=None):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from scripts.data.trading_calendar import sessions
        local = now or datetime.now(ZoneInfo('America/New_York'))
        if local.tzinfo is None:
            local = local.replace(tzinfo=ZoneInfo('America/New_York'))
        local = local.astimezone(ZoneInfo('America/New_York'))
        if sessions(local.date(), local.date()).empty:
            return False
        ran = False
        day = local.date().isoformat()
        if (local.hour, local.minute) >= (16, 20):
            try:
                ran |= self.jobs.execute('selection_and_reconcile', day,
                                         lambda: self._selection(day))
            except Exception:
                logger.exception('selection_and_reconcile 任务异常（不阻断后续任务）')
        if self.scheduler.setup_due(local):
            ran |= self.jobs.execute('daily_setup_shadow', day, self.setup_runner, max_attempts=3)
        if self.scheduler.outcome_due(local):
            ran |= self.jobs.execute('selection_outcomes', day, self.runner, max_attempts=3)
        return ran

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        def loop():
            while not self.stop_event.wait(30):
                try:
                    self.tick()
                except Exception:
                    logger.exception('日任务失败；状态已记录，调度线程继续运行')
        self.thread = threading.Thread(target=loop, name='selection-outcomes', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
