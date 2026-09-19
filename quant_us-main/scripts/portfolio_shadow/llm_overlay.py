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
                   # 决策时点早于模型训练数据截止：模型「知道」当时还不可能知道的事。
                   # 这是 as-of 证据过滤修不好的泄漏，只能拒绝并如实披露。
                   'MODEL_KNOWLEDGE_CUTOFF',
                   # 数据质量门：关键行情/身份缺失或未来
                   'DATA_BLOCKED_QUOTE',
                   # 数据质量门：无可用证据，模型无从判断（引用不到证据的 VETO 必被拒）
                   'INSUFFICIENT_EVIDENCE',
                   # 上一次调用已发起但结果未落库（进程挂在两者之间）：钱可能已花且金额
                   # 不可知，不复用也不重试付费，按 ABSTAIN 采用父策略并把成本挂账待补记
                   'RECALL_ABANDONED',
                   # 评审窗口已过，由结算按设计 §3.3 冻结 —— **模型从未被咨询**。
                   # 漏掉它会让「没被问过」在报告里长成「被问过但采用父策略」。
                   'DECISION_DEADLINE_MISSED')
# 未发起任何调用、因而确实零成本的原因（区别于「调用过但成本未知」）
NO_CALL_REASONS = ('HISTORICAL_AS_OF', 'MODEL_KNOWLEDGE_CUTOFF', 'DATA_BLOCKED_QUOTE',
                   'INSUFFICIENT_EVIDENCE', 'DECISION_DEADLINE_MISSED')
# 上两类在报告里的细分（`ABSTAIN_REASONS` 的下属分组，并集必须等于 ABSTAIN_REASONS）：
#   数据拦截 —— 机会级，连 R 一起拦（设计 §5.3），从「模型可评审」分母里剔除
DATA_BLOCK_REASONS = ('DATA_BLOCKED_QUOTE',)
#   质量弃权 —— 包本身不足以判断，同样不进入模型分母
QUALITY_ABSTAIN_REASONS = ('INSUFFICIENT_EVIDENCE',)
#   故障/降级 —— 本该有判断却没有：分母要算它，否则缺口会被算成「模型没有价值」
FAILURE_ABSTAIN_REASONS = ('TIMED_OUT', 'FAILED', 'INVALID_OUTPUT', 'LATE_RESPONSE',
                           'RECALL_ABANDONED', 'MODEL_KNOWLEDGE_CUTOFF', 'HISTORICAL_AS_OF',
                           'DECISION_DEADLINE_MISSED')


def is_program_abstain(reason_code) -> bool:
    """该动作是否由**程序侧**产生（模型没有参与判断）。

    这些原因下 `ABSTAIN` 的语义是「采用父策略」，于是 L 会与 R 走出**完全相同**的仓位 ——
    报告侧必须据此把「没被问过」与「被问过、模型自己弃权」分开：混在一起时，一次调度缺口
    会被读成「模型没有价值」，而真相是模型从未参与。这是 `ABSTAIN_REASONS` 里那句
    「调用方不得据此提高任何模型否决率」在报告侧的执行。
    """
    return reason_code in ABSTAIN_REASONS


def program_abstain_class(reason_code) -> str | None:
    """程序侧弃权的细分：`data_blocked` / `quality_abstain` / `failure`；非程序侧返回 None。"""
    if reason_code in DATA_BLOCK_REASONS:
        return 'data_blocked'
    if reason_code in QUALITY_ABSTAIN_REASONS:
        return 'quality_abstain'
    if reason_code in FAILURE_ABSTAIN_REASONS:
        return 'failure'
    return None


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
        # 必须说明「规则计划漏了什么、它如何改变本次机会」：只复述指标或只说不安，
        # 都不是语义增量（设计 §6 的三个结构化问题）
        if not str(output.get('thesis_contrast') or '').strip():
            errors.append('VETO_NO_THESIS_CONTRAST')
        evidence_ids = output.get('evidence_ids') or []
        if not evidence_ids:
            errors.append('VETO_NO_EVIDENCE')
        else:
            by_id = {e.get('evidence_id'): e for e in packet.get('events') or []}
            unknown = [eid for eid in evidence_ids if eid not in by_id]
            if unknown:
                errors.append('EVIDENCE_NOT_IN_PACKET')
            # 归属**不在这里拦截**：设计 §7.1 把 MARKET 列为允许的归属，§7.2 对排除类动作
            # 只要求"至少一条支持排除的有效证据"；审计 §4.2 的原话是"要求至少一条有效引用，
            # 并在报告中分开统计同证券与市场/板块证据"。此处曾有一道更严的守卫
            # （`VETO_NO_COMPANY_EVIDENCE`，要求至少一条本证券证据，由用户在 2026-09 选定），
            # 但当前证据供给只有市场级日报 ⇒ 该守卫使 VETO **结构上不可达**，L 恒等于 R。
            # 现已放宽到设计与审计的原始要求，归属构成改由 `citation_subjects` 如实披露。
    return (not errors), errors


