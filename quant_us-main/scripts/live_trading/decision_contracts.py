"""LLM 决策角色的程序边界；不包含模型调用或交易执行。"""
from dataclasses import dataclass
from typing import FrozenSet


@dataclass(frozen=True)
class DecisionRoleContract:
    role: str
    subject_type: str
    allowed_actions: FrozenSet[str]
    fallback_action: str
    outcome_horizons: tuple
    may_affect_shadow_path: bool


ROLE_CONTRACTS = {
    'selection': DecisionRoleContract(
        role='selection', subject_type='research_batch',
        allowed_actions=frozenset({'llm_ranking'}), fallback_action='rule_ranking',
        outcome_horizons=(1, 3, 5, 10, 20), may_affect_shadow_path=True),
    'entry': DecisionRoleContract(
        role='entry', subject_type='signal',
        allowed_actions=frozenset({'execute_now', 'defer', 'reject'}),
        fallback_action='rule_baseline', outcome_horizons=(1, 3, 5, 10, 20),
        may_affect_shadow_path=True),
    'position': DecisionRoleContract(
        role='position', subject_type='trade',
        allowed_actions=frozenset({
            'hold', 'tighten_protection', 'reduce', 'exit', 'post_exit_review'}),
        fallback_action='hold', outcome_horizons=(1, 3, 5, 10, 20),
        may_affect_shadow_path=True),
    # Portfolio 只在**程序生成的合法模板**里选一个（§6.3），因此 fallback 是规则分配。
    # 它可以在 L 路上改变容量分配 —— 但 §8 规定初始等级为 observe，故实际不生效，
    # 直到按 §12 晋级。`may_affect_shadow_path` 描述的是**该角色被授权后**的能力边界。
    'portfolio': DecisionRoleContract(
        role='portfolio', subject_type='portfolio',
        allowed_actions=frozenset({
            'keep_rule_allocation', 'select_ranked_subset',
            'reduce_same_group_concentration', 'hold_cash_buffer'}),
        fallback_action='keep_rule_allocation', outcome_horizons=(1, 3, 5, 10, 20),
        may_affect_shadow_path=True),
    # Review **永不改变任何路径**（§6.5）：它的出口只有"提出协议候选/不提"，
    # 候选须人工批准才可能成为新版本。故 `may_affect_shadow_path=False` ——
    # 这条不是倾向性描述，而是该角色的定义性约束，由测试钉死。
    'review': DecisionRoleContract(
        role='review', subject_type='evaluation_window',
        allowed_actions=frozenset({'propose_change', 'no_change'}),
        fallback_action='no_change', outcome_horizons=(),
        may_affect_shadow_path=False),
}


def role_contract(role: str) -> DecisionRoleContract:
    try:
        return ROLE_CONTRACTS[role]
    except KeyError as exc:
        raise ValueError(f'非法 role: {role}') from exc


def validate_role_action(role: str, action: str) -> None:
    contract = role_contract(role)
    if action not in contract.allowed_actions:
        raise ValueError(f'{role} 不允许动作: {action}')
