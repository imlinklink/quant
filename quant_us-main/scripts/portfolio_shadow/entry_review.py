"""入场评审编排（设计 §7）：所有 CLI 入口共用的唯一决策路径。

状态机：PREPARED → CALL_STARTED → COMPLETED / FAILED / TIMED_OUT / UNKNOWN，
最终动作另存 FROZEN（`Application.decision_frozen`），与成交（`execution_applied`）分开。

规则（照设计 §7）：

- `decision_id` 绑定 experiment / scope / opportunity / packet_hash / 模型 / prompt 与
  schema 版本 —— 其中任一变化都是**另一个问题**，不能复用旧答案；
- 原子领取走 SQLite 事务（`BEGIN IMMEDIATE`），**网络调用在事务结束之后**；
- 已领取但无回复（进程在发送后崩溃）→ 不盲目重发；租约过期的接管者把它判为 UNKNOWN
  并按 ABSTAIN 冻结；
- 重启只读取已冻结动作，不能再次调用去挑一个更满意的答案；
- 迟到回复只追加审计事件，不改变已冻结动作。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from scripts.live_trading.decision_ledger.event_store import stable_id

from .llm_overlay import (SCHEMA_VERSION, OverlayDecision, decide_overlay,
                          model_call_expected, resolve_overlay, validate_model_output)
from .schema import Application

# 模型总超时（设计 §7 建议 60 秒，且不超过剩余决策窗口）
MODEL_TIMEOUT_SECONDS = 60
# 租约：CALL_STARTED 超过此时长仍未落结果，视为崩溃遗留（可被接管判为 UNKNOWN）
LEASE_SECONDS = 300
# prompt 版本：与 system prompt / 输出契约一起构成「同一个问题」的一部分
PROMPT_VERSION = 'entry-veto-v1'


def _now(clock=None) -> datetime:
    return (clock or (lambda: datetime.now(timezone.utc)))()


# 模型结果状态（llm_overlay 词汇）→ 尝试状态机取值（设计 §7 词汇）。
# 两套词汇必须显式对齐：'OK' 不是终态值，若不映射，一次**成功**的尝试会被当成
# 从未终结，重跑时按「崩溃遗留」处理 —— 覆盖掉已付费的有效结果。
_ATTEMPT_STATUS_MAP = {'OK': 'COMPLETED', 'FAILED': 'FAILED', 'TIMED_OUT': 'TIMED_OUT'}


def attempt_status_for(model_status) -> str:
    """把模型结果状态映射成尝试状态。未知的程序侧拒发（HISTORICAL_AS_OF 等）算失败终态，
    绝不能落在一个非终态值上。"""
    return _ATTEMPT_STATUS_MAP.get(str(model_status), 'FAILED')


def decision_id_for(experiment_id, scope, opportunity_id, packet_id, model_id,
                    prompt_version=PROMPT_VERSION, schema_version=SCHEMA_VERSION) -> str:
    """绑定「同一个问题」的全部要素。任一变化即另一个 decision。"""
    return stable_id('decision', experiment_id, scope, opportunity_id, packet_id,
                     str(model_id), prompt_version, schema_version)


@dataclass(frozen=True)
class ModelAttemptResult:
    """一次模型尝试的完整留痕（设计 §7：不能只保存 packet_hash 及最终 action）。"""
    status: str                        # COMPLETED / FAILED / TIMED_OUT / UNKNOWN
    model_result: dict = field(default_factory=dict)
    started_at: str = ''
    received_at: str = ''
    request_id: str = ''
    validation_errors: tuple = ()
    raw_output: str = ''


@dataclass(frozen=True)
class EntryReviewOutcome:
    decision_id: str
    decision: OverlayDecision
    frozen: bool                       # False = 尚未有最终动作（不得据此写账目）
    note: str = ''


class EntryReviewer:
    """L 侧入场评审的编排者。R 账户不经过这里（执行父策略原计划）。"""

    def __init__(self, store, *, scope, model_factory, model_id='', timeout_seconds=None,
                 now=None, lease_seconds=LEASE_SECONDS, knowledge_cutoff=None,
                 debug: bool = False):
        self.store = store
        self.scope = scope
        self.model_factory = model_factory
        self.model_id = model_id
        self.now = now
        self.lease_seconds = lease_seconds
        # 超时不得超过剩余决策窗口，由调用方传入的 deadline 收窄
        self.timeout_seconds = timeout_seconds or MODEL_TIMEOUT_SECONDS
        self.knowledge_cutoff = knowledge_cutoff
        # 设计 §9 的 historical_debug：真实调用留痕用于提示词调试，但**不写 Application**，
        # 因而不会进入正式 R/L 表现 —— 过去的执行日不能用今天生成的模型结果补填。
        self.debug = debug

    # ---- 单个步骤（供测试与需要分步执行的入口使用）----

    def prepare_review(self, opp, packet) -> str:
        """登记 decision（PREPARED）。幂等。"""
        decision_id = decision_id_for(self.store.experiment_id, self.scope,
                                      opp.opportunity_id(), packet['packet_id'],
                                      self.model_id)
        self.store.prepare_job_run(decision_id, 1, {
            'scope': self.scope, 'opportunity_id': opp.opportunity_id(),
            'packet_id': packet['packet_id'], 'model_id': self.model_id,
            'prompt_version': PROMPT_VERSION, 'schema_version': SCHEMA_VERSION})
        return decision_id

    def claim_attempt(self, decision_id, *, body=None) -> str:
        """原子领取（设计 §7）。返回 claimed / already_started / finalized。"""
        lease_until = (_now(self.now) + timedelta(seconds=self.lease_seconds)).isoformat()
        return self.store.claim_attempt(decision_id, lease_until=lease_until, body=body)

    def call_model(self, packet, deadline, decision_id) -> ModelAttemptResult:
        """发起一次（且仅一次）模型调用。**必须在领取事务结束之后调用。**"""
        started_at = _now(self.now).isoformat()
        model_result = self.model_factory().call(packet, deadline)
        received_at = _now(self.now).isoformat()
        output = model_result.get('output')
        if output is None:
            errors = ('NO_OUTPUT',)
        else:
            _, errors = validate_model_output(output, packet)
            errors = tuple(errors)
        return ModelAttemptResult(
            status=str(model_result.get('status') or 'FAILED'),
            model_result=model_result, started_at=started_at, received_at=received_at,
            request_id=stable_id('llm_request', decision_id, started_at),
            validation_errors=errors,
            raw_output='' if output is None else json.dumps(output, ensure_ascii=False,
                                                            sort_keys=True))

    def finalize_action(self, opp, packet, decision, decision_id, result=None, *,
                        frozen=True, note='') -> None:
        """冻结最终动作（设计 §7）。冻结 ≠ 成交，`execution_applied` 留待结算。"""
        self.store.put_application(Application(
            scope=self.scope, opportunity_id=opp.opportunity_id(), action=decision.action,
            reason_code=decision.reason_code, decision_id=decision_id,
            as_of=packet['as_of'], decision_frozen=frozen,
            execution_applied=False, model_cost=decision.model_cost,
            raw_action=decision.raw_action,
            late_response_observed=decision.late_response_observed,
            cost_uncertain=decision.cost_uncertain, attempt_id=decision_id))

    # ---- 完整编排 ----

    def review(self, opp, packet, deadline, *, force_recall=False) -> EntryReviewOutcome:
        """完成一次入场评审：复用已冻结 → 领取 → 调用 → 冻结。"""
        decision_id = decision_id_for(self.store.experiment_id, self.scope,
                                      opp.opportunity_id(), packet['packet_id'],
                                      self.model_id)
        if self.debug:
            return self._debug_review(opp, packet, deadline, decision_id)
        frozen_record = self.store.application(self.scope, opp.opportunity_id())
        if frozen_record and frozen_record.get('decision_id'):
            if frozen_record['decision_id'] != decision_id:
                # packet 变了 ⇒ 那是**另一个问题**的答案。复用等于答非所问，重算等于用
                # 今天的信息改写当时的决策依据 —— 两者都不行，直接报错。
                raise ValueError(f'PACKET_CHANGED_ON_RERUN:{opp.opportunity_id()}')
            if not force_recall:
                return EntryReviewOutcome(decision_id, _from_application(frozen_record),
                                          True, 'reused_frozen')

        self.prepare_review(opp, packet)
        claim = self.claim_attempt(decision_id, body={
            'scope': self.scope, 'opportunity_id': opp.opportunity_id(),
            'packet_id': packet['packet_id'], 'model_id': self.model_id,
            'prompt_version': PROMPT_VERSION, 'schema_version': SCHEMA_VERSION})

        if claim == 'finalized':
            record = self.store.application(self.scope, opp.opportunity_id())
            if record is not None:
                return EntryReviewOutcome(decision_id, _from_application(record), True,
                                          'reused_terminal')
            # 终态但动作未落库（调用完成后崩溃）：从尝试记录**恢复**，不重调
            attempt = self.store.job_run(decision_id) or {}
            d = _decision_from_attempt(attempt, decision_id)
            self.finalize_action(opp, packet, d, decision_id)
            return EntryReviewOutcome(decision_id, d, True,
                                      'recovered_from_attempt' if attempt.get('action')
                                      else 'terminal_without_action')
        if claim == 'already_started':
            # 另一个 worker 租约有效、正在调用：不重复调用，也不替它冻结
            return EntryReviewOutcome(
                decision_id,
                OverlayDecision('ABSTAIN', 'ATTEMPT_IN_FLIGHT', 0, '', False, True,
                                decision_id),
                False, 'attempt_in_flight')
        if claim == 'abandoned':
            # 上次领取的租约已过期且无结果（进程在发送后崩溃）：**不盲目重发**。
            # 钱可能已经花了且金额不可知 —— 判为 UNKNOWN，按 ABSTAIN 采用父策略，
            # 该次尝试挂账待补记（设计 §7）。
            self.store.put_job_run(decision_id, 1, 'UNKNOWN', {
                'scope': self.scope, 'packet_id': packet['packet_id'],
                'model_id': self.model_id, 'prompt_version': PROMPT_VERSION,
                'schema_version': SCHEMA_VERSION, 'detected_at': _now(self.now).isoformat(),
                'reason': 'LEASE_EXPIRED_WITHOUT_RESULT'})
            d = OverlayDecision('ABSTAIN', 'RECALL_ABANDONED', 0, '', False, True,
                                decision_id)
            self.finalize_action(opp, packet, d, decision_id)
            return EntryReviewOutcome(decision_id, d, True, 'abandoned')

        if not model_call_expected(packet):
            d = decide_overlay(packet, None, deadline, attempt_id=decision_id)
            self.store.put_job_run(decision_id, 1, 'COMPLETED', {
                'scope': self.scope, 'packet_id': packet['packet_id'],
                'model_id': self.model_id, 'prompt_version': PROMPT_VERSION,
                'schema_version': SCHEMA_VERSION, 'gated': True,
                'completed_at': _now(self.now).isoformat(),
                **_decision_body(d)})
            self.finalize_action(opp, packet, d, decision_id)
            return EntryReviewOutcome(decision_id, d, True, 'gated')

        result = self.call_model(packet, deadline, decision_id)
        # 绑定 decision_id：成本未知时要靠它挂账待补记
        d = replace(resolve_overlay(packet, result.model_result, deadline),
                    attempt_id=decision_id)
        self.store.put_job_run(decision_id, 1, attempt_status_for(result.status), {
            'scope': self.scope, 'packet_id': packet['packet_id'],
            'model_id': self.model_id, 'prompt_version': PROMPT_VERSION,
            'schema_version': SCHEMA_VERSION, 'model_status': result.status,
            'request_id': result.request_id,
            'started_at': result.started_at, 'received_at': result.received_at,
            'raw_output': result.raw_output,
            'validation_errors': list(result.validation_errors),
            'cost_micro': result.model_result.get('cost_micro'),
            'cost_uncertain': bool(result.model_result.get('cost_uncertain')),
            **_decision_body(d)})
        self.finalize_action(opp, packet, d, decision_id, result)
        return EntryReviewOutcome(decision_id, d, True, result.status.lower())

    def _debug_review(self, opp, packet, deadline, decision_id) -> EntryReviewOutcome:
        """设计 §9 的 `historical_debug` 分支：真实调用留痕，但**不冻结动作**。

        「不写 Application」不是省略，而是这个模式的全部意义 —— 调试调用一旦落成账目，
        过去的执行日就相当于用了今天生成的模型结果补填前瞻记录。
        """
        body = {'scope': self.scope, 'opportunity_id': opp.opportunity_id(),
                'packet_id': packet['packet_id'], 'model_id': self.model_id,
                'prompt_version': PROMPT_VERSION, 'schema_version': SCHEMA_VERSION,
                'historical_debug': True}
        self.prepare_review(opp, packet)
        claim = self.claim_attempt(decision_id, body=body)
        if claim == 'already_started':
            return EntryReviewOutcome(
                decision_id, OverlayDecision('ABSTAIN', 'ATTEMPT_IN_FLIGHT', 0, '', False,
                                             True, decision_id), False, 'attempt_in_flight')
        if claim in ('finalized', 'abandoned'):
            if claim == 'abandoned':
                self.store.put_job_run(decision_id, 1, 'UNKNOWN', {
                    **body, 'detected_at': _now(self.now).isoformat(),
                    'reason': 'LEASE_EXPIRED_WITHOUT_RESULT'})
            attempt = self.store.job_run(decision_id) or {}
            return EntryReviewOutcome(decision_id, _decision_from_attempt(attempt, decision_id),
                                      False, 'reused_debug_attempt')
        result = self.call_model(packet, deadline, decision_id)
        d = replace(resolve_overlay(packet, result.model_result, deadline),
                    attempt_id=decision_id)
        self.store.put_job_run(decision_id, 1, attempt_status_for(result.status), {
            **body, 'model_status': result.status,
            'request_id': result.request_id, 'started_at': result.started_at,
            'received_at': result.received_at, 'raw_output': result.raw_output,
            'validation_errors': list(result.validation_errors),
            'cost_micro': result.model_result.get('cost_micro'),
            'cost_uncertain': bool(result.model_result.get('cost_uncertain')),
            **_decision_body(d)})
        return EntryReviewOutcome(decision_id, d, False, 'historical_debug')

    def load_applications(self, scope=None) -> list:
        """已冻结动作（设计 §7 的 load_applications）。"""
        return [a for a in self.store.applications(scope or self.scope)
                if a.get('decision_frozen') and a.get('decision_id')]


def _decision_body(d: OverlayDecision) -> dict:
    """把最终动作写进尝试记录：这样「调用完成但 Application 未落库」也能恢复而不重调。"""
    return {'action': d.action, 'reason_code': d.reason_code, 'model_cost': d.model_cost,
            'cost_uncertain': d.cost_uncertain, 'raw_action': d.raw_action,
            'late_response_observed': d.late_response_observed}


def _decision_from_attempt(attempt: dict, decision_id: str) -> OverlayDecision:
    """从尝试记录还原决定（终态但动作未落库时用）。"""
    if attempt.get('action'):
        return OverlayDecision(
            attempt['action'], attempt.get('reason_code', ''),
            attempt.get('model_cost', 0), attempt.get('raw_action', ''),
            attempt.get('late_response_observed', False),
            attempt.get('cost_uncertain', False), decision_id)
    return OverlayDecision('ABSTAIN', 'ATTEMPT_TERMINAL_WITHOUT_ACTION', 0, '', False, True,
                           decision_id)


def _from_application(record: dict) -> OverlayDecision:
    return OverlayDecision(
        record['action'], record['reason_code'], record.get('model_cost', 0),
        record.get('raw_action', ''), record.get('late_response_observed', False),
        record.get('cost_uncertain', False), record.get('decision_id', ''))
