"""DecisionEngine 主链路路由。

默认保持 legacy；只有显式把具体角色设为 shadow 才切换到 v2。
本模块不实现双模型调用。模型调用开始后失败时也不回退旧模型。
"""
from typing import Any, Dict

from scripts.live_trading.decision_engine import DecisionEngine


ROLES = ('selection', 'entry', 'position')
MODES = ('legacy', 'shadow')


def engine_v2_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """兼容完整 config、llm_decision 子树或 engine_v2 子树。"""
    config = config or {}
    decision = config.get('llm_decision') if isinstance(config.get('llm_decision'), dict) else config
    engine = decision.get('engine_v2') if isinstance(decision.get('engine_v2'), dict) else decision
    return engine or {}


class DecisionRuntime:
    def __init__(self, registry, advisor=None, config=None):
        self.registry = registry
        self.advisor = advisor
        self.config = config or {}
        self.engine_config = engine_v2_config(self.config)
        self._engine = None

    def mode(self, role: str) -> str:
        if role not in ROLES:
            raise ValueError(f'非法决策角色: {role}')
        value = str(self.engine_config.get(role, 'legacy')).lower()
        if value not in MODES:
            raise ValueError(f'非法 DecisionEngine 模式: {role}={value}')
        return value

    def is_shadow(self, role: str) -> bool:
        return self.mode(role) == 'shadow'

    def engine(self) -> DecisionEngine:
        if self._engine is None:
            self._engine = DecisionEngine(
                self.registry, advisor=self.advisor, config=self.config)
        return self._engine

    def may_fallback(self, phase: str) -> bool:
        """只允许在创建快照/attempt 之前回退。"""
        return (bool(self.engine_config.get('fallback_before_call', True))
                and phase in ('route', 'build_packet'))
