"""LLM 权限状态机（阶段 I3 / J3）。

每项 LLM 权限单独维护状态：shadow → recommend → constrained_action → disabled。
默认全 shadow：模型建议被记录但不改变任何执行行为。升级需满足样本门槛。

权限清单（对应 roadmap）：
  - selection_rank           选股排序（首个迭代，shadow）
  - entry_review             入场评审 support/defer/oppose
  - exit_review              持仓/卖出评审
  - position_scale           仓位档位 0 / 0.5x / 1.0x（只允许降档）
  - plan_template            计划模板选择
  - auto_exit_thesis         仅 thesis_invalidated 的有限自动降险
"""
from enum import Enum

PERMISSIONS = ('selection_rank', 'entry_review', 'exit_review',
               'position_scale', 'plan_template', 'auto_exit_thesis')

# 升级顺序：shadow 最低，disabled 表示「显式关闭」
LEVEL_ORDER = ('shadow', 'recommend', 'constrained_action', 'disabled')


class PermissionLevel(str, Enum):
    SHADOW = 'shadow'
    RECOMMEND = 'recommend'
    CONSTRAINED_ACTION = 'constrained_action'
    DISABLED = 'disabled'


def parse_level(value, default='shadow'):
    value = str(value or '').lower()
    return value if value in LEVEL_ORDER else default


def level_for(permission, config):
    """读取某项权限的当前级别。config 形如 {'llm_permissions': {'position_scale': 'shadow'}}。"""
    if permission not in PERMISSIONS:
        raise ValueError(f'未知权限: {permission}')
    cfg = (config or {}).get('llm_permissions') or {}
    default = cfg.get('_default', 'shadow')
    return parse_level(cfg.get(permission, default), default)


def permits(permission, level):
    """某项权限在 level 下是否允许其动作被执行（constrain 才真正改量）。"""
    return level in (PermissionLevel.CONSTRAINED_ACTION, PermissionLevel.RECOMMEND)


def allowed_scale(permission, level, proposed_scale):
    """仓位档位：仅 constrained_action 且建议为降档(<=1.0)时允许应用到数量；否则影子。

    Returns:
        (final_scale, applied)  applied=True 表示本次真正应用降档。
    """
    if proposed_scale is None or not permits(permission, level):
        return 1.0, False
    if proposed_scale >= 1.0:
        return 1.0, False  # 1.0x = 现有上限，无变化；加档>1.0 永远不允许
    if proposed_scale < 0:
        return 1.0, False
    # 只有 constrained_action 真正应用降档；recommend 只记录不应用
    if level != PermissionLevel.CONSTRAINED_ACTION:
        return 1.0, False
    return float(proposed_scale), True


def eligibility_met(stats, thresholds):
    """J3 门槛：stats={'independent_samples','market_phases','improved','data_leak_checked'}，
    thresholds 对应最小值。全部满足才算可升级。"""
    thresholds = thresholds or {}
    checks = {
        'independent_samples': stats.get('independent_samples', 0) >= thresholds.get('min_samples', 0),
        'market_phases': stats.get('market_phases', 0) >= thresholds.get('min_market_phases', 1),
        'data_leak_checked': bool(stats.get('data_leak_checked', False)),
    }
    if thresholds.get('require_improvement', False):
        checks['improved'] = bool(stats.get('improved', False))
    return all(checks.values()), checks


def promote_candidate(permission, config, stats, thresholds):
    """判断某项权限是否满足从 shadow 升到 recommend 的门槛（J3）。只读判定。"""
    current = level_for(permission, config)
    if current not in (PermissionLevel.SHADOW, PermissionLevel.RECOMMEND):
        return False
    ok, _ = eligibility_met(stats, thresholds)
    return ok
