"""LLM 入场否决 overlay（PR5）：`entry-veto-v1` 三态 PASS/VETO/ABSTAIN。

验证器检查股票/机会/包绑定、动作枚举、VETO 必须有允许原因与有效证据；超时/失败/晚到/
无效输出一律降级 ABSTAIN（VETO 只取消本次机会、释放资金保持现金、不补买）。模型成本
每次尝试都计，无论结果。

成本不可知（`cost_uncertain`）时**不当作零成本**：金额记 0 但标记为待补记，由调用方写
`model_cost` 事件（`uncertain=True` + `attempt_id`），后续以独立补记事件关联同一
`attempt_id` 补扣，净值在此之前是暂定的。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone

SCHEMA_VERSION = 'entry-veto-v1'
ACTIONS = ('PASS', 'VETO', 'ABSTAIN')
# 第一版允许的否决原因：重大指引/经营逻辑反证、重大公司事件风险。
VETO_REASON_CODES = ('MATERIAL_THESIS_CONTRADICTION', 'MATERIAL_COMPANY_EVENT_RISK')
# 非模型 VETO 原因的程序侧 ABSTAIN：调用方不得据此提高任何“模型否决率”
ABSTAIN_REASONS = ('TIMED_OUT', 'FAILED', 'INVALID_OUTPUT', 'LATE_RESPONSE',
                   # 历史 as-of：不该发起实时调用（回复只会因迟到被弃权）
                   'HISTORICAL_AS_OF',
                   # 数据质量门：关键行情/身份缺失或未来
                   'DATA_BLOCKED_QUOTE',
                   # 数据质量门：无可用证据，模型无从判断（引用不到证据的 VETO 必被拒）
                   'INSUFFICIENT_EVIDENCE')
# 未发起任何调用、因而确实零成本的原因（区别于「调用过但成本未知」）
NO_CALL_REASONS = ('HISTORICAL_AS_OF', 'DATA_BLOCKED_QUOTE', 'INSUFFICIENT_EVIDENCE')


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
    cost_uncertain: bool = False  # 本次调用确实发生过但成本不可知 → 待补记
    attempt_id: str = ''  # 绑定该次模型尝试，供补记事件关联


def _cost_of(model_result: dict) -> tuple[int, bool]:
    """已知成本 → (微美元, False)；不可知 → (0, True)。

    0 表示「本次尚未计入费用」，不是「实际免费」。
    """
    raw = model_result.get('cost_micro')
    if raw is None or model_result.get('cost_uncertain'):
        return 0, True
    return int(raw), False


def resolve_overlay(packet: dict, model_result: dict, deadline: str) -> OverlayDecision:
    """把一次模型尝试解析成最终动作；无效/晚到/超时/失败 → ABSTAIN。"""
    cost, uncertain = _cost_of(model_result)
    status = model_result.get('status')
    if status in ABSTAIN_REASONS:
        return OverlayDecision('ABSTAIN', status, cost, '', False, uncertain)
    output = model_result.get('output')
    raw = output.get('action', '') if isinstance(output, dict) else ''
    completed = _parse(model_result.get('completed_at'))
    deadline_dt = _parse(deadline)
    if completed is not None and deadline_dt is not None and completed > deadline_dt:
        return OverlayDecision('ABSTAIN', 'LATE_RESPONSE', cost, raw, True, uncertain)
    valid, _ = validate_model_output(output, packet)
    if not valid:
        return OverlayDecision('ABSTAIN', 'INVALID_OUTPUT', cost, raw, False, uncertain)
    return OverlayDecision(output['action'], output.get('reason_code', ''), cost, raw,
                           False, uncertain)


def decide_overlay(packet: dict, model, deadline: str, *, attempt_id: str = '') -> OverlayDecision:
    """数据质量门 → 模型调用 → 校验，是 overlay 的唯一入口。

    BLOCK（关键行情/身份缺失或未来）→ action='BLOCK'，**不调用模型**、零成本，调用方须
    据此剔除该 intent：ABSTAIN 的语义是「采用父策略」＝照常成交，而 BLOCK 恰恰是「关键
    数据不可用」——照着成交就正是证据层要防的前视。

    LLM_INSUFFICIENT（无可用证据）→ action='ABSTAIN'（采用父策略）且**不调用模型**：
    没有可引用的证据，VETO 必然被验证器拒绝，调用纯属浪费。

    模型必须能看到证据正文（`summary`/`title`），否则三态判断没有依据。
    """
    level = (packet.get('data_quality') or {}).get('level')
    if level == 'BLOCK':
        return OverlayDecision('BLOCK', 'DATA_BLOCKED_QUOTE', 0, '', False, False, attempt_id)
    if level == 'LLM_INSUFFICIENT':
        return OverlayDecision('ABSTAIN', 'INSUFFICIENT_EVIDENCE', 0, '', False, False, attempt_id)
    result = resolve_overlay(packet, model.call(packet, deadline), deadline)
    return replace(result, attempt_id=attempt_id)


class FakeModel:
    """确定性测试模型：可注入 PASS/VETO/ABSTAIN 与成本，无网络。"""

    def __init__(self, action='PASS', reason_code='', evidence_ids=None, *, cost_micro=100,
                 status='OK', completed_at='2026-01-02T00:00:00+00:00', cost_uncertain=False):
        self.action = action
        self.reason_code = reason_code
        self.evidence_ids = evidence_ids or []
        self.cost_micro = cost_micro
        self.status = status
        self.completed_at = completed_at
        self.cost_uncertain = cost_uncertain

    def call(self, packet: dict, deadline: str) -> dict:
        output = {'schema_version': SCHEMA_VERSION,
                  'opportunity_id': packet.get('opportunity_id'),
                  'packet_id': packet.get('packet_id'),
                  'action': self.action,
                  'reason_code': self.reason_code,
                  'evidence_ids': list(self.evidence_ids),
                  'explanation': ''}
        return {'status': self.status, 'output': output, 'completed_at': self.completed_at,
                'cost_micro': self.cost_micro, 'cost_uncertain': self.cost_uncertain}


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

    前置拒绝：截止时刻已过期超 `max_staleness_seconds` → 'HISTORICAL_AS_OF'：历史回放里
    实时调用只会在截止后返回，必然被 LATE_RESPONSE 丢弃，不该发起。

    注意 `deadline` 的语义是「决策最晚仍可执行的时刻」（如次日开盘前的截止），**不是**
    决策发生的时刻：决策在 t 收盘后做出，`completed_at` 落在 as_of 与 deadline 之间才算
    按时。deadline 在调用时天然处于未来，不能据此判前视。

    另：`cost_usd is None`（含 advisor 的 `cost_uncertain`）→ `cost_micro=None`，由调用方
    记成待补记，**不当作零成本**。
    """

    def __init__(self, advisor, *, now=None, max_staleness_seconds: float = 6 * 3600):
        self.advisor = advisor
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.max_staleness_seconds = max_staleness_seconds

    def call(self, packet: dict, deadline: str) -> dict:
        now = self._now()
        deadline_dt = _parse(deadline)
        if deadline_dt is None or (now - deadline_dt).total_seconds() > self.max_staleness_seconds:
            return {'status': 'HISTORICAL_AS_OF', 'output': None,
                    'completed_at': now.isoformat(), 'cost_micro': 0, 'cost_uncertain': False}
        prompt = json.dumps({'decision_type': 'entry_veto',
                             'evidence_packet': packet,
                             'output_schema': ENTRY_VETO_SCHEMA}, ensure_ascii=False)
        output = self.advisor.chat(prompt, system=ENTRY_VETO_SYSTEM)
        completed_at = self._now().isoformat()
        meta = dict(getattr(self.advisor, 'last_metadata', None) or {})
        cost_usd = meta.get('cost_usd')
        cost_micro = None if cost_usd is None else int(round(float(cost_usd) * 1e6))
        status = 'OK' if output is not None else 'FAILED'
        return {'status': status, 'output': output, 'completed_at': completed_at,
                'cost_micro': cost_micro, 'cost_uncertain': cost_micro is None}
