"""Review Decision v1（设计 §6.5）：从结果提出**可检验**的协议改进假设。

Review **不参与单笔交易执行**，也不改配置。它的输出只有一件事：
`protocol_change_candidate` —— 一个可以被回放检验、但**必须人工批准**才会成为新版本的候选。

三条构造性约束（写在这里，并由测试钉死）：

1. **单一变量**：`proposed_change.variable` 必须取自 `CHANGEABLE_VARIABLES` 白名单，且
   只允许一个变量。改动多个变量会让效果无法归因，"验证是否改善"就退化成"碰巧变好了"。
2. **必须自带否证条件**：`validation_plan` 必须给出观察窗口、最低样本数与**停止条件** ——
   没有停止条件的假设不是假设，是许愿。
3. **不可自动生效**：本模块没有任何写配置/改权限的入口。候选只落成 append-only 事件，
   由 `protocol_changes.record_candidate` 写入；`approve/reject` 也只写事件。
   白名单里**不含任何安全开关**（硬退出、风险预算上限、最大仓位数等），
   所以「模型把自己的约束改松」在构造上不可能。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

REVIEW_V1_SCHEMA_VERSION = 'review-v1'
REVIEW_V1_PROMPT_VERSION = 'review-v1'

# 可提议修改的协议变量白名单 → 允许的方向。
# **刻意不含安全开关**：硬退出开关、总风险预算、单笔上限、最大仓位数、回撤阶梯阈值
# 都不在表内 —— 让 Review 有权提议放松自己的约束，是这条链路最不该有的能力。
CHANGEABLE_VARIABLES = {
    'execution_policy.horizon': ('increase', 'decrease'),
    'execution_policy.max_wait_sessions': ('increase', 'decrease'),
    'risk_policy.single_position_risk_bp': ('decrease',),
    'risk_policy.top_n': ('decrease',),
    'risk_policy.max_weight_bp': ('decrease',),
    'llm_policy.evidence_window_days': ('increase', 'decrease'),
    'position_review.check_interval_seconds': ('increase', 'decrease'),
}

# 明确列出的**禁止**变量，用于给出清晰的拒绝理由（而不是笼统的"不在白名单"）
FORBIDDEN_VARIABLES = (
    'hard_exit.enabled', 'risk_policy.max_total_risk_bp', 'risk_policy.max_positions',
    'risk_policy.drawdown_ladder', 'llm_permissions',
)

REVIEW_DECISION_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['schema_version', 'packet_id', 'status', 'failure_patterns',
                 'proposed_change', 'expected_improvement', 'possible_regression',
                 'validation_plan', 'reason_codes', 'missing_information'],
    'properties': {
        'schema_version': {'type': 'string'},
        'packet_id': {'type': 'string'},
        'status': {'enum': ['complete', 'insufficient_information', 'failed', 'stale']},
        'failure_patterns': {
            'type': 'array',
            'items': {
                'type': 'object', 'additionalProperties': False,
                'required': ['pattern', 'sample_group', 'observed'],
                'properties': {
                    'pattern': {'type': 'string', 'minLength': 1},
                    # 支持该判断的样本组：必须是程序统计里存在的分组名
                    'sample_group': {'type': 'string', 'minLength': 1},
                    'observed': {'type': 'object'},
                },
            },
        },
        'proposed_change': {
            'type': ['object', 'null'], 'additionalProperties': False,
            'required': ['variable', 'from_value', 'to_value', 'direction'],
            'properties': {
                'variable': {'type': 'string', 'minLength': 1},
                'from_value': {},
                'to_value': {},
                'direction': {'enum': ['increase', 'decrease']},
            },
        },
        'expected_improvement': {
            'type': ['object', 'null'], 'additionalProperties': False,
            'required': ['metric', 'direction'],
            'properties': {'metric': {'type': 'string', 'minLength': 1},
                           'direction': {'enum': ['increase', 'decrease']}},
        },
        'possible_regression': {
            'type': 'array',
            'items': {'type': 'object', 'additionalProperties': False,
                      'required': ['metric', 'direction'],
                      'properties': {'metric': {'type': 'string', 'minLength': 1},
                                     'direction': {'enum': ['increase', 'decrease']}}},
        },
        'validation_plan': {
            'type': ['object', 'null'], 'additionalProperties': False,
            'required': ['window_sessions', 'min_samples', 'stop_condition'],
            'properties': {
                'window_sessions': {'type': 'integer', 'minimum': 1},
                'min_samples': {'type': 'integer', 'minimum': 1},
                'stop_condition': {'type': 'string', 'minLength': 1},
            },
        },
        'reason_codes': {'type': 'array', 'items': {'type': 'string'}},
        # 由 `normalize_review_output` 在校验**之前**回填（propose_change / no_change），
        # 故必须在 schema 里合法 —— `_decide` 是 normalize → validate，schema 又是
        # additionalProperties: False。不放进 required：模型不必自己给。
        'action': {'type': 'string'},
        'missing_information': {'type': 'array', 'items': {'type': 'string'}},
    },
}

REVIEW_SYSTEM = (
    '你是策略复盘研究员。你的输入是**程序算好的统计**（各角色的决策、应用与结果，'
    'R/L 收益与回撤，按证据来源/动作/市场状态/风险组分层的样本），不是行情判断。\n'
    '任务：从这些统计里提出**一个可检验的协议改进假设**。不是评价某笔交易对错，'
    '也不是复述指标。\n'
    '硬性要求：\n'
    '- `proposed_change.variable` 只能取给定的可变更白名单里的**一个**变量；'
    '改多个变量会让效果无法归因，一律视为无效输出。\n'
    '- 必须给出 `validation_plan`：观察窗口（会话数）、最低样本数、**停止条件**。'
    '没有停止条件的假设不算假设。\n'
    '- 必须给出 `possible_regression`：这个改动可能让哪个指标变差。只讲好处的建议是无效的。\n'
    '- 失败模式必须挂在程序给出的 `sample_group` 上，不得自行编造分组。\n'
    '- 样本不足或统计不支持任何结论时，把 `proposed_change` 置为 null 并在\n'
    '  `missing_information` 里说明缺什么 —— **这不算失败**，编一个改动才是。\n'
    '你没有修改配置、权限或阈值的权限；你的输出只是一个待人工批准的候选。'
    '输出严格 JSON，遵循 output_schema。'
)


def normalize_review_output(raw: Dict[str, Any], packet: Dict[str, Any],
                            ) -> Dict[str, Any]:
    """把「有没有提出改动」映射成统一动作词汇：propose_change / no_change。

    `no_change` 是父策略（不提任何改动），不是失败 —— 样本不足时它才是正确答案。
    """
    import copy
    out = copy.deepcopy(raw)
    out['action'] = 'propose_change' if out.get('proposed_change') else 'no_change'
    return out


def build_review_prompt(packet: Dict[str, Any]) -> str:
    return json.dumps({'decision_type': 'protocol_review', 'input': packet,
                       'output_schema': REVIEW_DECISION_SCHEMA}, ensure_ascii=False)


def validate_review_v1(raw: Dict[str, Any], packet: Dict[str, Any],
                       as_of: Optional[str] = None) -> List[str]:
    """§6.5：单一变量、可检验、且不得触碰安全开关。"""
    from jsonschema import validate
    errors: List[str] = []
    try:
        validate(raw, REVIEW_DECISION_SCHEMA)
    except Exception as exc:
        return [f'schema: {exc}']
    if raw.get('packet_id') != packet.get('packet_id'):
        errors.append('PACKET_MISMATCH')

    known_groups = {g.get('name') for g in packet.get('sample_groups') or []}
    for pattern in raw.get('failure_patterns') or []:
        group = pattern.get('sample_group')
        if known_groups and group not in known_groups:
            errors.append(f'失败模式挂在未知样本组: {group}（不得自行编造分组）')

    change = raw.get('proposed_change')
    if raw.get('status') == 'complete' and change is None:
        # 允许"没有可提的改动"，但必须显式说明缺什么 —— 与 §6.5 的 missing_information 一致
        if not raw.get('missing_information'):
            errors.append('无建议改动时必须说明 missing_information')
    if change is not None:
        variable = str(change.get('variable') or '')
        if variable in FORBIDDEN_VARIABLES:
            errors.append(f'禁止提议修改安全相关变量: {variable}')
        elif variable not in CHANGEABLE_VARIABLES:
            errors.append(f'变量不在可变更白名单: {variable}'
                          f'（允许：{sorted(CHANGEABLE_VARIABLES)}）')
        else:
            allowed = CHANGEABLE_VARIABLES[variable]
            if change.get('direction') not in allowed:
                errors.append(f'{variable} 只允许方向 {list(allowed)}，'
                              f'实际 {change.get("direction")!r}')
        if not raw.get('possible_regression'):
            errors.append('必须给出 possible_regression（只讲好处的建议无效）')
        plan = raw.get('validation_plan')
        if not plan:
            errors.append('有建议改动时必须给出 validation_plan')
        elif not str(plan.get('stop_condition') or '').strip():
            errors.append('validation_plan 必须给出停止条件')
    return errors


def protocol_candidate_from(raw: Dict[str, Any], packet: Dict[str, Any]) -> Optional[dict]:
    """把已校验的输出转成 `protocol_change_candidate` 载荷；无可提改动时返回 None。

    **这是本模块唯一的出口，且它只产出数据。** 没有任何路径从这里通向配置写入或
    权限变更：候选落成 append-only 事件，须人工批准（见 `protocol_changes`）。
    """
    if raw.get('status') != 'complete' or raw.get('proposed_change') is None:
        return None
    change = raw['proposed_change']
    return {
        'packet_id': packet.get('packet_id'),
        'base_protocol_version': packet.get('protocol_version'),
        'failure_patterns': list(raw.get('failure_patterns') or []),
        'variable': change.get('variable'),
        'from_value': change.get('from_value'),
        'to_value': change.get('to_value'),
        'direction': change.get('direction'),
        'expected_improvement': raw.get('expected_improvement'),
        'possible_regression': list(raw.get('possible_regression') or []),
        'validation_plan': raw.get('validation_plan'),
        'reason_codes': list(raw.get('reason_codes') or []),
    }
