"""PR1 硬退出路由：hard_risk 退出不同步依赖 LLM/人工确认。

设计目标（对应 llm-trading-evolution-technical-design.md §10）：
  - 固定止损 / 移动止损 / 组合熔断 / 券商风险 = hard_risk：程序直达执行器；
  - 论文退出 / 事件风险 = thesis：仍进确认台；
  - 定时复核 = scheduled_review：按调度。

本模块先交付 DRY-RUN 可测试核心：幂等 intent、占用待卖、成交冲正；
真实券商下单通过 `HardExitRouter` 的 `dry_run=False` 路径（需 OpenD），
未在监控器接活前不改变现网行为。
"""
import logging
import time
from typing import Dict, Optional

from .decision_ledger.event_store import EventStore, enqueue, stable_id
from .position_registry import REGISTRY

logger = logging.getLogger(__name__)

# 硬风险退出：程序满足条件即执行，不等待模型/人工
HARD_RISK_REASONS = frozenset({'fixed_stop', 'trailing_stop', 'portfolio_breaker', 'broker_risk'})
# 论文/事件型退出：进确认台
THESIS_REASONS = frozenset({'thesis_invalidated', 'event_risk', 'target_realized', 'thesis_exit'})
# 定时复核
SCHEDULED_REASONS = frozenset({'time_exit', 'scheduled'})


def classify_exit_reason(reason: str, config: Optional[Dict] = None) -> str:
    """把退出原因归类为 hard_risk / thesis / scheduled_review。

    time_exit 默认属 scheduled；若配置 hard_exit.time_exit_is_hard=true 则视为 hard_risk。
    """
    r = str(reason or '')
    # 去掉包装前缀（如 “卖出|fixed_stop”）后取最后一段匹配
    token = r.split('|')[-1].strip()
    hard_cfg = (config or {}).get('hard_exit') or {}
    if token in HARD_RISK_REASONS:
        return 'hard_risk'
    if token == 'time_exit' and bool(hard_cfg.get('time_exit_is_hard', False)):
        return 'hard_risk'
    if token in THESIS_REASONS:
        return 'thesis'
    if token in SCHEDULED_REASONS:
        return 'scheduled_review'
    # 未知原因默认 thesis（保守：不进自动执行）
    return 'thesis'


class HardExitRouter:
    """硬退出直达执行器（DRY-RUN 优先）。"""

    def __init__(self, registry=None, config: Optional[Dict] = None, execution=None):
        self.registry = registry or REGISTRY
        self.config = config or {}
        self.events = EventStore(self.registry)
        self.execution = execution  # ExecutionService（可空，DRY-RUN 直接冲正）

    def submit(self, *, trade_id: str, code: str, reason: str,
               market_price: Optional[float] = None,
               dry_run: bool = True) -> Dict:
        """提交一次硬退出。

        委托 ExecutionService.submit_system_exit 完成真实下单（含 DRY-RUN 冲正）。
        幂等 exit_id 保证同一触发只产生一个订单意图。
        """
        category = classify_exit_reason(reason, self.config)
        exit_id = stable_id('hard_exit', self.registry.namespace, trade_id, code, reason)
        if self.execution is None:
            # 无执行服务：纯路由记录，不落订单
            return {'status': 'no_execution_service', 'exit_id': exit_id,
                    'category': category}

        # 校验持仓存在、可卖数量，且无活跃卖单（交给 submit_system_exit，先在此预检）
        with self.registry.transaction() as book:
            pos = book['positions'].get(code)
            if not pos or float(pos.get('qty', 0)) <= 0:
                return {'status': 'no_position', 'exit_id': exit_id}
            qty = float(pos.get('qty', 0))
            if exit_id in book['orders']:
                return {'status': 'duplicate', 'exit_id': exit_id,
                        'order_status': book['orders'][exit_id]['status']}
            from .execution import ACTIVE
            if any(o['code'] == code and o['side'] == 'sell' and o['status'] in ACTIVE
                   for o in book['orders'].values()):
                return {'status': 'active_sell_exists', 'exit_id': exit_id}
        price = float(market_price) if market_price else float(pos.get('entry_price', 0))

        try:
            status = self.execution.submit_system_exit(
                exit_id=exit_id, code=code, qty=qty, price=price,
                reason=reason, category=category)
        except Exception as e:
            logger.warning(f"[HardExit] {code} {reason} 提交异常: {type(e).__name__}: {e}")
            return {'status': 'failed', 'exit_id': exit_id,
                    'category': category, 'error': str(e)}
        return {'status': status, 'exit_id': exit_id,
                'category': category, 'fill_price': price if status == 'filled' else None}
