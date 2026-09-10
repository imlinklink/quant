"""DecisionEngine 影子接入桥（T3）。

把现有 selection / entry / position 的输入构造成 §7/§8/§9 的 packet，调用 DecisionEngine。
默认全 shadow，不改变现网行为：只落决策账本、attempt、validated snapshot 与投影，
供 replay / metrics / outcome 使用。现网监控器可逐步切到本桥，模型调用仍只发生在 DecisionEngine。

用法：
    bridge = ShadowBridge(registry, advisor, config)
    result = bridge.run_selection(universe, packets, rule_ranking=None)
"""
import time
from typing import Any, Dict, List, Optional

from mutifactor.llm.contracts.common import build_evidence_item
from mutifactor.llm.contracts.entry_v2 import build_entry_templates
from mutifactor.llm.contracts.position_v2 import build_position_action_templates
from scripts.live_trading.decision_engine import DecisionEngine
from scripts.live_trading.decision_ledger.decision_run_store import build_context
from scripts.live_trading.decision_ledger.event_store import utc

# 默认版本（§4.1 / §11.2）；实际版本由调用方从配置覆盖
DEFAULT_VERSIONS = {
    'packet_schema': 'selection-v4.2',
    'prompt': 'selection-v4.2',
    'output_schema': 'selection-v4.2',
    'feature': 'feature-v2',
    'rule': 'rule-v2',
    'permission': 'permission-v2',
}


def evidence_item(e: Dict[str, Any], subject_code: Optional[str]) -> Dict[str, Any]:
    """把 legacy 证据 dict 归一为统一 EvidenceItem（§4.2）。"""
    observed = e.get('observed_at') or e.get('event_time') or utc()
    return build_evidence_item(
        evidence_id=e.get('evidence_id') or '',
        subject_code=subject_code,
        kind=e.get('kind', 'rule'),
        source=e.get('source', 'internal:legacy'),
        source_grade=int(e.get('source_grade', 1)),
        summary=e.get('summary', ''),
        observed_at=observed,
        published_at=e.get('published_at') or e.get('event_time'),
        effective_at=observed,
        cluster_id=e.get('cluster_id'),
        content_hash=e.get('content_hash'),
        quality=e.get('quality', 'good'),
        quality_reasons=e.get('quality_reasons'),
        payload=e.get('payload') or {},
    )


