"""词表：自带**超集**，不向任何 checkout 借。

**为什么不能 import `scripts.portfolio_shadow.llm_overlay`**：那份词表在运行版本之间
是会变的。实测 pin `cf2440d` 的 `ABSTAIN_REASONS` **缺 `MODEL_BUDGET_EXHAUSTED`**，
而 dev 有（本包要读的 L1 账本恰恰由 dev 血统的 checkout 写入）。若借 pin 的词表来解释
L1 的账本，`MODEL_BUDGET_EXHAUSTED` 会被判成「不是程序侧弃权」⇒ 计入「模型知情」⇒
**预算耗尽被静默算成模型参与过判断**（需求 §5.4 点名要分开的八类弃权之一）。

所以这里放一份**并集**，并带版本号；`tests/unit/ops/test_analytics_vocabulary.py` 钉死
「它是当前 checkout 词表的超集，且在交集上逐码分类一致」。未知码**不猜**：落成
`UNCLASSIFIED` 并写进 `missing_reasons`，页面上显示「未采集（词表未覆盖）」。
"""
from __future__ import annotations

VOCABULARY_VERSION = 1

# ---- 入场/持仓动作与弃权原因（两份 checkout 的并集）----------------------------
FIXTURE_MODEL_IDS = ('', 'fixture', None)

POSITION_EXIT = 'POSITION_EXIT'
POSITION_HOLD = 'POSITION_HOLD'
POSITION_TIGHTEN_NOT_APPLIED = 'POSITION_TIGHTEN_NOT_APPLIED'
REDUCE_TIERS = (25, 50)
PATH_CHANGING_ACTIONS = (POSITION_EXIT,) + tuple(f'POSITION_REDUCE_{t}' for t in REDUCE_TIERS)
POSITION_ABSTAIN = 'POSITION_ABSTAIN'

DATA_BLOCK_REASONS = ('DATA_BLOCKED_QUOTE',)
QUALITY_ABSTAIN_REASONS = ('INSUFFICIENT_EVIDENCE',)
FAILURE_ABSTAIN_REASONS = (
    'TIMED_OUT', 'FAILED', 'INVALID_OUTPUT', 'LATE_RESPONSE', 'RECALL_ABANDONED',
    'MODEL_KNOWLEDGE_CUTOFF', 'HISTORICAL_AS_OF', 'DECISION_DEADLINE_MISSED',
    'MODEL_BUDGET_EXHAUSTED',          # ← pin 缺这一条；见模块 docstring
)
ABSTAIN_REASONS = tuple(DATA_BLOCK_REASONS) + tuple(QUALITY_ABSTAIN_REASONS) + \
    tuple(FAILURE_ABSTAIN_REASONS)
# 未发起任何调用、因而确实零成本的原因（区别于「调用过但成本未知」）
NO_CALL_REASONS = ('HISTORICAL_AS_OF', 'MODEL_KNOWLEDGE_CUTOFF', 'DATA_BLOCKED_QUOTE',
                   'INSUFFICIENT_EVIDENCE', 'DECISION_DEADLINE_MISSED',
                   'MODEL_BUDGET_EXHAUSTED')
ACTIONS = ('PASS', 'VETO', 'ABSTAIN')
VETO_REASON_CODES = ('MATERIAL_THESIS_CONTRADICTION', 'MATERIAL_COMPANY_EVENT_RISK')
# 账户级动作与候选终态：照 `schema.py` 的枚举（有超集/一致性测试钉死，不另立一份语义）
CANDIDATE_TERMINAL = ('RULE_REJECTED', 'DATA_BLOCKED', 'WAITING', 'EXPIRED', 'READY')
ACCOUNT_ACTIONS = ('RISK_REJECTED', 'VETOED', 'INTENT_CREATED', 'MISSED_EXECUTION')
SHADOW_TERMINALS = CANDIDATE_TERMINAL + ('VETOED', 'MISSED_EXECUTION', 'EXECUTED')
ATTEMPT_STATUSES = ('PREPARED', 'CALL_STARTED', 'COMPLETED', 'FAILED', 'TIMED_OUT', 'UNKNOWN')
STEP_TYPES = ('fill', 'split', 'dividend_record', 'dividend_pay', 'settle', 'nav',
              'model_cost', 'model_cost_settlement', 'hold', 'missed', 'protection_state',
              'stop_update_applied')
# 信息性原因码：**不属于**弃权分类，但账本里会出现。漏掉它们只会让页面多一条
# 「词表未覆盖」的噪声（那是如实披露，不是错误），但既然已知就该收进来。
INFORMATIONAL_REASONS = ('PARENT_STRATEGY', 'NO_ACTION')


class PinnedVocabulary:
    """`report.py` 的算式需要的那一小块词表接口（形状与 `PackageVocabulary` 相同）。"""

    FIXTURE_MODEL_IDS = FIXTURE_MODEL_IDS
    PATH_CHANGING_ACTIONS = PATH_CHANGING_ACTIONS
    POSITION_HOLD = POSITION_HOLD
    POSITION_TIGHTEN_NOT_APPLIED = POSITION_TIGHTEN_NOT_APPLIED

    @staticmethod
    def is_program_abstain(reason_code) -> bool:
        return reason_code in ABSTAIN_REASONS

    @staticmethod
    def program_abstain_class(reason_code):
        if reason_code in DATA_BLOCK_REASONS:
            return 'data_blocked'
        if reason_code in QUALITY_ABSTAIN_REASONS:
            return 'quality_abstain'
        if reason_code in FAILURE_ABSTAIN_REASONS:
            return 'failure'
        return None

    @staticmethod
    def citation_subjects(evidence_ids, events, *, subject_id: str = '') -> dict:
        """被引用证据的归属构成（同证券 / 各市场级 / 未知）。与包内实现逐行同义。"""
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


PINNED_VOCABULARY = PinnedVocabulary()


def classify_unknown(codes) -> list:
    """账本里出现过、而词表没覆盖的码 —— **不静默归桶**，由调用方写进 `missing_reasons`。"""
    known = set(ABSTAIN_REASONS) | set(ACTIONS) | set(VETO_REASON_CODES) | \
        set(SHADOW_TERMINALS) | set(ATTEMPT_STATUSES) | set(STEP_TYPES) | \
        set(ACCOUNT_ACTIONS) | set(INFORMATIONAL_REASONS) | \
        set(PATH_CHANGING_ACTIONS) | {POSITION_HOLD, POSITION_TIGHTEN_NOT_APPLIED,
                                      POSITION_ABSTAIN}
    return sorted({c for c in codes if c and c not in known})
