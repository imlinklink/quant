"""回撤阶梯（PR4，设计 §7）：账户级风险预算，纯离线、确定性。

drawdown = 1 − full_cost_equity / high_water（≥0 为亏损幅度）。上升按阈值从高到低命中；
恢复带迟滞（需连续 `recover_sessions` 个完整会话低于恢复阈值）。REVIEW_REQUIRED / LIMIT_BREACH
仅显式审查事件解除，不自动、不重置高点。阈值仅在完整估值（OK）下更新。
"""
from __future__ import annotations

RISK_STATES = ('NORMAL', 'REDUCED', 'PAUSED_ENTRY', 'REVIEW_REQUIRED', 'LIMIT_BREACH')

_DEFAULTS = {
    'limit_breach': 0.20,
    'review_required': 0.18,
    'paused_entry': 0.15,
    'reduced': 0.10,
    'normal_recover': 0.08,
    'reduced_recover': 0.12,
    'recover_sessions': 5,
}


_RANK = {'NORMAL': 0, 'REDUCED': 1, 'PAUSED_ENTRY': 2, 'REVIEW_REQUIRED': 3, 'LIMIT_BREACH': 4}


def evaluate_ladder(drawdown: float, current: str, streak: int, policy: dict | None) -> tuple[str, int]:
    """按回撤阶梯转移，返回 (新状态, 连续恢复会话数)。

    只允许向更严重状态**升级**；向更轻状态**降级**必须走迟滞（连续 N 会话低于恢复阈值），
    且 REVIEW_REQUIRED / LIMIT_BREACH 不自动降级（仅显式审查事件解除）。
    """
    t = dict(_DEFAULTS)
    if policy:
        t.update(policy.get('drawdown_ladder', {}) or {})
    # 升级目标：只看回撤阈值，不区分当前状态
    if drawdown >= t['limit_breach']:
        target = 'LIMIT_BREACH'
    elif drawdown >= t['review_required']:
        target = 'REVIEW_REQUIRED'
    elif drawdown >= t['paused_entry']:
        target = 'PAUSED_ENTRY'
    elif drawdown >= t['reduced']:
        target = 'REDUCED'
    else:
        target = 'NORMAL'
    if _RANK[target] > _RANK[current]:
        return target, 0  # 升级
    if _RANK[target] < _RANK[current]:
        # 降级：迟滞；REVIEW_REQUIRED / LIMIT_BREACH 不自动降级
        if current == 'REDUCED' and drawdown < t['normal_recover']:
            streak += 1
            return ('NORMAL', 0) if streak >= t['recover_sessions'] else ('REDUCED', streak)
        if current == 'PAUSED_ENTRY' and drawdown < t['reduced_recover']:
            streak += 1
            return ('REDUCED', 0) if streak >= t['recover_sessions'] else ('PAUSED_ENTRY', streak)
        return current, 0
    return current, 0  # 同级保持，重置 streak


def entry_allowed(state: str) -> bool:
    """是否允许新增仓位（NORMAL/REDUCED 允许，其余暂停）。"""
    return state in ('NORMAL', 'REDUCED')


def budget_bp(state: str, base_bp: int) -> int:
    """单笔风险预算（bp）：NORMAL 原值，REDUCED 减半，暂停态为 0。"""
    if state == 'NORMAL':
        return base_bp
    if state == 'REDUCED':
        return base_bp // 2
    return 0
