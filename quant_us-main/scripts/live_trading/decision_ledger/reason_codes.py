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

# 合法角色
ROLES = ('selection', 'entry', 'position')

REASON_REGISTRY_VERSION = 'reason-v2'


def is_valid_reason(code: str) -> bool:
    return code in REASON_REGISTRY


def valid_for_role(code: str, role: str) -> bool:
    """原因码是否适用于某角色（entry 只能出现在 entry 等）。"""
    meta = REASON_REGISTRY.get(code)
    if not meta:
        return False
    return role in meta['roles']


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
