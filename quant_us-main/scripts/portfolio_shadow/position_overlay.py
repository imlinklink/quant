"""LLM 持仓动作 overlay：hold / reduce / exit（+ tighten 的显式空操作）。

与入场 overlay（`entry-veto-v1`）同构，但语义与词汇都不同：

- 模型输出契约**沿用 Position v2**（`position_v2.POSITION_DECISION_SCHEMA` /
  `validate_position_v2` / `build_position_action_templates`），只**追加一个绑定字段**
  `packet_id` —— 影子侧的评审是独立编排，必须能证明这次判断是针对**这个**冻结包作出的。
  `validate_position_v2` 的 schema 是 `additionalProperties: False`，故校验前先摘掉
  绑定字段再交给它，两套契约都不需要改动。
- `Application.action` 用本模块的动作词汇。**减仓档位编进动作名**
  （`POSITION_REDUCE_25`）：档位必须与动作同生共死，而给 `Application` 加字段会改变
  已落库事件的 payload（须递增账本 schema），会让现有冻结实验的账本失效。
- 成本、弃权原因、程序侧/模型侧弃权的区分，全部复用 `llm_overlay` 的既有词汇与函数，
  这样报告侧的 `is_program_abstain` / `program_abstain_class` 对持仓同样成立。
"""
from __future__ import annotations

import json
from dataclasses import replace

from mutifactor.llm.contracts.position_v2 import (POSITION_DECISION_SCHEMA,
                                                  POSITION_SYSTEM,
                                                  POSITION_V2_PROMPT_VERSION,
                                                  POSITION_V2_SCHEMA_VERSION,
                                                  REDUCE_TIERS, validate_position_v2)

from .llm_overlay import ABSTAIN_REASONS, OverlayDecision, RealModel, gate

# 影子侧持仓评审的输出契约版本：Position v2 的输出 + packet 绑定要求，故独立于
# `POSITION_V2_SCHEMA_VERSION`（实盘那条路不需要绑定字段，两者不是同一个问题）。
POSITION_ACTION_SCHEMA_VERSION = 'position-action-v1'

# 应用动作词汇（写进 Application.action，与入场的 PASS/VETO/ABSTAIN 命名空间不冲突）
POSITION_ABSTAIN = 'POSITION_ABSTAIN'
POSITION_HOLD = 'POSITION_HOLD'
POSITION_EXIT = 'POSITION_EXIT'
# tighten_protection 目前恒为空操作：程序模板的 new_protection_price 就是当前保护线
# （position_v2.build_position_action_templates）。如实记录，不伪造一次收紧。
POSITION_TIGHTEN_NOT_APPLIED = 'POSITION_TIGHTEN_NOT_APPLIED'

# 真正会改变 L 路持仓的动作（其余都不改变路径，不得计入「计划改变率」）
PATH_CHANGING_ACTIONS = (POSITION_EXIT,) + tuple(
    f'POSITION_REDUCE_{int(t * 100)}' for t in REDUCE_TIERS)


def reduce_action(tier: float) -> str:
    return f'POSITION_REDUCE_{int(round(float(tier) * 100))}'


def subject_key_for(opportunity_id: str, execution_session: str) -> str:
    """持仓评审的账本主体键。按**执行日**而非评审日定键：结算时按执行日直接取回，
    不必再推前置会话。"""
    return f'{opportunity_id}@pos:{execution_session}'


def reduce_tier(action: str) -> float | None:
    """从动作名解析减仓档位；非减仓动作返回 None。"""
    prefix = 'POSITION_REDUCE_'
    if not str(action or '').startswith(prefix):
        return None
    try:
        return int(action[len(prefix):]) / 100.0
    except ValueError:
        return None


# ---- 模型输出契约：Position v2 + packet 绑定 ----

# 影子侧独有的键：模型可以给（给了也不算错），但交回 Position v2 校验器前必须摘掉
# —— 它的 schema 是 additionalProperties: False。
_OVERLAY_ONLY_KEYS = ('packet_id', 'schema_version')

POSITION_ACTION_SCHEMA = {
    **POSITION_DECISION_SCHEMA,
    'properties': {**POSITION_DECISION_SCHEMA['properties'],
                   'packet_id': {'type': 'string'},
                   'schema_version': {'type': 'string'}},
    'required': [*POSITION_DECISION_SCHEMA['required'], 'packet_id'],
}

