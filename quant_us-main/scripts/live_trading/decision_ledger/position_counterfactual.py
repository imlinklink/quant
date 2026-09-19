"""LLM 持仓反事实：冻结同一起点的规则路径 R 与模型路径 L。

只写影子事件和 outcome 投影，不修改持仓、不生成订单。L 路只能执行已经通过
Position v2 校验的固定动作模板；两条路径都优先执行冻结时的硬保护线。
"""
from typing import Any, Dict, Iterable, List, Optional

from .event_store import EventStore, stable_id, utc


HORIZONS = (1, 3, 5, 10, 20)


def _selected_template(packet: Dict[str, Any], review: Dict[str, Any]) -> Dict[str, Any]:
    template_id = review.get('action_template_id')
    templates = {t.get('template_id'): t for t in packet.get('allowed_actions', [])}
    if template_id and template_id in templates:
        return dict(templates[template_id])
    action = review.get('proposed_action') or review.get('recommendation') or 'hold'
    for template in packet.get('allowed_actions', []):
        if template.get('action') == action:
            return dict(template)
    return {'template_id': None, 'action': action, 'quantity': 0.0}


def _citation_summary(packet: Dict[str, Any], review: Dict[str, Any]) -> Dict[str, Any]:
    cited = {eid for field in ('facts', 'inferences', 'counterevidence')
             for claim in review.get(field, []) for eid in claim.get('evidence_ids', [])}
    index = {e.get('evidence_id'): e for e in packet.get('new_evidence', [])}
    subjects: Dict[str, int] = {}
    for eid in cited:
        subject = str((index.get(eid) or {}).get('subject_code') or 'UNKNOWN')
        subjects[subject] = subjects.get(subject, 0) + 1
    return {'evidence_ids': sorted(cited), 'subject_counts': subjects}


def freeze_payload(packet: Dict[str, Any], review: Dict[str, Any], *,
                   review_id: str, trigger: str, fee_rate: float = 0.0) -> Dict[str, Any]:
    """构造不可变实验输入。调用方应只传入已验证完成的 Position v2 决策。"""
    trade = packet.get('trade') or {}
    protection = packet.get('protection') or {}
    context = packet.get('context') or {}
    template = _selected_template(packet, review)
    quantity = float(trade.get('remaining_qty') or 0.0)
    action_quantity = min(quantity, max(0.0, float(template.get('quantity') or 0.0)))
    as_of = utc(context.get('as_of'))
    return {
        'experiment_version': 'position-counterfactual-v1',
        'execution_model': 'next_bar_open_hard_stop_first_v1',
        'capital_policy': 'cash_no_reinvestment',
        'review_id': review_id,
        'decision_id': review.get('decision_id'),
        'trade_id': trade.get('trade_id'),
        'code': trade.get('code'),
        'as_of': as_of,
        'trigger': trigger,
        'starting_quantity': quantity,
        'reference_price': float(trade.get('mark_price') or 0.0),
        'entry_price': float(trade.get('entry_price') or 0.0),
        'active_stop': float(protection.get('active_stop') or 0.0),
        'hard_exit_authoritative': bool(protection.get('hard_exit_authoritative', True)),
        'fee_rate': max(0.0, float(fee_rate)),
        'r_path': {'action': 'hold', 'quantity': 0.0},
        'l_path': {
            'action': template.get('action') or 'hold',
            'action_template_id': template.get('template_id'),
            'quantity': action_quantity,
            'new_protection_price': template.get('new_protection_price'),
        },
        'citations': _citation_summary(packet, review),
        'frozen_at': as_of,
    }


def _sell(state: Dict[str, float], quantity: float, price: float, fee_rate: float) -> None:
    quantity = min(state['quantity'], max(0.0, quantity))
    state['quantity'] -= quantity
    state['cash'] += quantity * price * (1.0 - fee_rate)


def _equity(state: Dict[str, float], price: float) -> float:
    return state['cash'] + state['quantity'] * price


