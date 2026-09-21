"""overlay 评审编排基类（设计 §7）：所有角色共用的唯一决策路径骨架。

状态机：PREPARED → CALL_STARTED → COMPLETED / FAILED / TIMED_OUT / UNKNOWN，
最终动作另存 FROZEN（`Application.decision_frozen`），与成交（`execution_applied`）分开。

规则（照设计 §7）：

- `decision_id` 绑定 experiment / scope / 主体 / packet_hash / 模型 / prompt 与 schema
  版本 —— 其中任一变化都是**另一个问题**，不能复用旧答案；
- 原子领取走 SQLite 事务（`BEGIN IMMEDIATE`），**网络调用在事务结束之后**；
- 已领取但无回复（进程在发送后崩溃）→ 不盲目重发；租约过期的接管者把它判为 UNKNOWN
  并按弃权冻结；
- 重启只读取已冻结动作，不能再次调用去挑一个更满意的答案；
- 迟到回复只追加审计事件，不改变已冻结动作。

**为什么要有这一层**：入场与持仓评审共用同一套租约/恢复/冻结语义，而两处逐字复制
这段序列正是这个仓库已经吃过亏的形态（`chandelier_exit_manager._DummyStrategy` 就是
同一份止损逻辑的副本）。角色差异全部收敛到 `OverlaySpec` 与 `subject_key`。
"""
from __future__ import annotations

import threading

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable

from scripts.live_trading.decision_ledger.event_store import stable_id

from .llm_overlay import OverlayDecision, _parse, model_call_expected
from .schema import Application

# 模型总超时（设计 §7 建议 60 秒，且不超过剩余决策窗口）
MODEL_TIMEOUT_SECONDS = 60
# 租约：CALL_STARTED 超过此时长仍未落结果，视为崩溃遗留（可被接管判为 UNKNOWN）
LEASE_SECONDS = 300


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


def make_decision_id(experiment_id, scope, subject_key, packet_id, model_id,
                     prompt_version, schema_version) -> str:
    """绑定「同一个问题」的全部要素。任一变化即另一个 decision。

    `subject_key` 对入场是 `opportunity_id`，对持仓是 `f'{opportunity_id}@pos:{session}'`
    —— 只要求「同一主体在同一时点是同一个字符串」。版本参数无默认值：默认值会让
    「忘记传版本」静默退化成另一个问题，角色侧各自显式提供。
    """
    return stable_id('decision', experiment_id, scope, subject_key, packet_id,
                     str(model_id), prompt_version, schema_version)


@dataclass(frozen=True)
class OverlaySpec:
    """角色的差异面：只有这几项与角色有关，其余编排完全共用。"""
    role: str                    # entry / position
    prompt_version: str
    schema_version: str
    validate: Callable           # (output, packet) -> (valid, errors)
    decide: Callable             # (packet, model, deadline, *, attempt_id) -> OverlayDecision


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
class ReviewOutcome:
    decision_id: str
    decision: OverlayDecision
    frozen: bool                       # False = 尚未有最终动作（不得据此写账目）
    note: str = ''