POSITION_ACTION_SYSTEM = POSITION_SYSTEM + (
    '\n本次为影子持仓评审，另有一条程序性要求：输出必须回填 packet_id，'
    '其值等于输入里的 packet_id，用于证明判断针对的是这个冻结证据包。\n'
    'reduce 与 exit 只能引用输入 new_evidence 里的 evidence_id，且**至少一条必须属于本持仓证券'
    '自己**（subject_code 等于本次交易的 code）。市场级日报（subject_code=MARKET）对每个持仓'
    '都可见，那是背景；市场事实规则计划已经用市场门考虑过了，所以它**单独撑不起一次减仓或退出**。'
)


def build_position_action_prompt(packet: dict) -> str:
    return json.dumps({'decision_type': 'position_action', 'input': packet,
                       'output_schema': POSITION_ACTION_SCHEMA}, ensure_ascii=False)


def validate_position_output(output, packet: dict) -> tuple[bool, list[str]]:
    """校验模型输出：先绑包，再交给 Position v2 契约。"""
    errors: list[str] = []
    if not isinstance(output, dict):
        return False, ['NOT_DICT']
    if output.get('packet_id') != packet.get('packet_id'):
        errors.append('PACKET_MISMATCH')
        return False, errors
    core = {k: v for k, v in output.items() if k not in _OVERLAY_ONLY_KEYS}
    errors.extend(validate_position_v2(core, packet))
    if errors:
        return False, errors
    # 归属**不在这里拦截**（详见 `llm_overlay.validate_model_output` 的同一处说明）：
    # 这里曾有一道 `POSITION_NO_COMPANY_EVIDENCE`（要求 reduce/exit 至少引用一条本证券证据），
    # 但当前证据供给只有市场级日报 ⇒ 该守卫使 reduce/exit **结构上不可达**，L 恒等于 R。
    # 「至少一条有效引用」由 `validate_position_v2` 保证；归属构成改由报告如实披露。
    return True, errors


def _template_for(packet: dict, output: dict) -> dict:
    tid = output.get('action_template_id')
    return {t.get('template_id'): t for t in packet.get('allowed_actions') or []}.get(tid) or {}


def _reduce_tier_from(packet: dict, output: dict) -> float | None:
    """优先取程序模板的 constraints.tier，回退解析 template_id 的 ':reduce:NN' 后缀。"""
    template = _template_for(packet, output)
    tier = (template.get('constraints') or {}).get('tier')
    if tier is not None:
        try:
            return float(tier)
        except (TypeError, ValueError):
            pass
    tid = str(output.get('action_template_id') or '')
    if ':reduce:' in tid:
        try:
            return int(tid.rsplit(':', 1)[1]) / 100.0
        except ValueError:
            return None
    return None


def applied_action(output: dict, packet: dict) -> str:
    """把 Position v2 动作映射成应用动作词汇。已通过校验的输出才可调用。"""
    action = output.get('action')
    if action == 'reduce':
        tier = _reduce_tier_from(packet, output)
        return reduce_action(tier) if tier else POSITION_HOLD
    if action == 'exit':
        return POSITION_EXIT
    if action == 'tighten_protection':
        return POSITION_TIGHTEN_NOT_APPLIED
    # hold 与 post_exit_review 都不改变持仓：L 路照常服从父策略。
    # 两者的区别留在 raw_action 与尝试记录的原始输出里，供决策追踪展开。
    return POSITION_HOLD


def resolve_position_overlay(packet: dict, model_result: dict, deadline: str) -> OverlayDecision:
    """把一次模型尝试解析成最终动作；无效/晚到/超时/失败 → 程序侧弃权（采用父策略）。"""
    from .llm_overlay import _cost_of, _parse
    cost, uncertain = _cost_of(model_result)
    status = model_result.get('status')
    if status in ABSTAIN_REASONS:
        return OverlayDecision(POSITION_ABSTAIN, status, cost, '', False, uncertain)
    output = model_result.get('output')
    raw = str(output.get('action') or '') if isinstance(output, dict) else ''
    completed = _parse(model_result.get('completed_at'))
    deadline_dt = _parse(deadline)
    if completed is not None and deadline_dt is not None and completed > deadline_dt:
        return OverlayDecision(POSITION_ABSTAIN, 'LATE_RESPONSE', cost, raw, True, uncertain)
    valid, _ = validate_position_output(output, packet)
    if not valid:
        return OverlayDecision(POSITION_ABSTAIN, 'INVALID_OUTPUT', cost, raw, False, uncertain)
    return OverlayDecision(applied_action(output, packet),
                           (output.get('reason_codes') or [''])[0], cost, raw, False, uncertain)


