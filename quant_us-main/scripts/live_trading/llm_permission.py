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
               'position_scale', 'plan_template', 'auto_exit_thesis',
               'protection_tighten', 'thesis_reduce',
               # P2 新增两角色（设计 §6.3 / §6.5）。初始等级 observe/shadow：
               # Portfolio 只在程序模板里选，Review 只能提候选，两者都不得自动生效。
               'portfolio_allocation', 'protocol_review')

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
    """某项权限在 level 下是否允许其动作被执行。

    仅 constrained_action 允许真正执行；recommend/shadow 只记录建议不执行。
    语义默认拒绝：recommend 不代表可动作。
    """
    return level == PermissionLevel.CONSTRAINED_ACTION


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
    return float(proposed_scale), True


def eligibility_met(stats, thresholds):
    """J3 门槛（默认拒绝）：stats={'independent_samples','market_phases','improved','data_leak_checked'}。

    必须显式提供 thresholds（min_samples / min_market_phases / require_improvement），
    缺省或未通过数据泄漏检查 → 不满足（fail-closed），避免默认 0 门槛被误升级。
    """
    thresholds = thresholds or {}
    # 默认拒绝：缺关键门槛或未配置则视为不满足
    if 'min_samples' not in thresholds or 'min_market_phases' not in thresholds:
        return False, {'_config': 'missing_thresholds', 'eligible': False}
    checks = {
        'independent_samples': stats.get('independent_samples', 0) >= thresholds.get('min_samples', 0),
        'market_phases': stats.get('market_phases', 0) >= thresholds.get('min_market_phases', 0),
        'data_leak_checked': bool(stats.get('data_leak_checked', False)),
    }
    if thresholds.get('require_improvement', False):
        checks['improved'] = bool(stats.get('improved', False))
    return all(checks.values()), checks


def promote_candidate(permission, config, stats, thresholds):
    """判断某项权限是否满足从 shadow 升到 recommend 的门槛（J3）。只读判定。

    门槛必须显式配置；未配置或未满足 → 不升级（默认拒绝）。
    """
    current = level_for(permission, config)
    if current not in (PermissionLevel.SHADOW, PermissionLevel.RECOMMEND):
        return False
    ok, _ = eligibility_met(stats, thresholds)
    return ok


# ==================== §8 权限模型 / §12 晋级门槛 ====================
#
# 代码的四级与设计 §8 的四级对应关系（明白写下，避免两套词汇各说各话）：
#
#   代码 shadow            ⟷ 设计 observe         只记录，不改变任何路径
#   代码 recommend         ⟷ 设计 shadow_affect   影响流程但需人工确认
#   代码 constrained_action ⟷ 设计 paper_authority 在授权范围内自动执行
#   代码 disabled           = 显式关闭（不是"最低级"，是"别用它"）
#
# 设计没有与 recommend 等价的级别（它把"人工确认"放在流程外）；`limited_live` 无对应物，
# 本设计明确不实施。

# 晋级阶梯。`disabled` 不在阶梯上——它表示显式关闭，不该被"晋级"越过。
# 用**纯字符串**而不是 `PermissionLevel`：`class X(str, Enum)` 在 Python 3.11 之前
# `str(x)` 得到 `'PermissionLevel.SHADOW'` 而不是 `'shadow'`，混用迟早出错。
PROMOTION_TIERS = (('shadow', 'recommend'), ('recommend', 'constrained_action'))

# 角色级取"最严格"的等级序。注意与 `permission_guard._LEVEL_RANK` 的差异：那里的
# `disabled` 记 0（等价于 shadow，因为两者都不执行动作）；但**晋级判定**里 `disabled`
# 是显式关闭，必须压过一切、不得被晋级越过，故记最高。
_LEVEL_RANK = {'shadow': 0, 'recommend': 1, 'constrained_action': 2, 'disabled': 3}

# 角色 → 该角色名下的权限。与 `mutifactor.llm.validators.action` 的
# `UMBRELLA` / `SPECIFIC_ACTION_PERMISSION` 必须一致，由测试钉死（两处定义漂移会让
# "某角色的权限等级"算成一个与执行时不同的集合）。
ROLE_PERMISSIONS = {
    'selection': ('selection_rank',),
    'entry': ('entry_review', 'plan_template', 'position_scale'),
    'position': ('exit_review', 'protection_tighten', 'thesis_reduce', 'auto_exit_thesis'),
    'portfolio': ('portfolio_allocation',),
    'review': ('protocol_review',),
}

