"""入场评审（设计 §7）：角色差异面 + 对既有公开 API 的保持。

编排序列全部在 `overlay_review.OverlayReviewer`；这里只声明入场角色与编排无关的差异，
并**原样保留**此前从本模块导出的名字（`EntryReviewer` / `EntryReviewOutcome` /
`ModelAttemptResult` / `attempt_status_for` / `decision_id_for`），避免调用方被迫改动。
"""
from __future__ import annotations

from .llm_overlay import (SCHEMA_VERSION, decide_overlay, resolve_overlay,
                          validate_model_output)
from .overlay_review import (LEASE_SECONDS, MODEL_TIMEOUT_SECONDS, ModelAttemptResult,
                             OverlayReviewer, OverlaySpec, ReviewOutcome,
                             attempt_status_for, make_decision_id)

# prompt 版本：与 system prompt / 输出契约一起构成「同一个问题」的一部分
PROMPT_VERSION = 'entry-veto-v1'

# 入场角色：三态 PASS/VETO/ABSTAIN，主体是 opportunity_id。
ENTRY_SPEC = OverlaySpec(role='entry', prompt_version=PROMPT_VERSION,
                         schema_version=SCHEMA_VERSION,
                         validate=validate_model_output, decide=decide_overlay)

# 向后兼容别名：入场评审的结果类型此前叫 EntryReviewOutcome。
EntryReviewOutcome = ReviewOutcome


def decision_id_for(experiment_id, scope, opportunity_id, packet_id, model_id,
                    prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION) -> str:
    """入场的 decision_id（保留原签名与默认值：调用方按 5 个位置参数调用）。"""
    return make_decision_id(experiment_id, scope, opportunity_id, packet_id, model_id,
                            prompt_version, schema_version)


class EntryReviewer(OverlayReviewer):
    """L 侧入场评审的编排者。R 账户不经过这里（执行父策略原计划）。"""

    spec = ENTRY_SPEC

    def subject_key(self, opp) -> str:
        return opp.opportunity_id()

    def subject_identity(self, opp) -> dict:
        return {'opportunity_id': opp.opportunity_id()}


__all__ = ['EntryReviewer', 'EntryReviewOutcome', 'ModelAttemptResult', 'OverlaySpec',
           'PROMPT_VERSION', 'attempt_status_for', 'decision_id_for', 'resolve_overlay',
           'LEASE_SECONDS', 'MODEL_TIMEOUT_SECONDS']