def citation_subjects(evidence_ids, events, *, subject_id: str = '') -> dict:
    """被引用证据的**归属构成**（同证券 / 各市场级 / 未知），用于披露而非拦截。

    这是放宽 `VETO_NO_COMPANY_EVIDENCE` 那一类守卫的配套：不再禁止市场证据驱动决策，
    但必须让「这次判断仅由市场级证据支撑」在账本与报告里看得见。
    """
    by_id = {e.get('evidence_id'): e for e in (events or [])}
    counts: dict = {}
    for eid in evidence_ids or ():
        event = by_id.get(eid)
        if event is None:
            continue
        owner = str(event.get('security_id') or '')
        key = 'self' if owner and owner == str(subject_id) else (owner or 'UNKNOWN')
        counts[key] = counts.get(key, 0) + 1
    return counts


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


def gate(packet: dict) -> tuple[str, str] | None:
    """数据质量门。返回 (action, reason) 表示短路；None 表示可以发起模型调用。

    与角色无关（只读 `packet['data_quality']['level']`），故入场与持仓共用。
    """
    level = (packet.get('data_quality') or {}).get('level')
    if level == 'BLOCK':
        return 'BLOCK', 'DATA_BLOCKED_QUOTE'
    if level == 'LLM_INSUFFICIENT':
        return 'ABSTAIN', 'INSUFFICIENT_EVIDENCE'
    return None


def _gate(packet: dict) -> tuple[str, str] | None:  # 兼容旧调用点
    return gate(packet)


def model_call_expected(packet: dict) -> bool:
    """该包是否会真正发起模型调用。调用方据此决定要不要先写 PENDING 尝试记录。"""
    return _gate(packet) is None


def decide_overlay(packet: dict, model, deadline: str, *, attempt_id: str = '') -> OverlayDecision:
    """数据质量门 → 模型调用 → 校验，是 overlay 的唯一入口。

    BLOCK（关键行情/身份缺失或未来）→ action='BLOCK'，**不调用模型**、零成本，调用方须
    据此剔除该 intent：ABSTAIN 的语义是「采用父策略」＝照常成交，而 BLOCK 恰恰是「关键
    数据不可用」——照着成交就正是证据层要防的前视。

    LLM_INSUFFICIENT（无可用证据）→ action='ABSTAIN'（采用父策略）且**不调用模型**：
    没有可引用的证据，VETO 必然被验证器拒绝，调用纯属浪费。

    模型必须能看到证据正文（`summary`/`title`），否则三态判断没有依据。
    """
    gated = _gate(packet)
    if gated is not None:
        return OverlayDecision(gated[0], gated[1], 0, '', False, False, attempt_id)
    result = resolve_overlay(packet, model.call(packet, deadline), deadline)
    return replace(result, attempt_id=attempt_id)


