"""DecisionEngine（技术设计 §12）：selection / entry / position 三类决策统一编排。

统一模板方法 `_decide`：
  save_input_snapshot → finalize decision_id（绑定冻结输入）→ 幂等检查 →
  record_requested → call_model → save_raw_attempt → parse → validate → permission → persist。

模型调用只发生在 `_decide`；页面、监控器和执行器不直接调用 LLM。
输入先于模型调用持久化；decision_id 覆盖 input_snapshot_id；任何校验失败 fail-closed。
关键审计事件（decision_requested/validated/permission_applied/effective_action）写失败即抛异常。
"""
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from mutifactor.llm.validators.action import (
    applicable_permissions, specific_permission,
)
from scripts.live_trading.decision_ledger.decision_run_store import (
    DecisionRunStore, finalize_decision_id,
)
from scripts.live_trading.decision_ledger.event_store import stable_id, utc
from scripts.live_trading.decision_ledger.permission_guard import PermissionGuard
from scripts.live_trading.decision_contracts import ROLE_CONTRACTS, role_contract, validate_role_action

logger = logging.getLogger(__name__)

VALID_ROLES = tuple(ROLE_CONTRACTS)


@dataclass(frozen=True)
class DecisionResult:
    decision_id: str
    role: str
    status: str
    validated_output: Optional[Dict[str, Any]]
    effective_action: Optional[str]
    permission_level: str
    validation_errors: Tuple[str, ...]
    input_snapshot_id: str
    attempt_id: Optional[str]
    model_action: Optional[str] = None
    shadow_difference: Optional[str] = None


def _selection_contract():
    from mutifactor.llm.contracts.selection_v4 import (
        SELECTION_V4_PROMPT_VERSION, SELECTION_V4_SCHEMA, SELECTION_V4_SCHEMA_VERSION,
        SELECTION_V4_SYSTEM, build_selection_prompt, normalize_selection_output,
        validate_selection_packet,
    )
    return {
        'schema_version': SELECTION_V4_SCHEMA_VERSION,
        'prompt_version': SELECTION_V4_PROMPT_VERSION,
        'output_schema': SELECTION_V4_SCHEMA,
        'system_prompt': SELECTION_V4_SYSTEM,
        'build_prompt': build_selection_prompt,
        'normalize': normalize_selection_output,
        'validate': validate_selection_packet,
    }


def _entry_contract():
    from mutifactor.llm.contracts.entry_v2 import (
        ENTRY_DECISION_SCHEMA, ENTRY_SYSTEM, ENTRY_V2_PROMPT_VERSION,
        ENTRY_V2_SCHEMA_VERSION, build_entry_prompt, normalize_entry_output,
        validate_entry_v2,
    )
    return {
        'schema_version': ENTRY_V2_SCHEMA_VERSION,
        'prompt_version': ENTRY_V2_PROMPT_VERSION,
        'output_schema': ENTRY_DECISION_SCHEMA,
        'system_prompt': ENTRY_SYSTEM,
        'build_prompt': build_entry_prompt,
        # 非逐字 fact 降级为 inference。**原先没有这一项**（selection/portfolio/review 都有）
        # ⇒ `fact 未逐字匹配` 直接让整条 entry 决策作废，L 路恒等于 R。
        'normalize': normalize_entry_output,
        'validate': validate_entry_v2,
    }


def _position_contract():
    from mutifactor.llm.contracts.position_v2 import (
        POSITION_DECISION_SCHEMA, POSITION_SYSTEM, POSITION_V2_PROMPT_VERSION,
        POSITION_V2_SCHEMA_VERSION, build_position_prompt, normalize_position_output,
        validate_position_v2,
    )
    return {
        'schema_version': POSITION_V2_SCHEMA_VERSION,
        'prompt_version': POSITION_V2_PROMPT_VERSION,
        'output_schema': POSITION_DECISION_SCHEMA,
        'system_prompt': POSITION_SYSTEM,
        'build_prompt': build_position_prompt,
        # 同上：原先缺这一项，持仓的 L 路也拿不到有效动作。
        'normalize': normalize_position_output,
        'validate': validate_position_v2,
    }


def _portfolio_contract():
    from mutifactor.llm.contracts.portfolio_v1 import (
        PORTFOLIO_DECISION_SCHEMA, PORTFOLIO_SYSTEM, PORTFOLIO_V1_PROMPT_VERSION,
        PORTFOLIO_V1_SCHEMA_VERSION, build_portfolio_prompt, normalize_portfolio_output,
        validate_portfolio_v1,
    )
    return {
        'schema_version': PORTFOLIO_V1_SCHEMA_VERSION,
        'prompt_version': PORTFOLIO_V1_PROMPT_VERSION,
        'output_schema': PORTFOLIO_DECISION_SCHEMA,
        'system_prompt': PORTFOLIO_SYSTEM,
        'build_prompt': build_portfolio_prompt,
        'normalize': normalize_portfolio_output,
        'validate': validate_portfolio_v1,
    }


