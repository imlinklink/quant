"""Evidence Packet（PR5）：入场否决用，自包含、确定性，不依赖 mutifactor。

双时间校验：每条证据的 `published_at` 与 `observed_at` 必须分别合法、带时区、≤ as_of；
关键行情（quote.price / quote.observed_at / security_id）缺失或未来 → BLOCK；
可选新闻不足/陈旧 → LLM_INSUFFICIENT（模型 ABSTAIN 但规则可继续）。
证据等级由采集器确定，不由模型输出覆盖。
"""
from __future__ import annotations

from datetime import datetime, timezone

from scripts.live_trading.decision_ledger.event_store import digest, stable_id

QUALITY_LEVELS = ('BLOCK', 'LLM_INSUFFICIENT', 'OK')


def _parse_iso(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def _norm_events(events, as_of_dt):
    """校验事件双时间，返回 (可用事件列表, 被丢弃计数)。"""
    usable, dropped = [], 0
    for i, e in enumerate(events or []):
        published = _parse_iso(e.get('published_at'))
        observed = _parse_iso(e.get('observed_at'))
        if published is None or observed is None:
            dropped += 1
            continue
        if published > as_of_dt or observed > as_of_dt:
            dropped += 1
            continue
        usable.append({
            'evidence_id': e.get('evidence_id') or stable_id('evidence', i, e.get('source')),
            'source': e.get('source', 'internal:rule'),
            'kind': e.get('kind', 'rule'),
            'published_at': e.get('published_at'),
            'observed_at': e.get('observed_at'),
            'content_hash': e.get('content_hash', digest(e.get('summary', ''))),
        })
    return usable, dropped


def build_entry_packet(opportunity, quote, events, fundamentals, as_of) -> dict:
    """构建入场否决用的冻结证据包。

    opportunity: Opportunity（含 security_id / opportunity_id()）。
    quote: {'price'(int 微美元), 'observed_at'(ISO)}。
    events: [{'summary','source','published_at','observed_at','kind','content_hash'}]。
    fundamentals: {'earnings_date', 'revenue_change', ...}（可选新闻）。
    as_of: 决策截止时刻（ISO，带时区）。
    """
    as_of_dt = _parse_iso(as_of)
    if as_of_dt is None:
        raise ValueError('AS_OF_INVALID')
    as_of_iso = as_of_dt.isoformat()

    # 关键数据（BLOCK 级）
    quote_price = (quote or {}).get('price')
    quote_observed = _parse_iso((quote or {}).get('observed_at'))
    critical_missing = []
    if quote_price is None or quote_price <= 0:
        critical_missing.append('quote.price')
    if quote_observed is None:
        critical_missing.append('quote.observed_at')
    if not getattr(opportunity, 'security_id', None):
        critical_missing.append('security_id')

    usable_events, dropped = _norm_events(events, as_of_dt)
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
        'quote': {'price': quote_price, 'observed_at': quote.get('observed_at') if quote else None},
        'events': usable_events,
        'fundamentals': fundamentals,
        'data_quality': {'level': quality, 'critical_missing': critical_missing,
                         'dropped_event_count': dropped},
        'as_of': as_of_iso,
    }
    packet['packet_id'] = stable_id('entry_packet', packet)
    return packet
