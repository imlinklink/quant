"""冻结 Evidence Packet（阶段 F1）。

LLM 只解释、不重算：所有指标由程序计算/传入，事实可回到输入证据。
每个事实有 evidence_id / source / published_at / observed_at / content_hash；
缺失/陈旧/冲突/未来时间显式标记，不写正常值掩盖。可复现：同一输入产出同一 packet_id。
"""
import math
import time
from datetime import datetime, timezone

from mutifactor.llm.trade_review import evidence
from scripts.live_trading.decision_ledger.event_store import stable_id, utc

# 来源等级：程序侧固定，LLM 不得自行升级
SOURCE_GRADE = {'rule': 1, 'news': 2, 'filing': 3, 'analyst': 2, 'macro': 2}


def _parse_iso(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def build_data_quality(quote, events, fundamentals, now):
    """确定性数据质量检查：缺失 / 陈旧 / 未来 / 来源等级，显式标记。"""
    now_dt = _parse_iso(now) or datetime.now(timezone.utc)
    checks = {'missing': [], 'stale': [], 'future': [], 'conflict': []}

    if not quote or not quote.get('price'):
        checks['missing'].append('quote.price')
    if quote and quote.get('observed_at'):
        q = _parse_iso(quote['observed_at'])
        if q and q > now_dt:
            checks['future'].append('quote.observed_at')
        elif q and (now_dt - q).total_seconds() > 86400:
            checks['stale'].append('quote.observed_at')

    for i, e in enumerate(events or []):
        ts = e.get('published_at') or e.get('observed_at')
        if not ts:
            checks['missing'].append(f'events[{i}].time')
            continue
        t = _parse_iso(ts)
        if t and t > now_dt:
            checks['future'].append(f'events[{i}].time')

    if fundamentals:
        for k in ('revenue_change', 'earnings_date', 'valuation_date'):
            if fundamentals.get(k) is None:
                checks['missing'].append(f'fundamentals.{k}')

    return {
        'ok': not any(checks.values()),
        'checks': checks,
        'source_grade': SOURCE_GRADE,
        'missing_count': len(checks['missing']),
        'stale_count': len(checks['stale']),
        'future_count': len(checks['future']),
    }


def build_evidence_packet(code, *, name=None, market=None, sector=None, risk_group=None,
                          quote=None, strategy=None, events=None, fundamentals=None,
                          account=None, now=None):
    """生成冻结的 evidence_packet。

    参数：
      quote: {'price', 'observed_at', 'ret_1d'?, 'ret_5d'?, 'ret_20d'?, 'vol_ratio'?, 'atr'?, 'trend'?}
      strategy: 各策略通过/原始分数/未通过原因/入场价/止损/目标/RR（程序计算）
      events: 事件列表，可含 evidence 对象或 {'summary','source','published_at','observed_at','kind'}
      fundamentals: {'revenue_change','earnings_date','valuation_date', ...}
      account: 脱敏账户约束（是否已持仓 / 行业风险占用 / 持仓数量上限），不含账号/现金/密钥

    可复现：同一输入（含 now、事件时间戳）产出同一 packet_id。
    """
    now = now if now is not None else time.time()
    events = list(events or [])

    evidence_items = []
    for e in events:
        if isinstance(e, dict) and e.get('evidence_id'):
            evidence_items.append(e)
        else:
            evidence_items.append(evidence(
                str(e.get('summary', '')), str(e.get('source', 'internal:rule')),
                e.get('observed_at', now), e.get('published_at'),
                e.get('cluster_id'), str(e.get('kind', 'rule'))))

    data_quality = build_data_quality(quote, evidence_items, fundamentals, now)

    packet = {
        'code': str(code),
        'identity': {'code': str(code), 'name': name, 'market': market,
                     'sector': sector, 'risk_group': risk_group},
        'quote': quote or {},
        'strategy': strategy or {},
        'events': evidence_items,
        'fundamentals': fundamentals or {},
        'account': account or {},
        'data_quality': data_quality,
        'as_of': utc(now),
    }
    packet['packet_id'] = stable_id('evidence_packet', packet)
    return packet