def decide_position_overlay(packet: dict, model, deadline: str, *,
                            attempt_id: str = '') -> OverlayDecision:
    """数据质量门 → 模型调用 → 校验，是持仓 overlay 的唯一入口。

    BLOCK（关键行情/身份缺失或未来）与 LLM_INSUFFICIENT（无可用证据）都**不调用模型、
    零成本**，一律落成 `POSITION_ABSTAIN`：对持仓而言「采用父策略」＝照常服从规则退出，
    这正是数据不可用时应有的安全结果（与入场侧不同 —— 入场侧 BLOCK 必须剔除 intent，
    因为「照常成交」在那里才是错的）。
    """
    gated = gate(packet)
    if gated is not None:
        # gated[1] 即 DATA_BLOCKED_QUOTE / INSUFFICIENT_EVIDENCE，报告侧据此分入
        # data_blocked / quality_abstain 而非「模型被问过但弃权」。
        return OverlayDecision(POSITION_ABSTAIN, gated[1], 0, '', False, False, attempt_id)
    result = resolve_position_overlay(packet, model.call(packet, deadline), deadline)
    return replace(result, attempt_id=attempt_id)


class PositionRealModel(RealModel):
    """真实 LLM 持仓评审。复用 `RealModel` 的两道前置门与成本语义，只换提示词与契约。"""

    def _system(self) -> str:
        return POSITION_ACTION_SYSTEM

    def _prompt(self, packet: dict) -> str:
        return build_position_action_prompt(packet)


class FakePositionModel:
    """确定性测试模型：可注入动作与成本，无网络。"""

    def __init__(self, action='hold', *, template_id='', reason_code='',
                 evidence_from_packet=False, cost_micro=100, status='OK',
                 completed_at='2026-01-02T00:00:00+00:00', cost_uncertain=False,
                 thesis_state='CONFIRMED'):
        self.action = action
        self.template_id = template_id
        # 默认空：原因码必须是 position 角色的合法取值（`reason_codes.valid_for_role`），
        # 给一个像 'POSITION_REVIEW' 这样的自造值会被校验器拒，让测试看起来像别的问题。
        self.reason_code = reason_code
        self.evidence_from_packet = evidence_from_packet
        self.cost_micro = cost_micro
        self.status = status
        self.completed_at = completed_at
        self.cost_uncertain = cost_uncertain
        self.thesis_state = thesis_state

    def call(self, packet: dict, deadline: str) -> dict:
        evidence_ids = []
        if self.evidence_from_packet:
            # 优先引用**本证券**的证据；当前证据供给只有市场级日报，故回退到包内任一证据
            # （MARKET 是设计 §7.1 允许的归属，归属构成由报告披露）。
            code = (packet.get('trade') or {}).get('code')
            items = [(e.get('evidence_id'), e.get('subject_code'),
                      e.get('summary') or '') for e in packet.get('new_evidence') or []
                     if e.get('evidence_id')]
            same = [row for row in items if row[1] == code]
            pool = same or items
            evidence_ids = [row[0] for row in pool[:1]]
            # fact 必须**逐字匹配**证据摘要（`validate_claims`），故引用谁就用谁的原文
            summary = pool[0][2] if pool else ''
        claim = {'text': summary, 'claim_type': 'fact', 'evidence_ids': evidence_ids}
        output = {
            'schema_version': POSITION_V2_SCHEMA_VERSION,
            'packet_id': packet.get('packet_id'),
            'status': 'complete',
            'thesis_state': self.thesis_state,
            'action': self.action,
            'action_template_id': self.template_id or None,
            'confidence': 'medium',
            'reason_codes': [self.reason_code] if self.reason_code else [],
            'facts': [claim] if evidence_ids else [],
            'inferences': [],
            'counterevidence': [],
            'missing_information': ['测试用缺口声明'],
        }
        return {'status': self.status, 'output': output, 'completed_at': self.completed_at,
                'cost_micro': self.cost_micro, 'cost_uncertain': self.cost_uncertain}


__all__ = ['POSITION_ABSTAIN', 'POSITION_ACTION_SCHEMA', 'POSITION_ACTION_SCHEMA_VERSION',
           'POSITION_ACTION_SYSTEM', 'POSITION_EXIT', 'POSITION_HOLD',
           'POSITION_TIGHTEN_NOT_APPLIED',
           'PATH_CHANGING_ACTIONS', 'POSITION_V2_PROMPT_VERSION',
           'POSITION_V2_SCHEMA_VERSION', 'FakePositionModel', 'PositionRealModel',
           'applied_action', 'build_position_action_prompt', 'decide_position_overlay',
           'reduce_action', 'reduce_tier', 'resolve_position_overlay',
           'validate_position_output']
