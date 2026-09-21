"""持仓评审的冻结证据包（影子侧）。

形状 = Position v2 的 PositionPacket（`context/trade/identity/protection/thesis/
new_evidence/allowed_actions`），使 `validate_position_v2` 与
`build_position_action_templates` 可以**逐字复用**，不另立一套持仓动作契约；
影子侧另加 `data_quality`（`llm_overlay.gate` 读的就是它）、`events`（原始形状，供
账本与报告溯源）、`evidence`（等级/排除统计）与 `packet_id`。

**评审主体是 R 账户的持仓**（规则决定、与 L 历史无关）。动作应用到 L 时按档位相对计算
（见 `paper_engine.step` 的 2.5 阶段）——冻结的绝对数量是按评审主体的剩余量算的，
拿它去卖 L 会过量。

证据归属**绝不覆盖**：`subject_code` 一律取自证据自身的 `security_id`。把每条证据都改写成
本次标的，会让跨股票校验形同虚设、市场日报伪装成公司事实（审计 §4.2）。
"""
from __future__ import annotations

from mutifactor.llm.contracts.position_v2 import (POSITION_V2_PROMPT_VERSION,
                                                  POSITION_V2_SCHEMA_VERSION,
                                                  build_position_action_templates)

from .evidence import _clean, _norm_events, _parse_iso, market_close
from .position_overlay import subject_key_for
from scripts.live_trading.decision_ledger.event_store import stable_id

POSITION_PACKET_SCHEMA_VERSION = 'position-packet-v1'


def _to_new_evidence(events, as_of_dt) -> list:
    """把影子事件的形状转成 Position v2 校验器要求的 `new_evidence` 形状。

    时间戳统一规范成 `isoformat()`：`validate_claims` 用**字符串比较**判未来证据，
    混合格式（'Z' vs '+00:00'）会让比较给出错误结论。
    """
    items = []
    for e in events:
        published = _parse_iso(e.get('published_at'))
        observed = _parse_iso(e.get('observed_at'))
        items.append({
            'evidence_id': e.get('evidence_id'),
            # 真实归属，不得改写成本次标的
            'subject_code': str(_clean(e.get('security_id')) or ''),
            'kind': e.get('kind') or e.get('event_type') or 'news',
            'source': e.get('source') or '',
            'source_url': e.get('source_url') or '',
            'summary': e.get('summary') or '',
            'title': e.get('title') or '',
            'cluster_id': e.get('cluster_id') or '',
            'content_hash': e.get('content_hash'),
            'published_at': published.isoformat() if published else None,
            'observed_at': observed.isoformat() if observed else None,
            'effective_at': None,
            'expires_at': None,
            'quality': e.get('quality') or 'ok',
            'quality_reasons': [],
        })
    return items