def simulate(frozen: Dict[str, Any], bars: Iterable[Dict[str, Any]],
             horizons=HORIZONS) -> List[Dict[str, Any]]:
    """用决策后的 OHLC bars 结算多期限 L/R 路径。

    首根 bar 先检查硬止损，再在开盘执行 LLM 模板，避免模型动作覆盖风控。
    跳空低于保护线按开盘价成交，否则按保护线成交。
    """
    rows = list(bars)
    qty = float(frozen.get('starting_quantity') or 0.0)
    reference = float(frozen.get('reference_price') or 0.0)
    if qty <= 0 or reference <= 0:
        raise ValueError('反事实起始数量与参考价格必须为正')
    fee_rate = max(0.0, float(frozen.get('fee_rate') or 0.0))
    stop = float(frozen.get('active_stop') or 0.0)
    hard_stop = bool(frozen.get('hard_exit_authoritative', True)) and stop > 0
    r = {'quantity': qty, 'cash': 0.0}
    l = {'quantity': qty, 'cash': 0.0}
    initial = qty * reference
    r_peak = l_peak = initial
    r_max_dd = l_max_dd = 0.0
    action_applied = False
    freed_cash = 0.0
    results = []
    wanted = set(int(h) for h in horizons)

    for index, bar in enumerate(rows, start=1):
        open_price = float(bar['open'])
        low = float(bar.get('low', open_price))
        close = float(bar.get('close', open_price))
        if hard_stop and low <= stop:
            fill = open_price if open_price <= stop else stop
            _sell(r, r['quantity'], fill, fee_rate)
            _sell(l, l['quantity'], fill, fee_rate)
        elif not action_applied:
            action = (frozen.get('l_path') or {}).get('action')
            if action in ('reduce', 'exit'):
                before = l['cash']
                sell_qty = l['quantity'] if action == 'exit' else float(
                    (frozen.get('l_path') or {}).get('quantity') or 0.0)
                _sell(l, sell_qty, open_price, fee_rate)
                freed_cash = l['cash'] - before
            action_applied = True

        r_value = _equity(r, close)
        l_value = _equity(l, close)
        r_peak, l_peak = max(r_peak, r_value), max(l_peak, l_value)
        r_max_dd = min(r_max_dd, (r_value - r_peak) / r_peak)
        l_max_dd = min(l_max_dd, (l_value - l_peak) / l_peak)
        if index not in wanted:
            continue
        r_return = r_value / initial - 1.0
        l_return = l_value / initial - 1.0
        delta = l_return - r_return
        results.append({
            'horizon': f'{index}d',
            'r_return_pct': r_return,
            'l_return_pct': l_return,
            'delta_return_pct': delta,
            'r_max_drawdown_pct': r_max_dd,
            'l_max_drawdown_pct': l_max_dd,
            'saved_loss_pct': max(0.0, delta) if r_return < 0 else 0.0,
            'missed_upside_pct': max(0.0, -delta) if r_return > 0 else 0.0,
            'freed_cash': freed_cash,
            'r_remaining_quantity': r['quantity'],
            'l_remaining_quantity': l['quantity'],
            'data_quality': 'good',
        })
    return results


class PositionCounterfactualLedger:
    """冻结实验并写入 append-only 决策事件。"""

    def __init__(self, registry):
        self.events = EventStore(registry)

    def freeze(self, packet: Dict[str, Any], review: Dict[str, Any], *,
               review_id: str, trigger: str, fee_rate: float = 0.0) -> Dict[str, Any]:
        payload = freeze_payload(packet, review, review_id=review_id, trigger=trigger,
                                 fee_rate=fee_rate)
        counterfactual_id = stable_id(
            'position_cf', self.events.scope, payload.get('trade_id'), review_id,
            payload.get('decision_id') or '')
        payload['counterfactual_id'] = counterfactual_id
        self.events.record('position_counterfactual_frozen', counterfactual_id, payload,
                           trade_id=payload.get('trade_id'), review_id=review_id,
                           decision_id=payload.get('decision_id'))
        return payload
