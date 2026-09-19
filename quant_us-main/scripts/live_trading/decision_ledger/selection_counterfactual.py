"""Selection 排序的容量反事实：R 用规则顺序，L 用模型 portfolio_rank。"""
from typing import Any, Dict, List

from .event_store import EventStore, stable_id


def freeze_payload(packet: Dict[str, Any], result, *, batch_id: str,
                   max_positions: int) -> Dict[str, Any]:
    universe = packet.get('universe') or {}
    eligible = list(universe.get('execution_eligible_codes') or
                    universe.get('discovery_codes') or [])
    output = result.validated_output or {}
    ranked = [row for row in output.get('ranked', []) if row.get('decision') == 'candidate'
              and row.get('code') in eligible]
    ranked.sort(key=lambda row: (row.get('portfolio_rank', 10**9), row.get('code', '')))
    capacity = max(1, int(max_positions))
    return {
        'experiment_version': 'selection-capacity-counterfactual-v1',
        'decision_id': result.decision_id,
        'batch_id': batch_id,
        'as_of': (packet.get('context') or {}).get('as_of'),
        'capacity': capacity,
        'rule_selected': eligible[:capacity],
        'llm_selected': [row['code'] for row in ranked[:capacity]],
        'replaced_out': [code for code in eligible[:capacity]
                         if code not in {row['code'] for row in ranked[:capacity]}],
        'replaced_in': [row['code'] for row in ranked[:capacity]
                        if row['code'] not in eligible[:capacity]],
        'ranked': [{'code': row['code'], 'portfolio_rank': row.get('portfolio_rank'),
                    'evidence_ids': sorted({eid for field in ('thesis', 'counterevidence')
                                             for claim in row.get(field, [])
                                             for eid in claim.get('evidence_ids', [])})}
                   for row in ranked],
    }


class SelectionCounterfactualLedger:
    def __init__(self, registry):
        self.events = EventStore(registry)

    def freeze(self, packet: Dict[str, Any], result, *, batch_id: str,
               max_positions: int) -> Dict[str, Any]:
        payload = freeze_payload(packet, result, batch_id=batch_id,
                                 max_positions=max_positions)
        counterfactual_id = stable_id('selection_cf', self.events.scope,
                                      batch_id, result.decision_id)
        payload['counterfactual_id'] = counterfactual_id
        self.events.record('selection_counterfactual_frozen', counterfactual_id, payload,
                           decision_id=result.decision_id)
        return payload