# 判定项 → (指标名, 门槛名, 方向)。方向决定怎么比：
#   min   指标 ≥ 门槛      max   指标 ≤ 门槛      truthy 指标为真
# 指标**取不到**（stats 里缺这个键）时一律判不合格并给出 `unavailable` 原因 ——
# 绝不允许"算不出来"被读成"通过了"。
_TIER_CHECKS = {
    ('shadow', 'recommend'): (
        ('output_validity', 'min_output_validity', 'min'),
        ('hard_risk_overreach', 'max_hard_risk_overreach', 'max'),
        ('unreplayable', 'max_unreplayable', 'max'),
        ('model_effective_distinguishable', 'require_model_effective_distinguishable', 'truthy'),
    ),
    ('recommend', 'constrained_action'): (
        ('independent_mature_samples', 'min_independent_samples', 'min'),
        ('excess_return_after_cost', 'min_excess_return_after_cost', 'min'),
        ('mdd_delta', 'max_mdd_delta', 'max'),
        ('top_contributor_share', 'max_top_contributor_share', 'max'),
        ('out_of_sample_window_locked', 'require_out_of_sample_window', 'truthy'),
        ('human_approval', 'require_human_approval', 'truthy'),
    ),
}

PROMOTION_THRESHOLD_KEYS = tuple(sorted(
    {key for checks in _TIER_CHECKS.values() for _m, key, _d in checks}
    | {'min_samples', 'min_market_phases', 'require_improvement'}))

# 设计 §12 的门槛默认值。**写死在代码里、由配置覆盖**：缺配置时用设计值而不是"无门槛"。
DEFAULT_PROMOTION_THRESHOLDS = {
    'min_output_validity': 0.95,
    'max_hard_risk_overreach': 0,
    'max_unreplayable': 0,
    'require_model_effective_distinguishable': True,
    'min_independent_samples': 30,
    'min_excess_return_after_cost': 0.0,
    'max_mdd_delta': 0.05,
    'max_top_contributor_share': 0.5,
    'require_out_of_sample_window': True,
    'require_human_approval': True,
}

# 人工确认项：**指标名**（stats 侧），无法从账本推出，必须由人显式声明，否则恒为未达标。
# 对应的**门槛名**是 `require_*`（`require_human_approval` / `require_out_of_sample_window`），
# 两者是不同的命名空间，别混。
OPERATOR_ATTESTED = ('out_of_sample_window_locked', 'human_approval')


def validate_permissions(config) -> list:
    """校验 `llm_permissions` 配置段；返回错误列表（空 = 通过）。

    严格校验的理由与 manifest 的未知键检查相同：拼错一个权限名或等级名会被
    `level_for` 的 `dict.get` 静默忽略、回落到 `_default`，让一个本该生效的安全门
    无声失效 —— 而且失效方向是"更松"还是"更紧"不可控。
    """
    errors = []
    cfg = (config or {}).get('llm_permissions')
    if cfg is None:
        return ['llm_permissions 缺失：默认值应当显式写出，不能靠代码里的隐式回落']
    if not isinstance(cfg, dict):
        return [f'llm_permissions 必须是映射，实际 {type(cfg).__name__}']
    allowed = set(PERMISSIONS) | {'_default', 'promotion'}
    unknown = sorted(set(cfg) - allowed)
    if unknown:
        errors.append(f'llm_permissions 含未知键 {unknown}（允许：{sorted(allowed)}）')
    if '_default' not in cfg:
        errors.append('llm_permissions._default 缺失：未列出的权限会静默落到某个默认值')
    for key, value in cfg.items():
        if key == 'promotion':
            errors.extend(_validate_promotion(value))
        elif key in allowed and str(value).lower() not in LEVEL_ORDER:
            errors.append(f'llm_permissions.{key} 等级非法：{value!r}'
                          f'（允许：{list(LEVEL_ORDER)}）')
    return errors