def stocks_from_packets(packets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 build_evidence_packet 的输出映射为 §7.1 的 stocks 列表。"""
    stocks = []
    for p in packets or []:
        code = p.get('code')
        evidence = [evidence_item(e, code) for e in p.get('events', [])]
        stocks.append({
            'code': code,
            'packet_id': p.get('packet_id'),
            'evidence': evidence,
            'features': p.get('strategy') or {},
            'option_view': {},
            'correlation_to_portfolio': None,
            'quality_gate': _quality_gate(p.get('data_quality') or {}),
        })
    return stocks


def _quality_gate(dq: Dict[str, Any]) -> Dict[str, Any]:
    """把 evidence_packet.data_quality 映射为 QualityGateResult（§5.1）。"""
    ok = bool(dq.get('ok', True))
    return {
        'status': 'pass' if ok else ('degraded' if dq.get('missing_count', 0) == 0 else 'fail'),
        'checks': [],
        'allowed_uses': ['rank', 'entry', 'position'] if ok else ['rank'],
        'generated_at': utc(),
    }


def build_selection_packet(*, batch_id: str, account_scope: str, discovery_codes: List[str],
                           stocks: List[Dict[str, Any]], as_of: str,
                           versions: Optional[Dict[str, str]] = None,
                           model: Optional[Dict[str, Any]] = None,
                           execution_eligible_codes: Optional[List[str]] = None,
                           hard_exclusions: Optional[List[Dict[str, str]]] = None,
                           portfolio: Optional[Dict[str, Any]] = None,
                           market_session: str = 'closed') -> Dict[str, Any]:
    """构造 §7.1 SelectionPacket。"""
    versions = dict(DEFAULT_VERSIONS, **(versions or {}))
    model = model or {'provider': 'deepseek', 'model_id': 'deepseek-chat',
                      'temperature': 0.0, 'timeout_seconds': 60}
    context = build_context(role='selection', subject_type='research_batch', subject_id=batch_id,
                            account_scope=account_scope, as_of=as_of, versions=versions,
                            model=model, market_session=market_session)
    return {
        'context': context,
        'universe': {
            'discovery_codes': list(discovery_codes),
            'execution_eligible_codes': list(execution_eligible_codes or discovery_codes),
            'hard_exclusions': list(hard_exclusions or []),
        },
        'market_regime': {},
        'portfolio': portfolio or {
            'positions': [], 'risk_group_usage': {}, 'remaining_risk_budget': 0.0,
            'drawdown_state': 'normal',
        },
        'stocks': list(stocks),
    }


def build_entry_packet(*, signal: Dict[str, Any], plan: Dict[str, Any],
                       evidence: List[Dict[str, Any]], account_scope: str,
                       subject_id: str, as_of: str,
                       standard_quantity: int, entry_price: float, initial_stop: float,
                       versions: Optional[Dict[str, str]] = None,
                       model: Optional[Dict[str, Any]] = None,
                       quality_uses=('rank', 'entry'),
                       expires_at: str = '2999-01-01T00:00:00+00:00',
                       review_triggers=(),
                       market_session: str = 'regular') -> Dict[str, Any]:
    """构造 §8.1 EntryPacket（程序先算好模板）。"""
    versions = versions or {'packet_schema': 'entry-v2', 'prompt': 'entry-v2',
                            'output_schema': 'entry-v2', 'feature': 'feature-v2',
                            'rule': 'rule-v2', 'permission': 'permission-v2'}
    model = model or {'provider': 'deepseek', 'model_id': 'deepseek-chat',
                      'temperature': 0.0, 'timeout_seconds': 30}
    context = build_context(role='entry', subject_type='signal', subject_id=subject_id,
                            account_scope=account_scope, as_of=as_of, versions=versions,
                            model=model, market_session=market_session)
    templates = build_entry_templates(plan=plan, standard_quantity=standard_quantity,
                                      entry_price=entry_price, initial_stop=initial_stop,
                                      review_triggers=review_triggers,
                                      expires_at=expires_at)
    return {
        'context': context,
        'signal': signal,
        'selection_context': {},
        'plan': plan,
        'templates': templates,
        'evidence': [evidence_item(e, plan.get('stock_code')) for e in evidence],
        'portfolio': {},
        'quality_gate': {'status': 'pass', 'allowed_uses': list(quality_uses)},
    }


def build_position_packet(*, trade: Dict[str, Any], protection: Dict[str, Any],
                          new_evidence: List[Dict[str, Any]], account_scope: str,
                          subject_id: str, as_of: str,
                          versions: Optional[Dict[str, str]] = None,
                          model: Optional[Dict[str, Any]] = None,
                          thesis: Optional[Dict[str, Any]] = None,
                          removed_evidence_ids: Optional[List[str]] = None,
                          market_session: str = 'regular') -> Dict[str, Any]:
    """构造 §9.2 PositionPacket（程序先算好动作模板）。"""
    versions = versions or {'packet_schema': 'position-v2', 'prompt': 'position-v2',
                            'output_schema': 'position-v2', 'feature': 'feature-v2',
                            'rule': 'rule-v2', 'permission': 'permission-v2'}
    model = model or {'provider': 'deepseek', 'model_id': 'deepseek-chat',
                      'temperature': 0.0, 'timeout_seconds': 30}
    context = build_context(role='position', subject_type='trade', subject_id=subject_id,
                            account_scope=account_scope, as_of=as_of, versions=versions,
                            model=model, market_session=market_session)
    templates = build_position_action_templates(
        trade=trade, active_stop=protection.get('active_stop', 0.0),
        expires_at='2999-01-01T00:00:00+00:00')
    code = trade.get('code')
    return {
        'context': context,
        'trade': trade,
        'protection': protection,
        'thesis': thesis or {},
        'new_evidence': [evidence_item(e, code) for e in new_evidence],
        'removed_or_expired_evidence_ids': list(removed_evidence_ids or []),
        'market_and_portfolio': {},
        'trigger': {},
        'allowed_actions': templates,
        'quality_gate': {'status': 'pass', 'allowed_uses': ['position']},
    }


class ShadowBridge:
    """把三类决策影子接入 DecisionEngine。默认 shadow，只落账不执行。"""

    def __init__(self, registry, advisor=None, config=None):
        self.engine = DecisionEngine(registry, advisor=advisor, config=config)

    def run_selection(self, *, batch_id: str, account_scope: str,
                      discovery_codes: List[str], packets: List[Dict[str, Any]],
                      as_of: str, versions=None, model=None,
                      execution_eligible_codes=None, hard_exclusions=None,
                      portfolio=None) -> Any:
        stocks = stocks_from_packets(packets)
        packet = build_selection_packet(
            batch_id=batch_id, account_scope=account_scope,
            discovery_codes=discovery_codes, stocks=stocks, as_of=as_of,
            versions=versions, model=model,
            execution_eligible_codes=execution_eligible_codes,
            hard_exclusions=hard_exclusions, portfolio=portfolio)
        return self.engine.decide_selection(packet)

    def run_entry(self, *, signal, plan, evidence, account_scope, subject_id, as_of,
                  standard_quantity, entry_price, initial_stop, versions=None, model=None,
                  expires_at='2999-01-01T00:00:00+00:00', review_triggers=()):
        packet = build_entry_packet(
            signal=signal, plan=plan, evidence=evidence, account_scope=account_scope,
            subject_id=subject_id, as_of=as_of, standard_quantity=standard_quantity,
            entry_price=entry_price, initial_stop=initial_stop,
            versions=versions, model=model, expires_at=expires_at,
            review_triggers=review_triggers)
        return self.engine.decide_entry(packet)

    def run_position(self, *, trade, protection, new_evidence, account_scope, subject_id,
                     as_of, versions=None, model=None, thesis=None):
        packet = build_position_packet(
            trade=trade, protection=protection, new_evidence=new_evidence,
            account_scope=account_scope, subject_id=subject_id, as_of=as_of,
            versions=versions, model=model, thesis=thesis)
        return self.engine.decide_position(packet)


def decision_metadata(result) -> Dict[str, Any]:
    """供旧业务对象保存的最小交叉引用。"""
    return {
        'decision_id': result.decision_id,
        'input_snapshot_id': result.input_snapshot_id,
        'decision_status': result.status,
        'decision_engine_version': 'v2',
        'model_action': result.model_action,
        'effective_action': result.effective_action,
        'permission_level': result.permission_level,
    }


def selection_legacy_projection(result) -> Dict[str, Any]:
    """Selection v4 → 当前研究批次结构；纯函数，不重新解释模型。"""
    meta = decision_metadata(result)
    output = result.validated_output or {}
    ranked = list(output.get('ranked') or []) if result.status == 'validated' else []
    def texts(rows):
        return [str(x.get('text') or x) if isinstance(x, dict) else str(x)
                for x in (rows or [])]

    def candidate(row):
        return dict(
            row,
            rank=row.get('portfolio_rank') or row.get('standalone_rank'),
            preferred_entry_mode=row.get('setup_type') or 'none',
            confidence_bucket=row.get('confidence') or 'low',
            thesis='；'.join(texts(row.get('thesis'))) or '未提供论文',
            watch_conditions=list(row.get('watch_conditions') or []),
            invalidators=texts(row.get('invalidation_conditions')),
            missing_information=list(row.get('missing_information') or []),
        )

    def exclusion(row):
        reasons = list(row.get('reason_codes') or [])
        return dict(row, reason='；'.join(reasons or texts(row.get('counterevidence')))
                    or '模型标记为 exclude')

    candidates = [candidate(row) for row in ranked
                  if row.get('decision') in ('candidate', 'watch')]
    exclusions = [exclusion(row) for row in ranked if row.get('decision') == 'exclude']
    return dict(meta, candidates=candidates, exclusions=exclusions,
                no_candidate_reason='; '.join(output.get('abstain_reason_codes') or []),
                error=None if result.status == 'validated' else
                ('validate_failed: ' + '; '.join(result.validation_errors)
                 if result.validation_errors else 'llm_failed'))


def entry_legacy_projection(result, request: Optional[Dict[str, Any]] = None,
                            now: Optional[float] = None, ttl: float = 180) -> Dict[str, Any]:
    """Entry v2 → ProposalStore.llm 兼容结构。"""
    request = request or {}
    now = time.time() if now is None else now
    output = result.validated_output or {}
    action = result.model_action
    recommendation = {
        'execute_now': 'support_execute',
        'defer': 'wait_for_confirmation',
        'reject': 'oppose_execute',
    }.get(action, 'defer')
    status = 'complete' if result.status == 'validated' else 'failed'
    projected = {
        'status': status,
        'recommendation': recommendation,
        'proposed_action': 'buy' if action == 'execute_now' else 'hold',
        'reason': '; '.join(output.get('reason_codes') or output.get('missing_information') or []),
        'facts': list(output.get('facts') or []),
        'inferences': list(output.get('inferences') or []),
        'counterevidence': list(output.get('counterevidence') or []),
        'missing_information': list(output.get('missing_information') or []),
        'plan_change_requested': False,
        'template_id': output.get('template_id'),
        'shadow_defer': action == 'defer',
        'review_id': request.get('review_id'),
        'plan_id': request.get('plan_id'),
        'plan_version': request.get('plan_version'),
        # Proposal 的 legacy input 与 v2 input 是两个不可变快照，分别保存。
        'input_snapshot_id': request.get('input_snapshot_id'),
        'decision_input_snapshot_id': result.input_snapshot_id,
        'requested_at': request.get('review_requested_at'),
        'responded_at': now,
        'expires_at': min(float(request.get('expires_at', now + ttl)), now + ttl),
        'raw_output': output,
    }
    projected.update(decision_metadata(result))
    projected['input_snapshot_id'] = request.get('input_snapshot_id')
    projected['decision_input_snapshot_id'] = result.input_snapshot_id
    return projected


def position_legacy_projection(result, *, trigger: str,
                               legacy_input_snapshot_id: str) -> Dict[str, Any]:
    """Position v2 → position_reviewed / ThesisLedger 兼容结构。"""
    output = result.validated_output or {}
    state_map = {
        'CONFIRMED': 'strengthened', 'WEAKENING': 'weakened',
        'INVALIDATED': 'invalidated', 'CLOSED': 'closed',
        'FORMING': 'established',
    }
    review = {
        'status': 'complete' if result.status == 'validated' else 'failed',
        'thesis_state': state_map.get(output.get('thesis_state'), 'unchanged'),
        'recommendation': result.model_action or 'hold',
        'proposed_action': result.model_action or 'hold',
        'facts': list(output.get('facts') or []),
        'inferences': list(output.get('inferences') or []),
        'counterevidence': list(output.get('counterevidence') or []),
        'missing_information': list(output.get('missing_information') or []),
        'action_template_id': output.get('action_template_id'),
        'shadow_only': True,
        'trigger': trigger,
        'input_snapshot_id': legacy_input_snapshot_id,
        'decision_input_snapshot_id': result.input_snapshot_id,
        'raw_output': output,
        'plan_change_applied': False,
    }
    review.update(decision_metadata(result))
    review['input_snapshot_id'] = legacy_input_snapshot_id
    review['decision_input_snapshot_id'] = result.input_snapshot_id
    return review
