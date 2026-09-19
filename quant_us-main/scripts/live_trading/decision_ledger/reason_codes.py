"""稳定原因码注册表（PR2，对应技术设计 §6）。

Schema 和前端都从注册表派生；模型/程序原因统一映射到稳定枚举，
自然语言解释只供用户阅读，枚举用于统计与权限评估。
"""
import logging

logger = logging.getLogger(__name__)

REASON_REGISTRY = {
    'INSUFFICIENT_EVIDENCE': {'roles': ('selection', 'entry', 'position')},
    'STALE_OR_LOW_QUALITY_DATA': {'roles': ('selection', 'entry', 'position')},
    'EVENT_RISK': {'roles': ('selection', 'entry', 'position')},
    'VALUATION_RISK': {'roles': ('selection', 'entry', 'position')},
    'TECHNICAL_NOT_CONFIRMED': {'roles': ('selection', 'entry')},
    'POOR_ASYMMETRY': {'roles': ('entry',)},
    'OPTIONS_CONFIRM': {'roles': ('selection', 'entry', 'position')},
    'OPTIONS_DIVERGE': {'roles': ('selection', 'entry', 'position')},
    'PORTFOLIO_CONCENTRATION': {'roles': ('selection', 'entry')},
    'REGIME_CONFLICT': {'roles': ('selection', 'entry', 'position')},
    'THESIS_WEAKENED': {'roles': ('position',)},
    'THESIS_INVALIDATED': {'roles': ('position',)},
    'TARGET_REALIZED': {'roles': ('position',)},
    'TIME_BUDGET_EXPIRED': {'roles': ('entry', 'position')},
}

# 旧小写原因码 → 新枚举（迁移用；旧事件保持原样不改写）
LEGACY_MIGRATION = {
    'event_risk': 'EVENT_RISK',
    'regime_conflict': 'REGIME_CONFLICT',
    'weak_confirmation': 'TECHNICAL_NOT_CONFIRMED',
    'stale_evidence': 'STALE_OR_LOW_QUALITY_DATA',
    'poor_asymmetry': 'POOR_ASYMMETRY',
    'data_gap': 'INSUFFICIENT_EVIDENCE',
}

# 这里曾有一个 `ROLES = ('selection','entry','position')`（2026-09-19 删除）。
# 它**零消费者**、且停在三个角色 —— 而角色权威是 `ROLE_CONTRACTS`（五个）。
# 一份没人用、又比真相少的清单，作用是让下一个读代码的人以为"只有三个角色"，
# 并在新增角色时被误当成需要同步的地方。角色适用性由 `REASON_REGISTRY[code]['roles']`
# 表达，`valid_for_role` 也只查它 —— 那才是这一个模块的权威。

REASON_REGISTRY_VERSION = 'reason-v2'


def is_valid_reason(code: str) -> bool:
    return code in REASON_REGISTRY


def valid_for_role(code: str, role: str) -> bool:
    """原因码是否适用于某角色（entry 只能出现在 entry 等）。"""
    meta = REASON_REGISTRY.get(code)
    if not meta:
        return False
    return role in meta['roles']


def reasons_for_role(role: str) -> tuple:
    """该角色的**全部合法原因码**，排序后返回。

    存在的理由：`entry_v2`/`position_v2` 的校验器对原因码 fail-closed
    （`valid_for_role`），而它们的提示词原先只发 packet + schema、**不给这份枚举** ——
    模型只能猜一个 14 项注册表里的值，猜错整条决策作废。真实模型实测被拒 5 个
    （`SUBJECT_ONLY_MARKET_EVIDENCE` 等，全是自造的合理词），而**夹具路径永远看不见**：
    `FakePositionModel` 的注释写明"给一个自造的会被校验器拒"，于是它手工只发合法码。
    证据 ID 的同类问题在 selection 上修过一次（提示词闭集），这里补上同一课。
    """
    return tuple(sorted(code for code, meta in REASON_REGISTRY.items()
                        if role in meta['roles']))


def with_reason_code_enum(schema: dict, role: str) -> dict:
    """返回 schema 的**副本**，把 `reason_codes.items` 收紧到该角色的合法枚举。

    为什么是"副本"而不是改模块常量：同一个 schema 对象被多个调用方共享（还进快照、进哈希），
    就地改会串味、不同角色的枚举会互相覆盖。

    为什么放在提示词侧而不是模块级常量：`mutifactor` 对 `scripts.*` 一直是**函数内导入**
    （分层），模块级导入会把这条依赖固化。
    """
    import copy
    out = copy.deepcopy(schema)
    props = out.get('properties') or {}
    if 'reason_codes' in props:
        props['reason_codes'] = {'type': 'array',
                                 'items': {'enum': list(reasons_for_role(role))}}
    return out


def normalize_reason(raw) -> str:
    """把任意输入规范成原因码：
    - 已是新枚举 → 原样；
    - 旧小写码 → 映射到新枚举；
    - 未知 → 返回 INSUFFICIENT_EVIDENCE（保守，不丢弃）。
    多 token（如“event_risk,data_gap”）取第一个可识别。
    """
    if not raw:
        return 'INSUFFICIENT_EVIDENCE'
    token = str(raw).strip().split('|')[-1].strip().split(',')[0].strip()
    up = token.upper()
    if up in REASON_REGISTRY:
        return up
    if token in LEGACY_MIGRATION:
        return LEGACY_MIGRATION[token]
    # 未知：保守映射，避免下游因未知码静默失效
    logger.debug('未知原因码 %r -> INSUFFICIENT_EVIDENCE', raw)
    return 'INSUFFICIENT_EVIDENCE'
