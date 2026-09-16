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


def evaluate_ladder(drawdown: float, current: str, streak: int, policy: dict | None) -> tuple[str, int]:
    """按回撤阶梯转移，返回 (新状态, 连续恢复会话数)。"""
    t = dict(_DEFAULTS)
    if policy:
        t.update(policy.get('drawdown_ladder', {}) or {})
    # 上升：从最高阈值往下命中
    if drawdown >= t['limit_breach']:
        return 'LIMIT_BREACH', 0
    if drawdown >= t['review_required']:
        return 'REVIEW_REQUIRED', 0
    if drawdown >= t['paused_entry']:
        return 'PAUSED_ENTRY', 0
    if drawdown >= t['reduced']:
        return 'REDUCED', 0
    # 恢复区（drawdown < reduced 阈值）
    if current == 'REDUCED':
        if drawdown < t['normal_recover']:
            streak += 1
            if streak >= t['recover_sessions']:
                return 'NORMAL', 0
            return 'REDUCED', streak
        return 'REDUCED', 0
    if current == 'PAUSED_ENTRY':
        # 回撤 <12% 连续 N 会话 → 降至 REDUCED；仍需人工风险审查（外部事件，此处只降级）
        if drawdown < t['reduced_recover']:
            streak += 1
            if streak >= t['recover_sessions']:
                return 'REDUCED', 0
            return 'PAUSED_ENTRY', streak
        return 'PAUSED_ENTRY', 0
    # REVIEW_REQUIRED / LIMIT_BREACH 不自动解除；NORMAL 保持
    return current, 0


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