def build_position_packet(*, security_id, trade, protection, events, as_of,
                          execution_session, account_scope, experiment_id,
                          opportunity_id, reviewed_session, shares, entry_price_micro,
                          mark_price_micro, thesis=None, identity=None, evidence=None,
                          model_knowledge_cutoff=None, market_context=None,
                          fetch_status='OK', expires_at=None, technical=None,
                          open_actions=None) -> dict:
    """构建持仓评审用的冻结证据包。

    `trade`/`protection` 描述的是**评审主体**（R 账户持仓）；`shares`/`mark_price_micro`
    用于关键数据判定。`as_of` 是证据的实际采集时刻，必须落在
    [评审日收盘, 执行日截止] 之间（设计 §3.1/§3.2 是两个不同的时刻，不能混）。

    `technical`：`strategy_research.technical_packet` 算出的逐项技术事实。给定它时，
    **充分性以技术包自己的标准为准**（§6.2），新闻只是可选输入 —— 否则「不接公司事件源」
    会让这个角色永远 `LLM_INSUFFICIENT`、模型永不被调用。不给它时行为与引入前完全相同。
    `open_actions`：本轮**只开放**的动作集合（§6.1 限定角色）。给定即过滤动作模板，
    并在包里记下这个集合供校验器强制。
    """
    as_of_dt = _parse_iso(as_of)
    if as_of_dt is None:
        raise ValueError('AS_OF_INVALID')
    as_of_iso = as_of_dt.isoformat()

    subject_key = subject_key_for(opportunity_id, execution_session)
    # 动作模板在评审日收盘冻结；失效时刻取执行日收盘（模板在本次执行日内有效）
    expires = expires_at or market_close(execution_session).isoformat()

    trade_body = {
        'trade_id': f'{security_id}@{reviewed_session}',
        'code': security_id,
        'direction': 'long',
        'remaining_qty': float(shares),
        'entry_price': entry_price_micro,
        'mark_price': mark_price_micro,
        'reviewed_session': reviewed_session,
        'entry_session': (trade or {}).get('entry_session'),
        'opportunity_id': opportunity_id,
    }
    protection = protection or {}
    allowed_actions = build_position_action_templates(
        trade=trade_body, active_stop=float(protection.get('active_stop') or 0.0),
        expires_at=expires)
    if open_actions is not None:
        allowed_actions = [t for t in allowed_actions if t.get('action') in set(open_actions)]

    # 关键数据（BLOCK 级）：缺任一，模型就没有可判断的持仓状态，且照常持仓反而是错的
    critical_missing = []
    if not security_id:
        critical_missing.append('security_id')
    if mark_price_micro is None or mark_price_micro <= 0:
        critical_missing.append('trade.mark_price')
    if shares is None or shares <= 0:
        critical_missing.append('trade.remaining_qty')
    if not protection.get('active_stop'):
        critical_missing.append('protection.active_stop')
    if not opportunity_id:
        critical_missing.append('opportunity_id')

    # 证据等级决定双时间过滤的严格程度（与 evidence_store.select_visible 施加的策略一致，
    # 不能反过来再卡一次 —— 那会让诊断模式表面接受、实际全丢）
    evidence = evidence or {}
    require_observed_at = evidence.get('evidence_mode') != 'diagnostic'
    usable_events, dropped = _norm_events(events, as_of_dt,
                                          require_observed_at=require_observed_at)
    quote_observed = _parse_iso((market_context or {}).get('observed_at'))

    # 技术事实（§6.2）：可逐字引用、归属为本持仓证券。排在新闻之前 —— 它才是持仓判断的
    # 主输入，新闻是可选的背景。
    tech = {}
    tech_items = []
    tech_sufficiency = None
    if technical:
        from scripts.strategy_research.technical_packet import (sufficiency as _suff,
                                                                to_evidence_items)
        tech_sufficiency = _suff(technical)
        tech = {'schema_version': tech_sufficiency['schema_version'],
                'facts': {name: fact.as_dict() for name, fact in sorted(technical.items())},
                'sufficiency': tech_sufficiency}
        tech_items = to_evidence_items(technical, security_id=security_id, as_of=as_of_iso)

    if critical_missing or (quote_observed is not None and quote_observed > as_of_dt):
        quality = 'BLOCK'
    elif tech_sufficiency is not None:
        # 有技术包 ⇒ 充分性以它为准（新闻缺失不再降级）
        quality = 'OK' if tech_sufficiency['level'] == 'OK' else 'LLM_INSUFFICIENT'
    elif not usable_events:
        quality = 'LLM_INSUFFICIENT'
    else:
        quality = 'OK'

    packet = {
        'schema_version': POSITION_PACKET_SCHEMA_VERSION,
        'subject_key': subject_key,
        'experiment_id': experiment_id,
        'context': {
            'role': 'position', 'subject_type': 'trade', 'subject_id': subject_key,
            'account_scope': account_scope, 'as_of': as_of_iso,
            'versions': {'packet_schema': POSITION_PACKET_SCHEMA_VERSION,
                         'prompt': POSITION_V2_PROMPT_VERSION,
                         'output_schema': POSITION_V2_SCHEMA_VERSION},
        },
        'trade': trade_body,
        'identity': {'security_id': security_id, 'code': security_id,
                     **(identity or {})},
        'protection': dict(protection),
        'thesis': thesis or {},
        'new_evidence': [*tech_items, *_to_new_evidence(usable_events, as_of_dt)],
        'removed_or_expired_evidence_ids': [],
        'allowed_actions': allowed_actions,
        'market_context': {'price': mark_price_micro, 'price_unit': 'micro_usd',
                           **(market_context or {})},
        # 原始形状：账本与报告按它溯源（content_hash / 正文截断标记都在这里）
        'events': usable_events,
        'data_quality': {'level': quality, 'critical_missing': critical_missing,
                         'dropped_event_count': dropped, 'fetch_status': fetch_status,
                         'evidence_mode': evidence.get('evidence_mode')},
        'as_of': as_of_iso,
        'model_knowledge_cutoff': model_knowledge_cutoff,
        'evidence': evidence,
        'provenance': {'as_of': as_of_iso, 'reviewed_session': reviewed_session,
                       'execution_session': execution_session,
                       'evidence_source_packet_hash': evidence.get('source_packet_hash'),
                       'model_knowledge_cutoff': model_knowledge_cutoff,
                       'packet_schema_version': POSITION_PACKET_SCHEMA_VERSION},
    }
    packet['packet_id'] = stable_id('position_packet', packet)
    if open_actions is not None:
        # 只在限定时才写这个键：默认路径的包保持与引入前逐字段相同（packet_id 也不变）
        packet['allowed_action_set'] = sorted(set(open_actions))
    if tech:
        # 同理：不给技术包就不新增键，legacy 包的 packet_id 一字不变
        packet['technical'] = tech
        packet['data_quality'].update({
            'technical_sufficiency': tech_sufficiency['level'],
            'technical_required_missing': tech_sufficiency['required_missing'],
            'news_events': len(usable_events), 'technical_facts': len(tech_items)})
        packet['packet_id'] = stable_id('position_packet', packet)
    return packet


__all__ = ['POSITION_PACKET_SCHEMA_VERSION', 'build_position_packet']
