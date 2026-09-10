"""动作与权限校验（技术设计 §13）：model action 与 effective action 分离。

模型动作（model action）是模型想要的；有效动作（effective action）是权限裁剪后
真正会进入确认台/执行器的动作。前端不能把 model action 展示成已生效动作。

权限命名（§13.1）：selection_rank / entry_review / plan_template / position_scale /
exit_review / protection_tighten / thesis_reduce / auto_exit_thesis。硬风险退出不属于
任何 LLM 权限。

关键（修复 P0「子权限被伞级绕过」）：
  - 每个动作的「适用权限集」= 伞级权限 + 具体子权限，取最严格（min）；
  - constrained_action 下逐子权限 gate（auto_exit_thesis 仅限 INVALIDATED 的 exit）。
"""
from typing import Any, Dict, List, Optional, Tuple

# 各角色的基线有效动作（shadow 时模型动作不生效）
EFFECTIVE_BASELINE = {
    'selection': 'rule_ranking',
    'entry': 'rule_baseline',
    'position': 'hold',
}

# 伞级权限（决定「LLM 动作是否影响流程」）
UMBRELLA = {
    'selection': 'selection_rank',
    'entry': 'entry_review',
    'position': 'exit_review',
}

# 具体子权限（决定「特定动作能否自动执行」）
SPECIFIC_ACTION_PERMISSION = {
    ('position', 'tighten_protection'): 'protection_tighten',
    ('position', 'reduce'): 'thesis_reduce',
    ('position', 'exit'): 'auto_exit_thesis',
}

# 每项权限允许放行的模型动作（constrained_action 下逐项 gate）
PERMISSION_ACTION_SCOPE = {
    'selection_rank': ('llm_ranking',),
    'entry_review': ('execute_now', 'defer', 'reject'),
    'plan_template': ('execute_now',),
    'position_scale': ('execute_now',),
    'exit_review': ('hold', 'tighten_protection', 'reduce', 'exit', 'post_exit_review'),
    'protection_tighten': ('tighten_protection',),
    'thesis_reduce': ('reduce',),
    'auto_exit_thesis': ('exit',),
}

LEVELS = ('shadow', 'recommend', 'constrained_action', 'disabled')

# 数值化权限序（disabled 视同 shadow，用于取最严格）
LEVEL_RANK = {'shadow': 0, 'recommend': 1, 'constrained_action': 2, 'disabled': 0}


def applicable_permissions(role: str, action: str) -> List[str]:
    """返回该动作的适用权限集（伞级 + 具体子权限），取最严格。

    entry 的 execute_now 额外受 plan_template（模板选择）与 position_scale（缩量）约束；
    defer/reject 只受 entry_review 约束。
    """
    umbrella = UMBRELLA.get(role)
    if umbrella is None:
        return []
    perms = [umbrella]
    if role == 'entry':
        if action == 'execute_now':
            perms.extend(['plan_template', 'position_scale'])
        return perms
    if role == 'position':
        specific = SPECIFIC_ACTION_PERMISSION.get((role, action))
        if specific and specific not in perms:
            perms.append(specific)
    return perms


def specific_permission(role: str, action: str) -> str:
    """动作对应的具体子权限（无则回伞级）。用于 scope gate。"""
    return SPECIFIC_ACTION_PERMISSION.get((role, action)) or UMBRELLA.get(role, '')


def most_restrictive(levels: Dict[str, str]) -> Tuple[str, str]:
    """在多个权限级别里取最严格者（数值最小）。返回 (最严格权限名, level)。"""
    best_perm, best_level = '', 'constrained_action'  # 从最高等级起，向下降
    for perm, lvl in levels.items():
        if LEVEL_RANK.get(lvl, 0) < LEVEL_RANK.get(best_level, 2):
            best_perm, best_level = perm, lvl
    return best_perm, best_level


def _shadow_difference(model_action: str, baseline: str) -> Optional[str]:
    return None if model_action == baseline else f'llm_would_{model_action}'


def apply_permission(role: str, model_action: str, permission_level: str,
                     permission_name: Optional[str] = None,
                     validated: Optional[Dict[str, Any]] = None,
                     packet: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按权限裁剪模型动作，返回 {model_action, permission_level, effective_action,
    shadow_difference, permission_name, requires_confirmation, reason}。

    规则：
      - shadow/disabled → 有效动作 = 角色基线，shadow_difference 记录模型意图；
      - recommend → 模型动作进入确认台（requires_confirmation=True）；
      - constrained_action → 仅当动作落在该子权限 scope 内才自动执行，否则降为基线；
      - auto_exit_thesis 仅当论文状态为 INVALIDATED 才放行 exit。
    """
    perm = permission_name or specific_permission(role, model_action)
    level = str(permission_level or 'shadow').lower()
    if level not in LEVELS:
        level = 'shadow'
    baseline = EFFECTIVE_BASELINE.get(role, 'hold')

    result = {
        'model_action': model_action,
        'permission_level': level,
        'permission_name': perm,
        'effective_action': baseline,
        'shadow_difference': _shadow_difference(model_action, baseline),
        'requires_confirmation': False,
        'reason': '',
    }

    if level in ('shadow', 'disabled'):
        result['reason'] = f'{perm} 处于 {level}，模型动作仅记录'
        return result

    if level == 'recommend':
        result['effective_action'] = model_action
        result['shadow_difference'] = None
        result['requires_confirmation'] = True
        result['reason'] = f'{perm} 为 recommend，模型动作需用户确认'
        return result

    # constrained_action：逐子权限 gate
    scope = PERMISSION_ACTION_SCOPE.get(perm, ())
    if model_action not in scope:
        result['effective_action'] = baseline
        result['shadow_difference'] = _shadow_difference(model_action, baseline)
        result['reason'] = f'{perm} 不覆盖动作 {model_action}，降为基线'
        return result

    # auto_exit_thesis 特殊：仅在 INVALIDATED 时放行 exit
    if perm == 'auto_exit_thesis':
        thesis_state = ((validated or {}).get('thesis_state') or
                        (packet or {}).get('thesis', {}).get('state'))
        if thesis_state != 'INVALIDATED':
            result['effective_action'] = baseline
            result['shadow_difference'] = _shadow_difference(model_action, baseline)
            result['reason'] = 'auto_exit_thesis 仅限论文 INVALIDATED'
            return result

    result['effective_action'] = model_action
    result['shadow_difference'] = None
    result['reason'] = f'{perm} 为 constrained_action，动作生效'
    return result
