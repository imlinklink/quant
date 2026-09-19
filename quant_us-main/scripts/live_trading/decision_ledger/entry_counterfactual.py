"""冻结 Entry v2 的规则路径 R 与模型路径 L；只记录，不下单。"""
from typing import Any, Dict, Iterable, List

from .event_store import EventStore, stable_id


def freeze_payload(packet: Dict[str, Any], result, *, proposal_id: str,
                   review_id: str) -> Dict[str, Any]:
    templates = {t.get('template_id'): t for t in packet.get('templates', [])}
    output = result.validated_output or {}
    plan = packet.get('plan') or {}
    standard = next((dict(t) for t in packet.get('templates', [])
                     if t.get('kind') == 'standard'), {})
    selected = dict(templates.get(output.get('template_id')) or {})
    evidence_ids = sorted({eid for field in ('facts', 'inferences', 'counterevidence')
                           for claim in output.get(field, [])
                           for eid in claim.get('evidence_ids', [])})
    return {
        'experiment_version': 'entry-counterfactual-v1',
        'decision_id': result.decision_id,
        'proposal_id': proposal_id,
        'review_id': review_id,
        'signal_id': (packet.get('signal') or {}).get('signal_id'),
        'code': plan.get('stock_code'),
        'as_of': (packet.get('context') or {}).get('as_of'),
        'rule_path': {'action': 'execute_now', 'template': standard},
        'llm_path': {'action': result.model_action, 'template': selected},
        'effective_action': result.effective_action,
        'permission_level': result.permission_level,
        'evidence_ids': evidence_ids,
    }


class EntryCounterfactualLedger:
    def __init__(self, registry):
        self.events = EventStore(registry)

    def freeze(self, packet: Dict[str, Any], result, *, proposal_id: str,
               review_id: str) -> Dict[str, Any]:
        payload = freeze_payload(packet, result, proposal_id=proposal_id, review_id=review_id)
        counterfactual_id = stable_id(
            'entry_cf', self.events.scope, payload.get('signal_id'), result.decision_id)
        payload['counterfactual_id'] = counterfactual_id
        self.events.record('entry_counterfactual_frozen', counterfactual_id, payload,
                           proposal_id=proposal_id, review_id=review_id,
                           signal_id=payload.get('signal_id'), decision_id=result.decision_id)
        return payload


def simulate(frozen: Dict[str, Any], bars: Iterable[Dict[str, Any]],
             horizons=(1, 3, 5, 10, 20), fee_rate: float = 0.0) -> List[Dict[str, Any]]:
    """用下一交易日开盘执行 R/L 入场模板，并以未使用资金为现金。"""
    rows = list(bars)
    if not rows:
        return []
    rule = (frozen.get('rule_path') or {}).get('template') or {}
    llm = (frozen.get('llm_path') or {}).get('template') or {}
    rule_qty = float(rule.get('quantity') or 0.0)
    llm_qty = float(llm.get('quantity') or 0.0)
    entry = float(rows[0]['open'])
    if rule_qty <= 0 or entry <= 0:
        raise ValueError('规则路径标准数量和下一开盘价必须为正')
    initial = rule_qty * entry
    fee_rate = max(0.0, float(fee_rate))

    def state(qty, template):
        cost = qty * entry * (1.0 + fee_rate)
        return {'quantity': qty, 'cash': initial - cost,
                'stop': float(template.get('initial_stop') or 0.0),
                'peak': initial, 'max_drawdown': 0.0}

    r, l = state(rule_qty, rule), state(llm_qty, llm)
    wanted = set(int(h) for h in horizons)
    outcomes = []
    for index, bar in enumerate(rows, start=1):
        open_price = float(bar['open'])
        low = float(bar.get('low', open_price))
        close = float(bar.get('close', open_price))
        for path in (r, l):
            if path['quantity'] > 0 and path['stop'] > 0 and low <= path['stop']:
                fill = open_price if open_price <= path['stop'] else path['stop']
                path['cash'] += path['quantity'] * fill * (1.0 - fee_rate)
                path['quantity'] = 0.0
            value = path['cash'] + path['quantity'] * close
            path['peak'] = max(path['peak'], value)
            path['max_drawdown'] = min(
                path['max_drawdown'], (value - path['peak']) / path['peak'])
        if index not in wanted:
            continue
        r_value = r['cash'] + r['quantity'] * close
        l_value = l['cash'] + l['quantity'] * close
        r_return, l_return = r_value / initial - 1.0, l_value / initial - 1.0
        delta = l_return - r_return
        outcomes.append({
            'horizon': f'{index}d', 'r_return_pct': r_return,
            'l_return_pct': l_return, 'delta_return_pct': delta,
            'r_max_drawdown_pct': r['max_drawdown'],
            'l_max_drawdown_pct': l['max_drawdown'],
            'saved_loss_pct': max(0.0, delta) if r_return < 0 else 0.0,
            'missed_upside_pct': max(0.0, -delta) if r_return > 0 else 0.0,
            'data_quality': 'good',
        })
    return outcomes