class OverlayReviewer:
    """L 侧 overlay 评审的编排者。R 账户不经过这里（执行父策略原计划）。"""

    spec: OverlaySpec = None

    def __init__(self, store, *, scope, model_factory, model_id='', timeout_seconds=None,
                 now=None, lease_seconds=LEASE_SECONDS, knowledge_cutoff=None,
                 debug: bool = False, model_budget_micro=None,
                 model_call_reserve_micro=None):
        self.store = store
        self.scope = scope
        self.model_factory = model_factory
        self.model_id = model_id
        self.now = now
        self.lease_seconds = lease_seconds
        # 超时不得超过剩余决策窗口，由调用方传入的 deadline 收窄
        self.timeout_seconds = timeout_seconds or MODEL_TIMEOUT_SECONDS
        # 调用预算（规划 §4.1）：真实模型有费用、数据与调度都会失败 ⇒ 启动时就设上限，
        # 用尽即弃权（零成本、不发起调用）。None = 未设上限（夹具与离线路径）。
        self.model_budget_micro = model_budget_micro
        # 每次调用的**预留**金额：金额未知的尝试与正在飞的尝试都按它折算占用。
        # 缺省 = 预算的 0.2%（够保守地覆盖单次调用量级），但**必须在 manifest 里显式给出**
        # 才谈得上可复核 —— 默认值只是让不设的实验仍能运行。
        self.model_call_reserve_micro = (
            model_call_reserve_micro
            or (int(model_budget_micro * 0.002) if model_budget_micro else None))
        self.knowledge_cutoff = knowledge_cutoff
        # 设计 §9 的 historical_debug：真实调用留痕用于提示词调试，但**不写 Application**，
        # 因而不会进入正式 R/L 表现 —— 过去的执行日不能用今天生成的模型结果补填。
        self.debug = debug

    # ---- 角色差异（子类实现）----

    def subject_key(self, subject) -> str:
        """该主体在账本里的键（`shadow_applications.opportunity_id` 列实际是通用主体键）。"""
        raise NotImplementedError

    def subject_identity(self, subject) -> dict:
        """写进尝试记录的主体标识字段（子类给角色自己的字段名，供崩溃恢复时核对）。"""
        raise NotImplementedError

    def decision_id(self, subject, packet) -> str:
        return make_decision_id(self.store.experiment_id, self.scope,
                                self.subject_key(subject), packet['packet_id'],
                                self.model_id, self.spec.prompt_version,
                                self.spec.schema_version)

    def _attempt_body(self, subject, packet) -> dict:
        """尝试记录的公共字段。调用方用字典合并追加自己的键（不能用关键字参数：
        `_decision_body` 已含 `cost_uncertain` 等，重复关键字会直接 TypeError）。"""
        return {'scope': self.scope, 'packet_id': packet['packet_id'],
                'model_id': self.model_id,
                'prompt_version': self.spec.prompt_version,
                'schema_version': self.spec.schema_version,
                **self.subject_identity(subject)}

    # ---- 单个步骤（供测试与需要分步执行的入口使用）----

    def prepare_review(self, subject, packet) -> str:
        """登记 decision（PREPARED）。幂等。"""
        decision_id = self.decision_id(subject, packet)
        self.store.prepare_job_run(decision_id, 1, self._attempt_body(subject, packet))
        return decision_id

    def claim_attempt(self, decision_id, *, body=None) -> str:
        """原子领取（设计 §7）。返回 claimed / already_started / abandoned / finalized。"""
        return self.store.claim_attempt(decision_id, now=_now(self.now).isoformat(),
                                        lease_seconds=self.lease_seconds, body=body)

    def _effective_timeout(self, deadline) -> float:
        """超时上限 = min(配置值, 距决策截止的剩余秒数)。

        注释里一直写着「超时不得超过剩余决策窗口」，但 `timeout_seconds` 此前**只被赋值、
        从未被使用** —— 一次挂死的 HTTP 调用能让整个日作业无限等待，而配置看起来是生效的。
        """
        limit = float(self.timeout_seconds)
        end = _parse(deadline)
        if end is not None:
            remaining = (end - _now(self.now)).total_seconds()
            limit = min(limit, max(1.0, remaining))
        return limit

    def call_model(self, packet, deadline, decision_id) -> ModelAttemptResult:
        """发起一次（且仅一次）模型调用。**必须在领取事务结束之后调用。**

        调用在**工作线程**里跑、主线程按 `_effective_timeout` 收口：超时就按 `TIMED_OUT`
        留痕并返回（`resolve_*` 会映射成弃权），不等待、不重试。线程是守护线程 ——
        挂死的调用不能拖住 Daily 作业。
        """
        started_at = _now(self.now).isoformat()
        box: dict = {}

        def _run():
            try:
                box['result'] = self.model_factory().call(packet, deadline)
            except BaseException as exc:   # 线程里的异常必须兜住，否则会静默丢失
                box['error'] = exc

        worker = threading.Thread(target=_run, daemon=True, name='overlay-model-call')
        worker.start()
        worker.join(self._effective_timeout(deadline))
        received_at = _now(self.now).isoformat()
        if worker.is_alive():
            return ModelAttemptResult(
                status='TIMED_OUT', model_result={'status': 'TIMED_OUT'},
                started_at=started_at, received_at=received_at,
                request_id=stable_id('llm_request', decision_id, started_at),
                validation_errors=('NO_OUTPUT',))
        if 'error' in box:
            raise box['error']
        model_result = box.get('result') or {}
        received_at = _now(self.now).isoformat()
        output = model_result.get('output')
        if output is None:
            errors = ('NO_OUTPUT',)
        else:
            _, errors = self.spec.validate(output, packet)
            errors = tuple(errors)
        return ModelAttemptResult(
            status=str(model_result.get('status') or 'FAILED'),
            model_result=model_result, started_at=started_at, received_at=received_at,
            request_id=stable_id('llm_request', decision_id, started_at),
            validation_errors=errors,
            raw_output='' if output is None else json.dumps(output, ensure_ascii=False,
                                                            sort_keys=True))

    def finalize_action(self, subject, packet, decision, decision_id, result=None, *,
                        frozen=True, note='') -> None:
        """冻结最终动作（设计 §7）。冻结 ≠ 成交，`execution_applied` 留待结算。"""
        self.store.put_application(Application(
            scope=self.scope, opportunity_id=self.subject_key(subject),
            action=decision.action, reason_code=decision.reason_code,
            decision_id=decision_id, as_of=packet['as_of'], decision_frozen=frozen,
            execution_applied=False, model_cost=decision.model_cost,
            raw_action=decision.raw_action,
            late_response_observed=decision.late_response_observed,
            cost_uncertain=decision.cost_uncertain, attempt_id=decision_id))

    # ---- 完整编排 ----

    def review(self, subject, packet, deadline, *, force_recall=False) -> ReviewOutcome:
        """完成一次评审：复用已冻结 → 领取 → 调用 → 冻结。"""
        decision_id = self.decision_id(subject, packet)
        key = self.subject_key(subject)
        if self.debug:
            return self._debug_review(subject, packet, deadline, decision_id)
        frozen_record = self.store.application(self.scope, key)
        if frozen_record and frozen_record.get('decision_id'):
            if frozen_record['decision_id'] != decision_id:
                # packet 变了 ⇒ 那是**另一个问题**的答案。复用等于答非所问，重算等于用
                # 今天的信息改写当时的决策依据 —— 两者都不行，直接报错。
                raise ValueError(f'PACKET_CHANGED_ON_RERUN:{key}')
            if not force_recall:
                return ReviewOutcome(decision_id, _from_application(frozen_record),
                                     True, 'reused_frozen')

        self.prepare_review(subject, packet)
        claim = self.claim_attempt(decision_id, body=self._attempt_body(subject, packet))

        if claim == 'finalized':
            record = self.store.application(self.scope, key)
            if record is not None:
                return ReviewOutcome(decision_id, _from_application(record), True,
                                     'reused_terminal')
            # 终态但动作未落库（调用完成后崩溃）：从尝试记录**恢复**，不重调
            attempt = self.store.job_run(decision_id) or {}
            d = _decision_from_attempt(attempt, decision_id)
            self.finalize_action(subject, packet, d, decision_id)
            return ReviewOutcome(decision_id, d, True,
                                 'recovered_from_attempt' if attempt.get('action')
                                 else 'terminal_without_action')
        if claim == 'already_started':
            # 另一个 worker 租约有效、正在调用：不重复调用，也不替它冻结
            return ReviewOutcome(
                decision_id,
                OverlayDecision('ABSTAIN', 'ATTEMPT_IN_FLIGHT', 0, '', False, True,
                                decision_id),
                False, 'attempt_in_flight')
        if claim == 'abandoned':
            # 上次领取的租约已过期且无结果（进程在发送后崩溃）：**不盲目重发**。
            # 钱可能已经花了且金额不可知 —— 判为 UNKNOWN，按弃权采用父策略，
            # 该次尝试挂账待补记（设计 §7）。
            self.store.put_job_run(decision_id, 1, 'UNKNOWN', {
                **self._attempt_body(subject, packet),
                'detected_at': _now(self.now).isoformat(),
                'reason': 'LEASE_EXPIRED_WITHOUT_RESULT'})
            d = OverlayDecision('ABSTAIN', 'RECALL_ABANDONED', 0, '', False, True,
                                decision_id)
            self.finalize_action(subject, packet, d, decision_id)
            return ReviewOutcome(decision_id, d, True, 'abandoned')

        if self._budget_exhausted():
            # 预算用尽：零成本、不调用、不重试，按弃权采用父策略。落 Application 是必须的
            # —— 否则结算会把「没评审过」当成缺口（`_ensure_position_reviewed`）。
            d = OverlayDecision('ABSTAIN', 'MODEL_BUDGET_EXHAUSTED', 0, '', False, False,
                                decision_id)
            self.store.put_job_run(decision_id, 1, 'COMPLETED', {
                **self._attempt_body(subject, packet), 'gated': True,
                'budget_exhausted': True, **self.budget_usage(),
                'completed_at': _now(self.now).isoformat(), **_decision_body(d)})
            self.finalize_action(subject, packet, d, decision_id)
            return ReviewOutcome(decision_id, d, True, 'budget_exhausted')

        if not model_call_expected(packet):
            d = self.spec.decide(packet, None, deadline, attempt_id=decision_id)
            self.store.put_job_run(decision_id, 1, 'COMPLETED', {
                **self._attempt_body(subject, packet), 'gated': True,
                'completed_at': _now(self.now).isoformat(), **_decision_body(d)})
            self.finalize_action(subject, packet, d, decision_id)
            return ReviewOutcome(decision_id, d, True, 'gated')

        result = self.call_model(packet, deadline, decision_id)
        # 绑定 decision_id：成本未知时要靠它挂账待补记
        d = replace(self.spec.decide(packet, _RecordingModel(result), deadline,
                                     attempt_id=decision_id))
        self.store.put_job_run(decision_id, 1, attempt_status_for(result.status), {
            **self._attempt_body(subject, packet),
            'model_status': result.status, 'request_id': result.request_id,
            'started_at': result.started_at, 'received_at': result.received_at,
            'raw_output': result.raw_output,
            'validation_errors': list(result.validation_errors),
            'cost_micro': result.model_result.get('cost_micro'),
            'cost_uncertain': bool(result.model_result.get('cost_uncertain')),
            **_decision_body(d)})
        self.finalize_action(subject, packet, d, decision_id, result)
        return ReviewOutcome(decision_id, d, True, result.status.lower())

    def budget_usage(self) -> dict:
        """预算占用 = **已结算** + (**金额未知** + **正在飞**) × 每次调用预留。

        只看已结算是不够的：未知金额在记账上就是 0、正在飞的调用还没落账，两者叠加时
        N 个持仓同时评审会各自以为「剩下的钱够」，总额就穿透了上限。
        预留是**保守**估计（宁可早停不可穿透），实际花费以 `settled` 为准。
        """
        settled, uncertain = self.store.model_cost_so_far(self.scope)
        in_flight = self.store.in_flight_attempts(self.scope)
        reserve = self.model_call_reserve_micro or 0
        return {'settled_micro': settled, 'unsettled_count': uncertain,
                'in_flight_count': in_flight, 'reserve_micro': reserve,
                'committed_micro': settled + (uncertain + in_flight) * reserve,
                'budget_micro': self.model_budget_micro}

    def _budget_exhausted(self) -> bool:
        if not self.model_budget_micro:
            return False
        return self.budget_usage()['committed_micro'] >= self.model_budget_micro

    def _debug_review(self, subject, packet, deadline, decision_id) -> ReviewOutcome:
        """设计 §9 的 `historical_debug` 分支：真实调用留痕，但**不冻结动作**。

        「不写 Application」不是省略，而是这个模式的全部意义 —— 调试调用一旦落成账目，
        过去的执行日就相当于用了今天生成的模型结果补填前瞻记录。
        """
        body = {**self._attempt_body(subject, packet), 'historical_debug': True}
        self.prepare_review(subject, packet)
        claim = self.claim_attempt(decision_id, body=body)
        if claim == 'already_started':
            return ReviewOutcome(
                decision_id, OverlayDecision('ABSTAIN', 'ATTEMPT_IN_FLIGHT', 0, '', False,
                                             True, decision_id), False, 'attempt_in_flight')
        if claim in ('finalized', 'abandoned'):
            if claim == 'abandoned':
                self.store.put_job_run(decision_id, 1, 'UNKNOWN', {
                    **body, 'detected_at': _now(self.now).isoformat(),
                    'reason': 'LEASE_EXPIRED_WITHOUT_RESULT'})
            attempt = self.store.job_run(decision_id) or {}
            return ReviewOutcome(decision_id, _decision_from_attempt(attempt, decision_id),
                                 False, 'reused_debug_attempt')
        result = self.call_model(packet, deadline, decision_id)
        d = replace(self.spec.decide(packet, _RecordingModel(result), deadline,
                                     attempt_id=decision_id))
        self.store.put_job_run(decision_id, 1, attempt_status_for(result.status), {
            **body, 'model_status': result.status, 'request_id': result.request_id,
            'started_at': result.started_at, 'received_at': result.received_at,
            'raw_output': result.raw_output,
            'validation_errors': list(result.validation_errors),
            'cost_micro': result.model_result.get('cost_micro'),
            'cost_uncertain': bool(result.model_result.get('cost_uncertain')),
            **_decision_body(d)})
        return ReviewOutcome(decision_id, d, False, 'historical_debug')

    def load_applications(self, scope=None) -> list:
        """已冻结动作（设计 §7 的 load_applications）。"""
        return [a for a in self.store.applications(scope or self.scope)
                if a.get('decision_frozen') and a.get('decision_id')]


class _RecordingModel:
    """把已经调用并记好的尝试结果交回给 spec.decide（避免二次调用）。"""

    def __init__(self, result: ModelAttemptResult):
        self._result = result

    def call(self, packet, deadline) -> dict:
        return self._result.model_result


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
