"""Evidence Packet（PR5）：入场否决用，自包含、确定性，不依赖 mutifactor。

双时间校验：每条证据的 `published_at` 与 `observed_at` 必须分别合法、带时区、≤ as_of；
关键行情（quote.price / quote.observed_at / security_id）缺失或未来 → BLOCK；
可选新闻不足/陈旧 → LLM_INSUFFICIENT（模型 ABSTAIN 但规则可继续）。
证据等级由采集器确定，不由模型输出覆盖。
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.evidence.evidence_store import market_close, market_time
from scripts.live_trading.decision_ledger.event_store import digest, stable_id

QUALITY_LEVELS = ('BLOCK', 'LLM_INSUFFICIENT', 'OK')

# 证据等级枚举在 schema（策略层）定义；这里只负责把模式映射成 evidence_store 的过滤策略。
# strict = 要求 observed_at 与来源已核实（点对点可追溯，能证明「决策时确实看得到这条」）；
# diagnostic = 放开这两项，只保留双时间里的 published_at 过滤。
DIAGNOSTIC_POLICY = {'require_observed_at': False, 'require_verified': False}


def policy_for_mode(mode: str) -> dict | None:
    if mode == 'strict':
        return None  # evidence_store 的默认策略就是严格层
    if mode == 'diagnostic':
        return dict(DIAGNOSTIC_POLICY)
    raise ValueError(f'UNKNOWN_EVIDENCE_MODE:{mode}')

# 进入模型 prompt 的正文长度上限。正文同时进 prompt 与落库，必须封顶；
# `content_hash` 始终对**全文**计算，截断与否另用 summary_truncated 标记，保证可审计。
MAX_SUMMARY_CHARS = 2000


def entry_decision_cutoff(session) -> str:
    """决策信息截止时刻 = 信号日 t 收盘。晚于此刻可见的信息不得进入证据包。"""
    return market_close(session).isoformat()


def entry_response_deadline(exec_session) -> str:
    """模型回复截止 = 执行日开盘前 10 分钟（最晚仍可执行的时刻，非决策时刻）。"""
    return market_time(exec_session, 9, 20).isoformat()


def _parse_iso(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def _norm_events(events, as_of_dt, *, require_observed_at: bool = True):
    """校验事件双时间，返回 (可用事件列表, 被丢弃计数)。

    `require_observed_at=False`（诊断模式）时才允许缺 `observed_at`：那表示「知道何时
    公布、但无法证明我们何时首次看到」。严格层下这必须丢弃 —— 否则点对点可得性无从证明。
    两者必须一致：`evidence_store.select_visible` 按同一策略放行，这一层再按相反策略丢弃
    会让诊断模式表面上被接受、实际全部流失。
    """
    usable, dropped = [], 0
    for e in events or []:
        published = _parse_iso(e.get('published_at'))
        observed = _parse_iso(e.get('observed_at'))
        if published is None or (require_observed_at and observed is None):
            dropped += 1
            continue
        if published > as_of_dt or (observed is not None and observed > as_of_dt):
            dropped += 1
            continue
        summary = str(e.get('summary') or '')
        content_hash = e.get('content_hash') or digest(summary)
        usable.append({
            # 内容寻址：不用列表下标，否则前面少一条事件会让后面所有 id 错位，
            # 已记录的 VETO 再也无法复验。
            'evidence_id': e.get('evidence_id') or stable_id(
                'evidence', e.get('source', ''), content_hash,
                e.get('published_at'), e.get('observed_at')),
            'security_id': e.get('security_id', ''),
            'source': e.get('source', 'internal:rule'),
            'source_url': e.get('source_url', ''),
            'kind': e.get('kind', 'rule'),
            'event_type': e.get('event_type') or e.get('kind', 'rule'),
            'published_at': e.get('published_at'),
            'observed_at': e.get('observed_at'),
            'cluster_id': e.get('cluster_id', ''),
            # 可读正文：模型必须能看到证据内容才能判断，只留哈希等于让它瞎猜。
            # summary = 可直接阅读的事实摘要；excerpt = 支持该摘要的短原文摘录。
            'title': str(e.get('title') or '')[:MAX_SUMMARY_CHARS],
            'summary': summary[:MAX_SUMMARY_CHARS],
            'excerpt': str(e.get('excerpt') or summary)[:MAX_SUMMARY_CHARS],
            'summary_truncated': len(summary) > MAX_SUMMARY_CHARS,
            'content_hash': content_hash,
        })
    return usable, dropped


def events_from_records(records, security_id, decision_cutoff, *, policy=None, resolver=None,
                        source_version: str = '', price_version: str = ''):
    """用 `evidence_store.build_packet` 的语义产出 entry-veto 事件。

    返回 `(events, exclusions, meta)`。**meta 必须一路带到证据包里**：`evidence_mode`
    决定这批事件是严格级还是诊断级，账本缺了它就分不清一次 VETO 建立在哪种证据上。

    可见性（双时间过滤 + 具名拒绝原因）、簇去重与修订处理全部复用 `evidence_store`，
    不另起一套更弱的实现。正文按 `evidence_id` 从原始记录回联并按 MAX_SUMMARY_CHARS 封顶；
    `build_packet` 本身只保 `content_hash`，不落明文。

    records 须是 `evidence_store.normalize_evidence` 规整过的帧（含 evidence_id）。
    """
    from scripts.evidence.evidence_store import build_packet
    packet, exclusions = build_packet(records, security_id, decision_cutoff,
                                      source_version=source_version,
                                      price_version=price_version,
                                      policy=policy, resolver=resolver)
    by_id = {str(r.get('evidence_id')): r for r in records.to_dict('records')}
    events = []
    for e in packet['events']:
        raw = by_id.get(str(e['evidence_id']), {})
        summary = ''
        for column in ('summary', 'summary_text', 'text', 'headline'):
            if raw.get(column):
                summary = str(raw[column])
                break
        events.append({
            'evidence_id': e['evidence_id'],
            'security_id': str(security_id),
            'source': e.get('source_id'),
            # 来源可核对：设计 §5.1 要求摘要必须有支持它的原文与链接
            'source_url': str(raw.get('source_url_or_archive_path') or ''),
            'kind': e.get('kind'),
            'event_type': str(raw.get('kind') or e.get('kind') or ''),
            'published_at': e.get('published_at'),
            'observed_at': e.get('observed_at'),
            'cluster_id': str(raw.get('source_record_id') or ''),
            'title': str(raw.get('title') or '')[:MAX_SUMMARY_CHARS],
            'summary': summary[:MAX_SUMMARY_CHARS],
            'excerpt': str(raw.get('excerpt') or summary)[:MAX_SUMMARY_CHARS],
            'summary_truncated': len(summary) > MAX_SUMMARY_CHARS,
            'content_hash': e.get('content_hash'),
        })
    reasons = {}
    for item in exclusions:
        reasons[item['reason']] = reasons.get(item['reason'], 0) + 1
    meta = {
        'evidence_mode': packet.get('evidence_mode'),
        'source_packet_hash': packet.get('packet_hash'),
        'included_event_count': len(events),
        'exclusion_count': len(exclusions),
        'exclusion_reasons': reasons,
    }
    return events, exclusions, meta


def build_entry_packet(opportunity, quote, events, fundamentals, as_of,
                       model_knowledge_cutoff=None, evidence=None,
                       identity=None, market_context=None, fetch_status='OK') -> dict:
    """构建入场否决用的冻结证据包（设计 §5.1 契约）。

    opportunity: Opportunity（含 security_id / opportunity_id()）。
    quote: {'price'(int 微美元), 'observed_at'(ISO)}。
    events: [{'summary','source','published_at','observed_at','kind','content_hash'}]。
    fundamentals: {'earnings_date', 'revenue_change', ...}（可选新闻）。
    as_of: 决策截止时刻（ISO，带时区）。
    model_knowledge_cutoff: 模型训练数据截止时刻（ISO）。写进包内一并冻结 —— 决策时点
        早于它时，as-of 证据过滤修不好泄漏，必须以显式字段披露而非默认无事。
    evidence: `events_from_records` 的 meta（证据等级/来源包哈希/排除统计）。一并冻结进
        包，使账本分得清一次 VETO 建立在严格级还是诊断级证据上。
    """
    as_of_dt = _parse_iso(as_of)
    if as_of_dt is None:
        raise ValueError('AS_OF_INVALID')
    as_of_iso = as_of_dt.isoformat()

    # 规则原计划：由规则侧冻结，模型只读（设计 §4：模型不自行计算 ATR/下单量/止损）
    rule_plan = {
        'parent_strategy_id': getattr(opportunity, 'parent_strategy_id', '') or '',
        'parent_version': opportunity.parent_version,
        'entry_rule': opportunity.entry_rule,
        'rule_reason_codes': list(getattr(opportunity, 'rule_reason_codes', ()) or ()),
        'signal_session': opportunity.signal_session,
        'planned_execution_session': opportunity.planned_execution_session,
        'stop_reference': dict(getattr(opportunity, 'stop_reference', {}) or {}),
        'exit_policy_id': opportunity.exit_policy_id,
        'decision_deadline': getattr(opportunity, 'decision_deadline', '') or '',
    }

    # 关键数据（BLOCK 级，设计 §5.3：关键行情、股票身份或规则计划缺失/无效）
    quote_price = (quote or {}).get('price')
    quote_observed = _parse_iso((quote or {}).get('observed_at'))
    critical_missing = []
    if quote_price is None or quote_price <= 0:
        critical_missing.append('quote.price')
    if quote_observed is None:
        critical_missing.append('quote.observed_at')
    if not getattr(opportunity, 'security_id', None):
        critical_missing.append('security_id')
    if not (rule_plan['parent_version'] and rule_plan['entry_rule']
            and rule_plan['planned_execution_session']):
        critical_missing.append('rule_plan')

    # 证据等级决定双时间过滤的严格程度：诊断模式允许缺 observed_at（同一策略已由
    # evidence_store.select_visible 施加过一次，这里保持一致，不能反过来再卡一次）
    evidence = evidence or {}
    require_observed_at = evidence.get('evidence_mode') != 'diagnostic'
    usable_events, dropped = _norm_events(events, as_of_dt,
                                          require_observed_at=require_observed_at)
    fundamentals = fundamentals or {}

    if critical_missing or (quote_observed is not None and quote_observed > as_of_dt):
        quality = 'BLOCK'
    elif not usable_events and not fundamentals:
        quality = 'LLM_INSUFFICIENT'
    else:
        quality = 'OK'

    packet = {
        'schema_version': 'entry-veto-v1',
        'opportunity_id': opportunity.opportunity_id(),
        'security_id': opportunity.security_id,
        'parent_version': opportunity.parent_version,
        # 设计 §5.1：identity 防同名公司事件误配
        'identity': {'security_id': opportunity.security_id, **(identity or {})},
        # 规则原计划：模型只读，不自行计算 ATR/下单量/止损
        'rule_plan': rule_plan,
        'market_context': {
            'price': quote_price, 'price_unit': 'micro_usd',
            'observed_at': quote.get('observed_at') if quote else None,
            **(market_context or {}),
        },
        'events': usable_events,
        'fundamentals': fundamentals,
        'data_quality': {'level': quality, 'critical_missing': critical_missing,
                         'dropped_event_count': dropped, 'fetch_status': fetch_status,
                         'evidence_mode': evidence.get('evidence_mode')},
        'as_of': as_of_iso,
        'model_knowledge_cutoff': model_knowledge_cutoff,
        'evidence': evidence,
        'provenance': {
            'as_of': as_of_iso,
            'market_snapshot_id': getattr(opportunity, 'market_snapshot_id', '') or '',
            'evidence_source_packet_hash': evidence.get('source_packet_hash'),
            'model_knowledge_cutoff': model_knowledge_cutoff,
            'packet_schema_version': 'entry-packet-v2',
        },
    }
    packet['packet_id'] = stable_id('entry_packet', packet)
    return packet
