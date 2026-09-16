"""LLM 入场否决 overlay（PR5）：`entry-veto-v1` 三态 PASS/VETO/ABSTAIN。

验证器检查股票/机会/包绑定、动作枚举、VETO 必须有允许原因与有效证据；超时/失败/晚到/
无效输出一律降级 ABSTAIN（VETO 只取消本次机会、释放资金保持现金、不补买）。模型成本
每次尝试都计，无论结果。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

SCHEMA_VERSION = 'entry-veto-v1'
ACTIONS = ('PASS', 'VETO', 'ABSTAIN')
# 第一版允许的否决原因：重大指引/经营逻辑反证、重大公司事件风险。
VETO_REASON_CODES = ('MATERIAL_THESIS_CONTRADICTION', 'MATERIAL_COMPANY_EVENT_RISK')
# 程序侧 ABSTAIN 原因（非模型 VETO 原因）
ABSTAIN_REASONS = ('TIMED_OUT', 'FAILED', 'INVALID_OUTPUT', 'LATE_RESPONSE')


def _parse(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def validate_model_output(output, packet: dict) -> tuple[bool, list[str]]:
    """校验模型输出契约；返回 (valid, errors)。无效即转 ABSTAIN。"""
    errors = []
    if not isinstance(output, dict):
        return False, ['NOT_DICT']
    if output.get('schema_version') != SCHEMA_VERSION:
        errors.append('SCHEMA_VERSION_MISMATCH')
    if output.get('opportunity_id') != packet.get('opportunity_id'):
        errors.append('OPPORTUNITY_MISMATCH')
    if output.get('packet_id') != packet.get('packet_id'):
        errors.append('PACKET_MISMATCH')
    action = output.get('action')
    if action not in ACTIONS:
        errors.append('ACTION_INVALID')
        return False, errors
    if action == 'VETO':
        if output.get('reason_code') not in VETO_REASON_CODES:
            errors.append('REASON_NOT_ALLOWED')
        evidence_ids = output.get('evidence_ids') or []
        if not evidence_ids:
            errors.append('VETO_NO_EVIDENCE')
        else:
            valid_ids = {e['evidence_id'] for e in packet.get('events', [])}
            if not all(eid in valid_ids for eid in evidence_ids):
                errors.append('EVIDENCE_NOT_IN_PACKET')
    return (not errors), errors


@dataclass(frozen=True)
class OverlayDecision:
    action: str  # 实际应用动作 PASS/VETO/ABSTAIN
    reason_code: str
    model_cost: int
    raw_action: str = ''  # 模型原始动作（可能被降级覆盖）
    late_response_observed: bool = False


def resolve_overlay(packet: dict, model_result: dict, deadline: str) -> OverlayDecision:
    """把一次模型尝试解析成最终动作；无效/晚到/超时/失败 → ABSTAIN。"""
    cost = int(model_result.get('cost_micro', 0))
    status = model_result.get('status')
    if status in ('FAILED', 'TIMED_OUT'):
        return OverlayDecision('ABSTAIN', status, cost, '', False)
    output = model_result.get('output')
    raw = output.get('action', '') if isinstance(output, dict) else ''
    completed = _parse(model_result.get('completed_at'))
    deadline_dt = _parse(deadline)
    if completed is not None and deadline_dt is not None and completed > deadline_dt:
        return OverlayDecision('ABSTAIN', 'LATE_RESPONSE', cost, raw, True)
    valid, _ = validate_model_output(output, packet)
    if not valid:
        return OverlayDecision('ABSTAIN', 'INVALID_OUTPUT', cost, raw, False)
    return OverlayDecision(output['action'], output.get('reason_code', ''), cost, raw, False)


class FakeModel:
    """确定性测试模型：可注入 PASS/VETO/ABSTAIN 与成本，无网络。"""

    def __init__(self, action='PASS', reason_code='', evidence_ids=None, *, cost_micro=100,
                 status='OK', completed_at='2026-01-02T00:00:00+00:00'):
        self.action = action
        self.reason_code = reason_code
        self.evidence_ids = evidence_ids or []
        self.cost_micro = cost_micro
        self.status = status
        self.completed_at = completed_at

    def call(self, packet: dict, deadline: str) -> dict:
        output = {'schema_version': SCHEMA_VERSION,
                  'opportunity_id': packet.get('opportunity_id'),
                  'packet_id': packet.get('packet_id'),
                  'action': self.action,
                  'reason_code': self.reason_code,
                  'evidence_ids': list(self.evidence_ids),
                  'explanation': ''}
        return {'status': self.status, 'output': output, 'completed_at': self.completed_at,
                'cost_micro': self.cost_micro}


ENTRY_VETO_SYSTEM = (
    '你是严格的美股入场风险否决器。任务：根据给定的候选证据包，判断是否否决该次入场。\n'
    '只允许三种动作：\n'
    '- PASS：证据无重大反对，放行（采用父策略）。\n'
    '- VETO：存在重大指引/经营逻辑反证或重大公司事件风险，否决本次入场；必须引用证据包里的 evidence_ids。\n'
    '- ABSTAIN：证据不足或无法判断，采用父策略（不否决）。\n'
    'VETO 只允许 reason_code：\n'
    '- MATERIAL_THESIS_CONTRADICTION：重大指引/经营逻辑反证\n'
    '- MATERIAL_COMPANY_EVENT_RISK：重大公司事件风险\n'
    '只依据给定证据推理，不编造；不得以「资金不足」等程序理由否决。输出严格 JSON，遵循 output_schema。'
)

ENTRY_VETO_SCHEMA = {
    'type': 'object',
    'properties': {
        'schema_version': {'type': 'string', 'const': 'entry-veto-v1'},
        'opportunity_id': {'type': 'string'},
        'packet_id': {'type': 'string'},
        'action': {'type': 'string', 'enum': ['PASS', 'VETO', 'ABSTAIN']},
        'reason_code': {'type': 'string'},
        'evidence_ids': {'type': 'array', 'items': {'type': 'string'}},
        'counterevidence_ids': {'type': 'array', 'items': {'type': 'string'}},
        'explanation': {'type': 'string'},
    },
    'required': ['schema_version', 'opportunity_id', 'packet_id', 'action'],
}


class RealModel:
    """真实 LLM 入场否决（DeepSeek via `mutifactor.llm.LLMAdvisor`）。

    `call(packet, deadline)` 构建 entry-veto prompt → `advisor.chat` → 映射成
    `model_result`（status/output/completed_at/cost_micro），供 `resolve_overlay` 使用。
    chat 返回 None（超时/失败/重试耗尽）→ status='FAILED'（resolve_overlay 会降级 ABSTAIN）。
    """

    def __init__(self, advisor):
        self.advisor = advisor

    def call(self, packet: dict, deadline: str) -> dict:
        prompt = json.dumps({'decision_type': 'entry_veto',
                             'evidence_packet': packet,
                             'output_schema': ENTRY_VETO_SCHEMA}, ensure_ascii=False)
        output = self.advisor.chat(prompt, system=ENTRY_VETO_SYSTEM)
        completed_at = datetime.now(timezone.utc).isoformat()
        meta = getattr(self.advisor, 'last_metadata', None) or {}
        cost_usd = meta.get('cost_usd') or 0.0
        cost_micro = int(round(float(cost_usd) * 1e6))
        if output is None:
            return {'status': 'FAILED', 'output': None, 'completed_at': completed_at,
                    'cost_micro': cost_micro}
        return {'status': 'OK', 'output': output, 'completed_at': completed_at,
                'cost_micro': cost_micro}
