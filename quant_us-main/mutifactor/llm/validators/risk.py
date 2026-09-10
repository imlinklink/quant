"""计划模板与风险边界校验（技术设计 validators/risk.py）。

程序计算模板与边界，模型不得扩大最大仓位、不得放宽硬止损、不得降低保护线、
不得编造任意委托价格。这里的校验在「提交订单前」再次执行（§7.3 / §8.4 / §9.5）。
"""
import math
from typing import Any, Dict, List, Optional


def _finite_positive(v) -> bool:
    try:
        return math.isfinite(float(v)) and float(v) > 0
    except (TypeError, ValueError):
        return False


def validate_entry_templates(templates: List[Dict], plan: Dict[str, Any]) -> List[str]:
    """校验入场模板：数量不超过标准量、止损不优于计划、价格为正、reject/wait 数量为 0。"""
    errors: List[str] = []
    if not templates:
        return ['缺少入场模板']
    by_kind = {t.get('kind'): t for t in templates}
    standard = by_kind.get('standard')
    plan_stop = (plan.get('risk') or {}).get('initial_stop')

    for t in templates:
        kind = t.get('kind')
        if not _finite_positive(t.get('entry_price_limit')):
            errors.append(f'{kind} 入场价无效')
        stop = t.get('initial_stop')
        if not _finite_positive(stop):
            errors.append(f'{kind} 初始止损无效')
        elif plan_stop is not None and float(stop) != float(plan_stop):
            # 模型不得放宽止损：模板止损必须等于计划止损
            errors.append(f'{kind} 止损偏离计划（不得放宽硬止损）')
        qty = t.get('quantity', 0)
        if not isinstance(qty, int) or qty < 0:
            errors.append(f'{kind} 数量非法')
        if kind in ('wait_for_confirmation', 'reject') and qty != 0:
            errors.append(f'{kind} 必须为 0 数量')
        if kind == 'half_size' and standard is not None:
            if qty > int(standard.get('quantity', 0)):
                errors.append('half_size 不得超过 standard 数量')
        if standard is not None and kind not in ('half_size', 'wait_for_confirmation', 'reject'):
            if qty > int(standard.get('quantity', 0)):
                errors.append(f'{kind} 数量超过标准量')
    return errors


def validate_position_templates(templates: List[Dict], trade: Dict[str, Any],
                                active_stop: Optional[float] = None) -> List[str]:
    """校验持仓动作模板：tighten 单调、reduce 只取预定义档位、exit=剩余数量。"""
    from mutifactor.llm.contracts.position_v2 import REDUCE_TIERS
    errors: List[str] = []
    remaining = float(trade.get('remaining_qty', 0))
    direction = trade.get('direction', 'long')

    for t in templates:
        action = t.get('action')
        qty = t.get('quantity', 0)
        if not math.isfinite(float(qty)) or float(qty) < 0:
            errors.append(f'{action} 数量非法')
        if action == 'exit' and abs(float(qty) - remaining) > 1e-8:
            errors.append('exit 数量必须等于剩余数量')
        if action == 'reduce':
            tier = (t.get('constraints') or {}).get('tier')
            if tier is None or float(tier) not in REDUCE_TIERS:
                errors.append(f'reduce 档位非法: {tier}（仅允许 {REDUCE_TIERS}）')
            elif abs(float(qty) - remaining * float(tier)) > 1e-8:
                errors.append('reduce 数量与档位不匹配')
        if action == 'tighten_protection':
            new_stop = t.get('new_protection_price')
            if new_stop is not None and active_stop is not None:
                # 多头：收紧（上移）保护线；不得低于当前保护线
                if direction == 'long' and float(new_stop) < float(active_stop):
                    errors.append('多头收紧保护线不得低于当前保护线')
                elif direction == 'short' and float(new_stop) > float(active_stop):
                    errors.append('空头收紧保护线不得高于当前保护线')
    return errors


def validate_price_drift(price: float, plan: Dict[str, Any],
                         max_drift_pct: Optional[float] = None) -> bool:
    """报价相对计划是否超过漂移上限（§8.4 强制 defer 条件）。"""
    constraints = plan.get('entry_constraints') or {}
    ref = constraints.get('price')
    if not _finite_positive(ref) or not _finite_positive(price):
        return True  # 缺失视为漂移（保守）
    limit = max_drift_pct if max_drift_pct is not None else constraints.get('max_price_drift_pct', .03)
    return abs(float(price) - float(ref)) / float(ref) > float(limit)