class FakeModel:
    """确定性测试模型：可注入 PASS/VETO/ABSTAIN 与成本，无网络。"""

    def __init__(self, action='PASS', reason_code='', evidence_ids=None, *, cost_micro=100,
                 status='OK', completed_at='2026-01-02T00:00:00+00:00', cost_uncertain=False,
                 evidence_from_packet=False, thesis_contrast='规则计划未覆盖的测试事实'):
        self.action = action
        # VETO 必须引用包内**本证券**的证据，否则会被验证器降级成 INVALID_OUTPUT。确定性的
        # VETO fixture（设计 §11 验收案例）需要这个开关 —— 它只能看到调用时传入的那个包。
        self.evidence_from_packet = evidence_from_packet
        self.reason_code = reason_code
        self.evidence_ids = evidence_ids or []
        # 默认给一个非空值：多数 VETO 测试关心的是别的东西（成本、执行、恢复），
        # 「缺 thesis_contrast 会被拒」由专门的测试用空值覆盖。
        self.thesis_contrast = thesis_contrast
        self.cost_micro = cost_micro
        self.status = status
        self.completed_at = completed_at
        self.cost_uncertain = cost_uncertain

    def call(self, packet: dict, deadline: str) -> dict:
        evidence_ids = list(self.evidence_ids)
        if self.evidence_from_packet:
            events = packet.get('events') or []
            # **优先取本证券的证据**：生产包的顺序是「市场级在前、证券事件在后」，直接取
            # 第一条会拿到 MARKET —— 那不是能支撑否决的证据（验证器会拒）。
            sid = packet.get('security_id')
            same = [e['evidence_id'] for e in events if e.get('security_id') == sid]
            pool = same or [e['evidence_id'] for e in events]
            evidence_ids = pool[:1] if pool else []
        output = {'schema_version': SCHEMA_VERSION,
                  'opportunity_id': packet.get('opportunity_id'),
                  'packet_id': packet.get('packet_id'),
                  'action': self.action,
                  'reason_code': self.reason_code,
                  'evidence_ids': evidence_ids,
                  'thesis_contrast': self.thesis_contrast,
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
    'VETO 的额外要求（任一不满足即视为无效输出，会被降级为 ABSTAIN）：\n'
    '- 必须反驳规则计划本身（说明规则漏看了什么、它如何改变本次机会），\n'
    '  而不是复述包里的指标或规则已经编码过的市场事实。\n'
    '- evidence_ids 只能从给定证据包里逐字选取，不得编造。\n'
    '  市场级日报（security_id=MARKET）是**允许的依据**；但它撑不起「本证券基本面已恶化」\n'
    '  这类论断 —— 若你的反证只来自市场级背景，请在 thesis_contrast 里明确说明这是市场层面的判断。\n'
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
        # 仅 VETO 必填（故不放进 required，否则 PASS/ABSTAIN 也要填）：规则计划漏看的事实，
        # 以及它如何改变本次机会。由 validate_model_output 强制。
        'thesis_contrast': {'type': 'string'},
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

    另两条在 `call` 里：
    - 决策时点早于 `knowledge_cutoff` → 'MODEL_KNOWLEDGE_CUTOFF'。这是 as-of 证据过滤
      修不好的泄漏（模型知道当时不可能知道的事），只能拒绝并在包/账本里如实披露。
    - `cost_usd is None`（含 advisor 的 `cost_uncertain`）→ `cost_micro=None`，由调用方
      记成待补记，**不当作零成本**。

    注意 `deadline` 的语义是「决策最晚仍可执行的时刻」（如次日开盘前的截止），**不是**
    决策发生的时刻：决策在 t 收盘后做出，`completed_at` 落在 as_of 与 deadline 之间才算
    按时。deadline 在调用时天然处于未来，不能据此判前视。
    """

    def __init__(self, advisor, *, now=None, max_staleness_seconds: float = 6 * 3600,
                 knowledge_cutoff=None, allow_historical: bool = False):
        self.advisor = advisor
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.max_staleness_seconds = max_staleness_seconds
        # 模型训练数据的截止时刻（ISO）。决策时点早于它 ⇒ 模型「知道」当时还不可能
        # 知道的事，as-of 证据过滤修不好，只能拒绝。None = 未声明（不做该检查，但
        # 这种未声明本身会被 manifest 的 freeze 门挡在正式运行之外）。
        self.knowledge_cutoff = knowledge_cutoff
        # 仅用于设计 §9 的 `historical_debug` 提示词调试：放开历史 as-of 与知识截止两道门，
        # 让历史日期也能发起真实调用。**调试调用不写 Application、不进正式 R/L 表现**，
        # 这个开关不得用于正式运行路径。
        self.allow_historical = allow_historical

    def _refusal(self, packet: dict, deadline: str, now) -> dict | None:
        """发起调用前的程序侧拒绝；返回 None 表示可以调用。角色无关，故可被子类复用。"""
        deadline_dt = _parse(deadline)
        if self.allow_historical:
            return None
        if (deadline_dt is None
                or (now - deadline_dt).total_seconds() > self.max_staleness_seconds):
            return {'status': 'HISTORICAL_AS_OF', 'output': None,
                    'completed_at': now.isoformat(), 'cost_micro': 0,
                    'cost_uncertain': False}
        cutoff = _parse(self.knowledge_cutoff)
        as_of = _parse(packet.get('as_of'))
        if cutoff is not None and as_of is not None and as_of < cutoff:
            return {'status': 'MODEL_KNOWLEDGE_CUTOFF', 'output': None,
                    'completed_at': now.isoformat(), 'cost_micro': 0,
                    'cost_uncertain': False}
        return None

    def _system(self) -> str:
        return ENTRY_VETO_SYSTEM

    def _prompt(self, packet: dict) -> str:
        return json.dumps({'decision_type': 'entry_veto', 'evidence_packet': packet,
                           'output_schema': ENTRY_VETO_SCHEMA}, ensure_ascii=False)

    def call(self, packet: dict, deadline: str) -> dict:
        now = self._now()
        refusal = self._refusal(packet, deadline, now)
        if refusal is not None:
            return refusal
        output = self.advisor.chat(self._prompt(packet), system=self._system())
        completed_at = self._now().isoformat()
        meta = dict(getattr(self.advisor, 'last_metadata', None) or {})
        cost_usd = meta.get('cost_usd')
        cost_micro = None if cost_usd is None else int(round(float(cost_usd) * 1e6))
        status = 'OK' if output is not None else 'FAILED'
        return {'status': status, 'output': output, 'completed_at': completed_at,
                'cost_micro': cost_micro, 'cost_uncertain': cost_micro is None}