def _validate_promotion(section) -> list:
    errors = []
    if not isinstance(section, dict):
        return [f'llm_permissions.promotion 必须是映射，实际 {type(section).__name__}']
    allowed = set(ROLE_PERMISSIONS) | set(PERMISSIONS) | {'_default'}
    unknown = sorted(set(section) - allowed)
    if unknown:
        errors.append(f'llm_permissions.promotion 含未知键 {unknown}'
                      f'（允许角色 {sorted(ROLE_PERMISSIONS)} 或权限名，以及 _default）')
    for key, thresholds in section.items():
        if not isinstance(thresholds, dict):
            errors.append(f'llm_permissions.promotion.{key} 必须是映射')
            continue
        bad = sorted(set(thresholds) - set(PROMOTION_THRESHOLD_KEYS))
        if bad:
            errors.append(f'llm_permissions.promotion.{key} 含未知门槛 {bad}'
                          f'（允许：{list(PROMOTION_THRESHOLD_KEYS)}）')
    return errors


def promotion_thresholds(role, config) -> dict:
    """该角色的门槛 = 设计默认值 ← 权限级覆盖 ← 角色级覆盖。"""
    section = ((config or {}).get('llm_permissions') or {}).get('promotion') or {}
    merged = dict(DEFAULT_PROMOTION_THRESHOLDS)
    merged.update(section.get('_default') or {})
    for permission in ROLE_PERMISSIONS.get(role, ()):
        merged.update(section.get(permission) or {})
    merged.update(section.get(role) or {})
    return merged


def _check(name, actual, required, direction, unavailable_reason=''):
    if actual is None:
        return {'name': name, 'actual': None, 'required': required, 'passed': False,
                'reason': f'unavailable：{unavailable_reason or "指标不可计算"}'
                          f'（不得读作通过）'}
    if direction == 'min':
        passed = actual >= required
        reason = '' if passed else f'{actual} < 门槛 {required}'
    elif direction == 'max':
        passed = actual <= required
        reason = '' if passed else f'{actual} > 门槛 {required}'
    else:
        passed = bool(actual)
        reason = '' if passed else '未满足（需显式确认）'
    return {'name': name, 'actual': actual, 'required': required, 'passed': passed,
            'reason': reason}


def promotion_verdict(role, config, stats) -> dict:
    """§12 晋级资格判定。**只读**：不切换任何级别、不改变执行路径。

    满足门槛只表示**具备晋级资格**，晋级仍须人工批准新 permission version（设计 §12）。
    返回逐项判定（含实际值、门槛、未达标原因），因此结论可审计、可复核。

    `stats` 缺失的指标一律判不合格并标 `unavailable` —— 把"算不出来"读成"通过了"
    是这类门槛最危险的失效方式。
    """
    permissions = ROLE_PERMISSIONS.get(role)
    if permissions is None:
        return {'role': role, 'permissions': [], 'current_level': None, 'target_level': None,
                'eligible': False, 'checks': [], 'unmet': [],
                'note': f'该角色尚未接入权限模型（已知：{sorted(ROLE_PERMISSIONS)}）'}
    levels = [level_for(p, config) for p in permissions]
    # 角色级取**最严格**的那一项：角色能否晋级不应被它名下最松的权限拉高
    current = max(levels, key=lambda lv: _LEVEL_RANK[lv])
    tier = next((t for t in PROMOTION_TIERS if t[0] == current), None)
    if tier is None:
        return {'role': role, 'permissions': list(permissions), 'current_level': str(current),
                'target_level': None, 'eligible': False, 'checks': [], 'unmet': [],
                'note': ('已在最高级别，无更高台阶' if current == PermissionLevel.CONSTRAINED_ACTION
                         else '权限被显式关闭，不得晋级')}
    thresholds = promotion_thresholds(role, config)
    unavailable = (stats or {}).get('_unavailable') or {}
    checks = [_check(name, (stats or {}).get(name), thresholds.get(key), direction,
                     unavailable.get(name, ''))
              for name, key, direction in _TIER_CHECKS[tier]]
    unmet = [c['name'] for c in checks if not c['passed']]
    return {'role': role, 'permissions': list(permissions), 'current_level': str(current),
            'target_level': str(tier[1]), 'eligible': not unmet, 'checks': checks,
            'unmet': unmet,
            'note': ('具备晋级资格 —— 但**不自动切换**：晋级须人工批准新 permission version'
                     if not unmet else '未达标，禁止晋级')}


def promotion_report(config, stats_by_role) -> dict:
    """全部角色的逐项判定（可审计输出）。同样只读。"""
    return {'roles': {role: promotion_verdict(role, config, stats_by_role.get(role) or {})
                      for role in sorted(ROLE_PERMISSIONS)},
            'levels': {p: level_for(p, config) for p in PERMISSIONS},
            'note': '满足门槛只表示具备晋级资格；本报告不切换任何级别、不改变执行路径'}