def _review_contract():
    from mutifactor.llm.contracts.review_v1 import (
        REVIEW_DECISION_SCHEMA, REVIEW_SYSTEM, REVIEW_V1_PROMPT_VERSION,
        REVIEW_V1_SCHEMA_VERSION, build_review_prompt, normalize_review_output,
        validate_review_v1,
    )
    return {
        'schema_version': REVIEW_V1_SCHEMA_VERSION,
        'prompt_version': REVIEW_V1_PROMPT_VERSION,
        'output_schema': REVIEW_DECISION_SCHEMA,
        'system_prompt': REVIEW_SYSTEM,
        'build_prompt': build_review_prompt,
        'normalize': normalize_review_output,
        'validate': validate_review_v1,
    }


_CONTRACTS = {
    'selection': _selection_contract,
    'entry': _entry_contract,
    'position': _position_contract,
    'portfolio': _portfolio_contract,
    'review': _review_contract,
}

# 关键审计事件：写失败即抛异常（不能出现「有动作无审计链」）
CRITICAL_EVENTS = frozenset({
    'decision_requested', 'decision_validated', 'decision_validation_failed',
    'permission_applied', 'decision_effective_action',
})


class DecisionEngine:
    """三类决策统一编排器。advisor 需提供 .chat(prompt, system=...) 与 .model。"""

    def __init__(self, registry, advisor=None, config=None, store=None, guard=None,
                 call_model: Optional[Callable] = None):
        self.registry = registry
        self.advisor = advisor
        self.config = config or {}
        self.store = store or DecisionRunStore(registry)
        self.guard = guard or PermissionGuard(registry, store=self.store)
        self._call_model_fn = call_model
        self.scope = registry.namespace

    # ---------- 入口 ----------

    def decide_selection(self, packet: Dict[str, Any]) -> DecisionResult:
        return self._decide('selection', 'research_batch', packet, _CONTRACTS['selection']())

    def decide_entry(self, packet: Dict[str, Any]) -> DecisionResult:
        return self._decide('entry', 'signal', packet, _CONTRACTS['entry']())

    def decide_position(self, packet: Dict[str, Any]) -> DecisionResult:
        return self._decide('position', 'trade', packet, _CONTRACTS['position']())

    def decide_portfolio(self, packet: Dict[str, Any]) -> DecisionResult:
        return self._decide('portfolio', 'portfolio', packet, _CONTRACTS['portfolio']())

    def decide_review(self, packet: Dict[str, Any]) -> DecisionResult:
        return self._decide('review', 'evaluation_window', packet,
                            _CONTRACTS['review']())

    # ---------- 显式重试（§4.1：新 attempt，不改变历史有效动作） ----------

    def retry_selection(self, packet: Dict[str, Any]) -> DecisionResult:
        return self._decide('selection', 'research_batch', packet,
                            _CONTRACTS['selection'](), force_rerun=True)

    def retry_entry(self, packet: Dict[str, Any]) -> DecisionResult:
        return self._decide('entry', 'signal', packet,
                            _CONTRACTS['entry'](), force_rerun=True)

    def retry_position(self, packet: Dict[str, Any]) -> DecisionResult:
        return self._decide('position', 'trade', packet,
                            _CONTRACTS['position'](), force_rerun=True)

    # ---------- 模板方法 ----------

    def _decide(self, role: str, subject_type: str, packet: Dict[str, Any],
                contract: Dict[str, Any], force_rerun: bool = False) -> DecisionResult:
        # 深拷贝：不得改写调用方 packet（否则快照内容会随最终 decision_id 变化，破坏幂等）
        import copy as _copy
        packet = _copy.deepcopy(packet)
        context = packet.get('context') or {}
        subject_id = context.get('subject_id', '')
        versions = context.get('versions') or {}
        model_cfg = context.get('model') or {}
        provider = model_cfg.get('provider', '')
        model_id = model_cfg.get('model_id', getattr(self.advisor, 'model', ''))
        if role not in VALID_ROLES:
            raise ValueError(f'非法 role: {role}')
        if context.get('role') != role:
            raise ValueError(f'DecisionContext role 与调用入口不一致: {context.get("role")} != {role}')
        if subject_type != role_contract(role).subject_type:
            raise ValueError(f'{role} subject_type 契约错误: {subject_type}')
        if context.get('subject_type') != subject_type:
            raise ValueError(
                f'DecisionContext subject_type 与调用入口不一致: '
                f'{context.get("subject_type")} != {subject_type}')
        if context.get('account_scope') != self.scope:
            raise ValueError(
                f'DecisionContext account_scope 与当前账户不一致: '
                f'{context.get("account_scope")} != {self.scope}')
        if not subject_id:
            raise ValueError('DecisionContext subject_id 不能为空')

        # 快照内不承载最终 decision_id（decision_id 依赖快照哈希，避免循环）
        context['decision_id'] = ''

        # 1. 输入先于模型调用持久化（快照 id 是输入内容哈希）
        input_snapshot_id = self.store.save_input_snapshot(role, subject_id, packet)

        # 2. 绑定冻结输入的 decision_id（§4.1：覆盖 input_snapshot_id）
        decision_id = finalize_decision_id(
            account_scope=self.scope, role=role, subject_id=subject_id,
            input_snapshot_id=input_snapshot_id, versions=versions, model_id=model_id)
        context['decision_id'] = decision_id

        # 3. 幂等：普通 decide 对正式结果直接恢复；显式重试走 retry_* 接口（force_rerun）
        existing = self.store.get_run(decision_id)
        if not force_rerun and existing and existing.get('status') in ('validated', 'failed'):
            return self._reconstruct(existing)

        # 4. 记录请求（关键事件，硬失败；同一 decision_id 幂等）
        self._record('decision_requested', decision_id,
                     {'role': role, 'subject_type': subject_type, 'subject_id': subject_id,
                      'input_snapshot_id': input_snapshot_id},
                     decision_id=decision_id, critical=True)
        self.store.save_run(self._run_row(context, subject_type, 'requested',
                                          input_snapshot_id, provider, model_id, None))

        # 5. 权限快照：决策开始时固化；重试沿用原快照，不重新生成带新时间的内容
        perm_snapshot = self.guard.load_permission_snapshot(decision_id)
        if perm_snapshot is None:
            perm_snapshot = self.guard.snapshot_permissions(self.config, versions)
            self.guard.save_permission_snapshot(decision_id, perm_snapshot)

        # 6. 模型调用
        attempt_id = stable_id('attempt', decision_id, model_id, str(time.time()))
        started = time.time()
        self._record('model_attempt_started', attempt_id,
                     {'decision_id': decision_id, 'model_id': model_id},
                     decision_id=decision_id, attempt_id=attempt_id)
        raw = self._call_model(contract, packet)
        latency_ms = int((time.time() - started) * 1000)

        if raw is None:
            self._save_attempt(attempt_id, decision_id, 'failed', None, None, latency_ms,
                               started, model_id)
            self._record('model_attempt_failed', attempt_id,
                         {'decision_id': decision_id, 'model_id': model_id},
                         decision_id=decision_id, attempt_id=attempt_id)
            self.store.save_run(self._run_row(context, subject_type, 'failed',
                                              input_snapshot_id, provider, model_id, attempt_id))
            return DecisionResult(decision_id, role, 'failed', None, None, 'shadow', (),
                                  input_snapshot_id, attempt_id)

        parsed = self._parse(raw)

        # 7. 确定性规范化 + 校验。raw/parsed attempt 保留原始模型响应；
        # validated snapshot 保存降权后的规范结果。
        errors: Tuple[str, ...] = ()
        if parsed is None:
            errors = ('模型无输出/非 JSON',)
        else:
            try:
                if contract.get('normalize'):
                    parsed = contract['normalize'](parsed, packet)
                errors = tuple(contract['validate'](parsed, packet) or [])
            except Exception as exc:
                errors = (f'validate异常: {type(exc).__name__}: {exc}',)

        self._save_attempt(attempt_id, decision_id, 'completed', raw, parsed, latency_ms,
                           started, model_id, validation_errors=list(errors) or None)

        # 可选的一次结构修复：只提供原输出、错误、合法 evidence ID 和原 schema，
        # 不重新要求模型分析行情。默认关闭，由运行配置显式启用。
        if errors and self._repair_enabled():
            repaired = self._repair_once(
                role, contract, packet, parsed, errors, decision_id, model_id)
            if repaired is not None:
                parsed, errors, attempt_id = repaired

        if errors:
            self._record('decision_validation_failed', [decision_id, attempt_id],
                         {'errors': list(errors)}, decision_id=decision_id,
                         attempt_id=attempt_id, critical=True)
            self.store.save_run(self._run_row(context, subject_type, 'failed',
                                              input_snapshot_id, provider, model_id, attempt_id))
            self.store.save_snapshot(
                'validated_decision', decision_id,
                {'status': 'failed', 'errors': list(errors)},
                version=self.store.next_snapshot_version('validated_decision', decision_id))
            return DecisionResult(decision_id, role, 'failed', parsed, None, 'shadow',
                                  errors, input_snapshot_id, attempt_id)

        validated = parsed

        # 8. 权限应用（伞级 + 具体子权限取最严格，子权限 scope gate）
        model_action = self._model_action(role, validated)
        validate_role_action(role, model_action)
        perms = applicable_permissions(role, model_action)
        restrictive = self.guard.most_restrictive(perms, perm_snapshot, self.config, versions)
        level = restrictive['level']
        perm_name = specific_permission(role, model_action)
        applied = self.guard.apply(role, model_action, level, perm_name,
                                   validated=validated, packet=packet)
        effective_action = applied['effective_action']

        # Entry 强制动作（§8.4）：未来数据/撤销/质量门 → 覆盖模型动作
        if role == 'entry':
            from mutifactor.llm.contracts.entry_v2 import forced_entry_action
            forced = forced_entry_action(packet, list(errors), as_of=context.get('as_of'))
            if forced:
                effective_action = forced
                applied['shadow_difference'] = f'forced_{forced}'

        self.store.save_snapshot(
            'validated_decision', decision_id, {
                'status': 'complete', 'output': validated, 'errors': [],
                'model_action': model_action, 'effective_action': effective_action,
                'permission_level': level,
            },
            version=self.store.next_snapshot_version('validated_decision', decision_id))

        self._record('decision_validated', [decision_id, attempt_id],
                     {'role': role, 'errors': []}, decision_id=decision_id,
                     attempt_id=attempt_id, critical=True)
        self._record('permission_applied', [decision_id, attempt_id],
                     {'permission_name': perm_name, 'level': level,
                      'applicable': restrictive.get('levels', {}),
                      'reason': restrictive.get('reason', '')},
                     decision_id=decision_id, attempt_id=attempt_id, critical=True)
        self._record('decision_effective_action', [decision_id, attempt_id],
                     {'model_action': model_action,
                      'effective_action': effective_action,
                      'permission_level': level,
                      'shadow_difference': applied.get('shadow_difference')},
                     decision_id=decision_id, attempt_id=attempt_id, critical=True)

        self.store.save_run(self._run_row(context, subject_type, 'validated',
                                          input_snapshot_id, provider, model_id, attempt_id,
                                          effective_action=effective_action))

        return DecisionResult(
            decision_id=decision_id, role=role, status='validated',
            validated_output=validated, effective_action=effective_action,
            permission_level=level, validation_errors=(), input_snapshot_id=input_snapshot_id,
            attempt_id=attempt_id, model_action=model_action,
            shadow_difference=applied.get('shadow_difference'))

    # ---------- 内部 ----------

    def _model_action(self, role: str, validated: Dict[str, Any]) -> str:
        if role == 'selection':
            return 'llm_ranking'
        # 回退用**该角色契约里的 fallback_action**，不要硬编码某个角色的默认值：
        # 写死 'hold' 会让新角色的缺失动作静默变成一次 position 动作。
        return validated.get('action') or role_contract(role).fallback_action

    def _call_model(self, contract: Dict[str, Any], packet: Dict[str, Any]):
        if self._call_model_fn is not None:
            return self._call_model_fn(contract, packet)
        if self.advisor is None or not getattr(self.advisor, 'enabled', True):
            return None
        prompt = contract['build_prompt'](packet)
        system = contract['system_prompt']
        try:
            return self.advisor.chat(prompt, system=system)
        except Exception as exc:
            logger.warning('模型调用异常: %s', exc)
            return None

    @staticmethod
    def _parse(raw):
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return None
        return None

    def _run_row(self, context: Dict[str, Any], subject_type: str, status: str,
                 input_snapshot_id: str, provider: str, model_id: str,
                 selected_attempt_id: Optional[str],
                 effective_action: Optional[str] = None) -> Dict[str, Any]:
        versions = context.get('versions') or {}
        return {
            'decision_id': context['decision_id'],
            'role': context['role'],
            'subject_type': subject_type,
            'subject_id': context.get('subject_id', ''),
            'as_of': context.get('as_of', utc()),
            'status': status,
            'input_snapshot_id': input_snapshot_id,
            'prompt_version': versions.get('prompt', ''),
            'output_schema_version': versions.get('output_schema', ''),
            'feature_version': versions.get('feature', ''),
            'rule_version': versions.get('rule', ''),
            'permission_version': versions.get('permission', ''),
            'provider': provider,
            'model_id': model_id,
            'selected_attempt_id': selected_attempt_id,
            'effective_action': effective_action,
            'created_at': utc(),
        }

    def _save_attempt(self, attempt_id, decision_id, status, raw, parsed,
                      latency_ms, started, model_id, validation_errors=None):
        meta = getattr(self.advisor, 'last_metadata', {}) or {}
        self.store.save_attempt({
            'attempt_id': attempt_id,
            'decision_id': decision_id,
            'started_at': utc(started),
            'completed_at': utc(),
            'status': status,
            'raw_response': json.dumps(raw, ensure_ascii=False, default=str) if raw is not None else None,
            'parsed_response': parsed,
            'validation_errors': validation_errors,
            'latency_ms': latency_ms,
            'input_tokens': (meta.get('usage') or {}).get('prompt_tokens'),
            'output_tokens': (meta.get('usage') or {}).get('completion_tokens'),
        })

    def _repair_enabled(self) -> bool:
        decision = (self.config.get('llm_decision')
                    if isinstance(self.config.get('llm_decision'), dict)
                    else self.config)
        repair = (decision or {}).get('validation_repair') or {}
        return bool(repair.get('enabled', False))

    @staticmethod
    def _evidence_ids(role: str, packet: Dict[str, Any]):
        if role == 'selection':
            items = [e for stock in packet.get('stocks', []) for e in stock.get('evidence', [])]
        elif role == 'position':
            items = packet.get('new_evidence', [])
        else:
            items = packet.get('evidence', [])
        return sorted({e.get('evidence_id') for e in items if e.get('evidence_id')})

    def _repair_once(self, role, contract, packet, parsed, errors, decision_id, model_id):
        repair_id = stable_id('attempt', decision_id, model_id, 'repair', str(time.time()))
        repair_input = {
            'repair_only': True, 'role': role, 'original_output': parsed,
            'validation_errors': list(errors),
            'allowed_evidence_ids': self._evidence_ids(role, packet),
            'output_schema': contract['output_schema'],
        }
        repair_contract = {
            'repair': True,
            'build_prompt': lambda value: json.dumps(value, ensure_ascii=False),
            'system_prompt': (
                '只修复给定 JSON 的结构和引用错误。不得增加新的事实、判断、动作或证据；'
                'evidence_ids 只能从 allowed_evidence_ids 逐字复制。只输出修复后的 JSON。'),
        }
        self._record('decision_repair_attempted', repair_id,
                     {'decision_id': decision_id, 'source_errors': list(errors)},
                     decision_id=decision_id, attempt_id=repair_id)
        started = time.time()
        raw = self._call_model(repair_contract, repair_input)
        latency_ms = int((time.time() - started) * 1000)
        repaired = self._parse(raw)
        repair_errors = ('模型无输出/非 JSON',) if repaired is None else ()
        if repaired is not None:
            try:
                if contract.get('normalize'):
                    repaired = contract['normalize'](repaired, packet)
                repair_errors = tuple(contract['validate'](repaired, packet) or [])
            except Exception as exc:
                repair_errors = (f'validate异常: {type(exc).__name__}: {exc}',)
        self._save_attempt(repair_id, decision_id,
                           'completed' if raw is not None else 'failed', raw, repaired,
                           latency_ms, started, model_id,
                           validation_errors=list(repair_errors) or None)
        return repaired, repair_errors, repair_id

    def _record(self, event_type, key, payload, critical=False, **links):
        """记录事件。关键审计事件写失败即抛异常；辅助事件 best-effort。"""
        try:
            self.store.events.record(event_type, key, payload, **links)
        except Exception:
            if critical or event_type in CRITICAL_EVENTS:
                raise
            logger.exception('辅助事件记录失败: %s', event_type)

    def _reconstruct(self, run: Dict[str, Any]) -> DecisionResult:
        decision_id = run['decision_id']
        role = run['role']
        validated = self.store.get_snapshot_latest('validated_decision', decision_id) or {}
        return DecisionResult(
            decision_id=decision_id, role=role, status=run['status'],
            validated_output=validated.get('output'),
            effective_action=validated.get('effective_action') or run.get('effective_action'),
            permission_level=validated.get('permission_level', 'shadow'),
            validation_errors=tuple(validated.get('errors') or []),
            input_snapshot_id=run['input_snapshot_id'],
            attempt_id=run.get('selected_attempt_id'),
            model_action=validated.get('model_action'))
