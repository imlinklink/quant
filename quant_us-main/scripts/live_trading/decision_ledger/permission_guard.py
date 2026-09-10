"""权限应用与自动降级（技术设计 §13 / §7.2）。

职责：
  - 决策开始时保存权限快照；执行时再读当前权限：
      当前更低 → 用更低；当前更高 → 仍用快照（防止旧决策借新权限执行）；
  - Prompt / Schema / 模型 / feature 版本变化 → 自动回到 shadow；
  - 运行异常 → 自动降级（写 permission_auto_downgraded 事件）。

硬风险退出不属于任何 LLM 权限（hard_exit_router 直连执行器）。
"""
import logging
from typing import Any, Dict, List, Optional

from mutifactor.llm.validators.action import apply_permission, most_restrictive
from scripts.live_trading.llm_permission import (
    level_for, parse_level,
)

logger = logging.getLogger(__name__)

# 数值化权限序（disabled 视同 shadow）
_LEVEL_RANK = {'shadow': 0, 'recommend': 1, 'constrained_action': 2, 'disabled': 0}

# 版本变化 → 回 shadow 的字段（§13.2）
VERSION_FIELDS = ('prompt', 'output_schema', 'model_id', 'feature')


class PermissionGuard:
    """权限应用：快照 vs 当前，取更低；版本变化回 shadow。"""

    def __init__(self, registry, store=None):
        from scripts.live_trading.decision_ledger.event_store import EventStore
        self.events = EventStore(registry)
        self.store = store
        self.scope = registry.namespace

    # ---------- 快照 ----------

    def snapshot_permissions(self, config: Dict[str, Any],
                             versions: Dict[str, str]) -> Dict[str, Any]:
        """把当前配置下的各项权限 + 版本固化成一个快照。"""
        from scripts.live_trading.llm_permission import PERMISSIONS
        return {
            'permissions': {p: level_for(p, config) for p in PERMISSIONS},
            'versions': dict(versions),
            'as_of': _now_iso(),
        }

    def save_permission_snapshot(self, key: str, snapshot: Dict[str, Any],
                                 version: int = 1) -> str:
        """保存权限快照，按 key 存储（与 load 对称）。返回 key。"""
        with self.events.transaction() as con:
            self.events.snapshot(con, 'permission_snapshot', key, version, snapshot)
        return key

    def load_permission_snapshot(self, key: str, version: int = 1):
        return self.events.get_snapshot('permission_snapshot', key, version)

    # ---------- 有效权限 ----------

    def effective_level(self, permission: str, snapshot: Optional[Dict[str, Any]],
                        current_config: Dict[str, Any],
                        current_versions: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """计算单个权限的有效级别（快照 vs 当前，取更低；版本变化回 shadow）。"""
        snap = snapshot or {}
        snap_level = parse_level((snap.get('permissions') or {}).get(permission, 'shadow'))
        current_level = parse_level(level_for(permission, current_config))
        snap_versions = snap.get('versions') or {}
        cur_versions = dict(current_versions or {})

        if _version_changed(snap_versions, cur_versions):
            return {'level': 'shadow',
                    'reason': 'Prompt/Schema/模型/feature 版本变化，自动回到 shadow'}

        if _LEVEL_RANK.get(current_level, 0) < _LEVEL_RANK.get(snap_level, 0):
            return {'level': current_level,
                    'reason': f'当前权限 {current_level} 低于快照 {snap_level}，采用更低'}
        if _LEVEL_RANK.get(current_level, 0) > _LEVEL_RANK.get(snap_level, 0):
            return {'level': snap_level,
                    'reason': f'当前权限 {current_level} 高于快照 {snap_level}，仍用快照（防旧决策借新权限）'}
        return {'level': snap_level, 'reason': '权限一致'}

    def effective_levels(self, permission_names: List[str],
                         snapshot: Optional[Dict[str, Any]],
                         current_config: Dict[str, Any],
                         current_versions: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """对一组适用权限分别计算有效级别。返回 {permission: level}。"""
        out: Dict[str, str] = {}
        for perm in permission_names:
            out[perm] = self.effective_level(perm, snapshot, current_config,
                                             current_versions)['level']
        return out

    def most_restrictive(self, permission_names: List[str],
                         snapshot: Optional[Dict[str, Any]],
                         current_config: Dict[str, Any],
                         current_versions: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """对一组适用权限取最严格者。返回 {permission_name, level, reason}。"""
        levels = self.effective_levels(permission_names, snapshot, current_config,
                                       current_versions)
        perm, level = most_restrictive(levels)
        return {'permission_name': perm, 'level': level,
                'reason': f'最严格权限 {perm}={level}', 'levels': levels}

    def apply(self, role: str, model_action: str, level: str,
              permission_name: Optional[str] = None,
              validated: Optional[Dict[str, Any]] = None,
              packet: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """把权限裁剪应用到模型动作（包装 action.apply_permission）。"""
        return apply_permission(role, model_action, level, permission_name,
                                validated=validated, packet=packet)

    # ---------- 事件 ----------

    def record_permission_change(self, permission: str, old_level: str,
                                 new_level: str, reason: str = '',
                                 versions: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """记录权限变化（§13.2 permission_changed）。"""
        from scripts.live_trading.decision_ledger.event_store import stable_id
        key = stable_id('permission_change', self.scope, permission, old_level, new_level)
        return self.events.record('permission_changed', key, {
            'permission': permission, 'old_level': old_level, 'new_level': new_level,
            'reason': reason, 'versions': versions or {},
        })

    def record_auto_downgrade(self, permission: str, from_level: str,
                              to_level: str, triggers: List[str]) -> Dict[str, Any]:
        """记录自动降级（§7.2 permission_auto_downgraded）。"""
        from scripts.live_trading.decision_ledger.event_store import stable_id
        key = stable_id('auto_downgrade', self.scope, permission, from_level, to_level)
        return self.events.record('permission_auto_downgraded', key, {
            'permission': permission, 'from_level': from_level, 'to_level': to_level,
            'triggers': list(triggers),
        })

    # ---------- 自动降级检测（§7.2） ----------

    def detect_downgrade(self, stats: Dict[str, Any],
                         thresholds: Optional[Dict[str, Any]] = None) -> List[str]:
        """返回触发的降级原因列表；空 = 不降级。"""
        t = thresholds or {}
        triggers: List[str] = []
        if stats.get('consecutive_data_quality_failures', 0) >= int(t.get('max_data_quality_failures', 5)):
            triggers.append('输入数据质量连续不合格')
        if stats.get('citation_error_rate', 0) > float(t.get('max_citation_error_rate', .1)):
            triggers.append('证据引用错误率显著上升')
        if bool(stats.get('all_same_action', False)):
            triggers.append('决策分布异常：长期单一动作')
        if stats.get('rolling_excess_return') is not None:
            rr = float(stats['rolling_excess_return'])
            floor = t.get('min_rolling_excess_return')
            if floor is not None and rr < float(floor):
                triggers.append('滚动窗口表现明显低于规则基线')
        if stats.get('confidence_flat', False):
            triggers.append('置信度失去区分度')
        return triggers


def _version_changed(snap_versions: Dict[str, str],
                     current_versions: Dict[str, str]) -> bool:
    for f in VERSION_FIELDS:
        if f in current_versions and f in snap_versions:
            if current_versions[f] != snap_versions[f]:
                return True
    return False


def _now_iso() -> str:
    from scripts.live_trading.decision_ledger.event_store import utc
    return utc()
