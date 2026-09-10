"""调度设计（技术设计 §14）：selection 盘前/盘后、entry 信号触发、position 去重队列。

本模块只做「何时触发 + 去重 + 优先级」，不调用模型、不执行订单。
"""
import logging
from datetime import datetime, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from .decision_ledger.event_store import EventStore, stable_id, utc

logger = logging.getLogger(__name__)

DEFAULT_TZ = 'America/New_York'

# position 触发器优先级（§14.3）：数字越小越优先
POSITION_PRIORITY = {
    'hard_exit_post': 1,      # 硬退出事后复核
    'new_event': 2,           # 新重大事件
    'thesis_near': 3,         # 论文接近失效条件
    'option_change': 4,       # 期权显著变化
    'scheduled_close': 5,     # 定时收盘复核
}


class ReviewScheduler:
    """定时与事件驱动复核调度（只读判定 + 幂等键）。"""

    def __init__(self, registry, config: Optional[dict] = None):
        self.events = EventStore(registry)
        self.config = config or {}
        self.scope = registry.namespace

    # ---------- Selection（§14.1） ----------

    def selection_slot(self, now: Optional[datetime] = None,
                       tz: str = DEFAULT_TZ) -> Optional[str]:
        """返回当前应运行的选股时段：premarket / postmarket / None（非交易日或未到点）。"""
        now = now or datetime.now(ZoneInfo(tz))
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo(tz))
        now = now.astimezone(ZoneInfo(tz))
        if now.weekday() >= 5:
            return None
        schedule = (self.config.get('llm_decision', {}).get('selection', {}).get('schedule')
                    or {'premarket': '08:45', 'postmarket': '16:20'})
        for slot, hm in schedule.items():
            hh, mm = (int(x) for x in str(hm).split(':')[:2])
            if now.hour == hh and now.minute == mm:
                return slot
        return None

    # ---------- Entry（§14.2） ----------

    def entry_key(self, signal_id: str, plan_version) -> str:
        return stable_id('entry_decision', self.scope, signal_id, str(plan_version))

    def has_active_entry(self, signal_id: str, plan_version) -> bool:
        """相同 signal_id + plan_version 是否已有活跃 Entry Decision。"""
        with self.events.transaction() as con:
            row = con.execute(
                'SELECT 1 FROM llm_decision_runs WHERE account_scope=? AND role=? '
                'AND subject_id=? AND status=?',
                (self.scope, 'entry', signal_id, 'validated')).fetchone()
        return row is not None

    # ---------- Position 去重 + 优先级（§14.3） ----------

    def position_trigger_key(self, trade_id: str, trigger_type: str,
                             cluster_ids: Optional[List[str]] = None,
                             time_bucket: str = '') -> str:
        return stable_id('position_review_trigger', trade_id, trigger_type,
                         sorted(cluster_ids or []), time_bucket)

    def position_priority(self, trigger_type: str) -> int:
        return POSITION_PRIORITY.get(trigger_type, 5)

    def trigger_seen(self, trigger_key: str) -> bool:
        with self.events.transaction() as con:
            row = con.execute(
                "SELECT 1 FROM decision_events WHERE account_scope=? AND event_type='position_review_triggered' "
                "AND event_id=?", (self.scope, stable_id('event', self.scope, 'position_review_triggered', trigger_key))).fetchone()
        return row is not None
