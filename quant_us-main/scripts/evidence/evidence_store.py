#!/usr/bin/env python3
"""历史证据快照核心库（技术设计 §3.1–§3.4）。

双时间过滤：事实只有在 `published_at <= decision_cutoff` 且 `observed_at <= decision_cutoff`
且通过其他质量门时，才进入严格历史回放；被拒绝的事实逐条记录原因。所有时间转 UTC 保存。
本模块不做任何网络或模型调用。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo('America/New_York')
UTC = timezone.utc

REQUIRED_COLUMNS = ('evidence_id', 'security_id', 'symbol_as_published', 'kind', 'source_id',
                    'source_record_id', 'source_url_or_archive_path', 'event_at', 'published_at',
                    'observed_at', 'ingested_at', 'version_id', 'supersedes_id', 'content_hash',
                    'summary_hash', 'quality_status', 'availability_proof', 'license_tag')

REJECTION_REASONS = ('FUTURE_PUBLICATION', 'FUTURE_OBSERVATION', 'OBSERVED_AT_UNPROVEN',
                     'REVISED_AFTER_CUTOFF', 'SOURCE_UNVERIFIED', 'LICENSE_RESTRICTED',
                     'SYMBOL_AMBIGUOUS', 'STALE', 'CONFLICT',
                     'DUPLICATE_EVENT')   # 扩展：同一源事件的重复转载，合并计一次

DEFAULT_POLICY = {
    'require_observed_at': True,        # 严格层；诊断层可置 False
    'require_verified': True,
    'allowed_license_tags': ('research-retention-allowed', 'research-use-only', 'public'),
    'stale_days': None,                 # None = 不做过期过滤
}


# --------------------------------------------------------------------------- 时间

def market_time(session, hour, minute=0, tz=NY):
    """把纽约本地时间转成 UTC ISO；夏令时由其自动处理。"""
    stamp = pd.Timestamp(session).tz_localize(tz) + timedelta(hours=hour, minutes=minute)
    return stamp.tz_convert(UTC)


def market_close(session, half_day=False):
    """交易日收盘时刻（16:00 ET；半日市 13:00 ET）的 UTC 时刻。"""
    return market_time(session, 13 if half_day else 16, 0)


def _to_utc(value):
    if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == '':
        return None
    stamp = pd.to_datetime(value, errors='coerce', utc=True)
    if pd.isna(stamp):
        raise ValueError(f'INVALID_TIME:{value}')
    return stamp.tz_convert(UTC).isoformat()


def to_utc_series(values) -> pd.Series:
    """逐元素解析成带时区的 datetime 列。

    **不要用 `pd.to_datetime(series)` 直接解析原始列**：混合格式（有的带微秒、有的不带）
    会让 pandas 按多数派推断，把少数派整列判成 `NaT` —— **静默丢数据**。实测：
    `pd.to_datetime(pd.Series(['2026-09-11T10:05:05.447198+00:00', '2026-08-04T20:00:00+00:00']))`
    返回 `[正确, NaT]`。逐元素解析没有格式推断这一步。
    """
    stamps = []
    for value in values:
        try:
            stamps.append(pd.Timestamp(_to_utc(value)) if _to_utc(value) else pd.NaT)
        except ValueError:
            stamps.append(pd.NaT)
    return pd.Series(stamps, dtype='datetime64[ns, UTC]')


def _hash_text(text: str) -> str:
    return hashlib.sha256(str(text).encode('utf-8')).hexdigest()


def content_hash(payload) -> str:
    """对原始内容（或规范化字符串）取 SHA-256。"""
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return _hash_text(payload)


def _normalize_digest(value) -> str:
    """保留来源已计算的 SHA-256；fixture/适配器给原文时只计算一次。"""
    if value is None or pd.isna(value) or str(value).strip() == '':
        return ''
    raw = str(value).strip()
    return raw.lower() if re.fullmatch(r'[0-9a-fA-F]{64}', raw) else content_hash(raw)


def evidence_id_for(record: dict) -> str:
    """稳定证据 ID。"""
    blob = json.dumps({k: record.get(k) for k in
                       ('source_id', 'source_record_id', 'version_id', 'content_hash',
                        'published_at', 'observed_at')},
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'ev_' + _hash_text(blob)[:24]


def normalize_evidence(frame: pd.DataFrame, *, ingested_at=None) -> pd.DataFrame:
    """把来源原始记录标准化为证据 Schema，并补 evidence_id。"""
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns and c != 'evidence_id']
    if missing:
        raise ValueError('证据缺字段: ' + ','.join(sorted(missing)))
    d = frame.copy()
    for column in ('event_at', 'published_at', 'observed_at'):
        d[column] = d[column].map(_to_utc)
    d['ingested_at'] = d['ingested_at'].map(_to_utc) if 'ingested_at' in d and \
        d['ingested_at'].notna().any() else (_to_utc(ingested_at) or datetime.now(UTC).isoformat())
    d['supersedes_id'] = d['supersedes_id'].where(d['supersedes_id'].notna(), None)
    d['content_hash'] = d['content_hash'].map(_normalize_digest)
    d['summary_hash'] = d['summary_hash'].map(_normalize_digest)
    d.loc[d['summary_hash'].eq(''), 'summary_hash'] = d['content_hash']
    d['evidence_id'] = [evidence_id_for(r) for r in d.to_dict('records')]
    return d[list(REQUIRED_COLUMNS)]


def validate_evidence(frame: pd.DataFrame) -> list:
    errors = []
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        return ['EVIDENCE_MISSING_COLUMNS:' + ','.join(sorted(missing))]
    if frame['evidence_id'].duplicated().any():
        errors.append('DUPLICATE_EVIDENCE_ID')
    pub = to_utc_series(frame['published_at'])
    obs = to_utc_series(frame['observed_at'])
    if (obs.notna() & pub.notna() & (obs < pub)).any():
        errors.append('OBSERVED_BEFORE_PUBLISHED')     # 首次可见不能早于公开
    if frame['ingested_at'].map(lambda v: v is None or str(v).strip() == '').any():
        errors.append('INGESTED_AT_MISSING')
    if frame['availability_proof'].map(
            lambda v: pd.isna(v) or str(v).strip().lower() in ('', 'nan', 'none')).any():
        errors.append('AVAILABILITY_PROOF_MISSING')
    return sorted(set(errors))


# --------------------------------------------------------------------------- 选择

def _resolve(record, security_id, resolver):
    """判断证据是否属于该 security_id；返回 (matched, ambiguous)。"""
    symbol = record.get('symbol_as_published')
    if resolver is not None and symbol:
        outcome = resolver(str(symbol), record.get('published_at'))
        if outcome.get('status') == 'ambiguous':
            return False, True
        if outcome.get('status') == 'resolved':
            resolved = str(outcome['security_id'])
            recorded = str(record.get('security_id') or '')
            if recorded and recorded != resolved:
                return False, True
            return resolved == str(security_id), False
        return False, False
    return str(record.get('security_id')) == str(security_id), False


def select_visible(records: pd.DataFrame, security_id, decision_cutoff, *, policy=None,
                   resolver=None) -> tuple:
    """返回 (可见证据列表, 拒绝记录列表)。拒绝原因见 REJECTION_REASONS。"""
    policy = {**DEFAULT_POLICY, **(policy or {})}
    cutoff = pd.to_datetime(decision_cutoff, errors='coerce', utc=True)
    if pd.isna(cutoff):
        raise ValueError('INVALID_DECISION_CUTOFF')
    visible, exclusions = [], []
    for record in records.to_dict('records'):
        matched, ambiguous = _resolve(record, security_id, resolver)
        if ambiguous:
            exclusions.append({'evidence_id': record.get('evidence_id'), 'reason': 'SYMBOL_AMBIGUOUS'})
            continue
        if not matched:
            continue                                   # 与目标证券无关，不计入拒绝
        published = pd.to_datetime(record.get('published_at'), errors='coerce', utc=True)
        observed = pd.to_datetime(record.get('observed_at'), errors='coerce', utc=True)
        if pd.isna(published) or published > cutoff:
            exclusions.append({'evidence_id': record.get('evidence_id'), 'reason': 'FUTURE_PUBLICATION'})
            continue
        if pd.isna(observed):
            if policy['require_observed_at']:
                exclusions.append({'evidence_id': record.get('evidence_id'),
                                   'reason': 'OBSERVED_AT_UNPROVEN'})
                continue
        elif observed > cutoff:
            exclusions.append({'evidence_id': record.get('evidence_id'), 'reason': 'FUTURE_OBSERVATION'})
            continue
        if policy['require_verified'] and str(record.get('quality_status')) != 'verified':
            exclusions.append({'evidence_id': record.get('evidence_id'), 'reason': 'SOURCE_UNVERIFIED'})
            continue
        if str(record.get('license_tag')) not in policy['allowed_license_tags']:
            exclusions.append({'evidence_id': record.get('evidence_id'), 'reason': 'LICENSE_RESTRICTED'})
            continue
        if policy.get('stale_days'):
            if published < cutoff - timedelta(days=int(policy['stale_days'])):
                exclusions.append({'evidence_id': record.get('evidence_id'), 'reason': 'STALE'})
                continue
        visible.append(record)
    return visible, exclusions


def _cluster_and_version(visible):
    """同一源事件只算一次：完全相同内容→DUPLICATE_EVENT；修订版本→REVISED_AFTER_CUTOFF。"""
    kept, exclusions = [], []
    clusters = {}
    for record in visible:
        key = str(record.get('source_record_id') or record.get('content_hash'))
        clusters.setdefault(key, []).append(record)
    for key, members in clusters.items():
        hashes = {str(m.get('content_hash')) for m in members}
        ordered = sorted(members, key=lambda m: str(m.get('observed_at') or m.get('published_at')))
        if len(hashes) == 1:                       # 重复转载 → 合并为一个事件
            kept.append(ordered[-1])
            for member in ordered[:-1]:
                exclusions.append({'evidence_id': member.get('evidence_id'),
                                   'reason': 'DUPLICATE_EVENT'})
            continue
        stamps = [str(m.get('observed_at') or m.get('published_at')) for m in ordered]
        if len(set(stamps)) < len(stamps):         # 可见版本同刻但内容不同 → 冲突
            for member in ordered:
                exclusions.append({'evidence_id': member.get('evidence_id'), 'reason': 'CONFLICT'})
            continue
        kept.append(ordered[-1])                   # 决策时点可见的最新修订
        for member in ordered[:-1]:
            exclusions.append({'evidence_id': member.get('evidence_id'),
                               'reason': 'REVISED_AFTER_CUTOFF'})
    return kept, exclusions


def _iso(value):
    """把（可能是 Timestamp 的）时间规整为 UTC ISO 字符串，便于稳定序列化。"""
    if value is None:
        return None
    stamp = pd.to_datetime(value, errors='coerce', utc=True)
    return None if pd.isna(stamp) else stamp.tz_convert(UTC).isoformat()


def build_packet(records: pd.DataFrame, security_id, decision_cutoff, *,
                 source_version='', price_version='', policy=None, resolver=None,
                 price_context=None) -> tuple:
    """构建冻结证据包：返回 (packet, exclusions)。事件按簇去重，避免重复计数。"""
    visible, exclusions = select_visible(records, security_id, decision_cutoff,
                                         policy=policy, resolver=resolver)
    kept, version_exclusions = _cluster_and_version(visible)
    exclusions += version_exclusions
    cutoff = pd.to_datetime(decision_cutoff, errors='coerce', utc=True).isoformat()
    events = sorted(({'evidence_id': r['evidence_id'], 'kind': r.get('kind'),
                      'event_at': _iso(r.get('event_at')),
                      'published_at': _iso(r.get('published_at')),
                      'observed_at': _iso(r.get('observed_at')),
                      'source_id': r.get('source_id'),
                      'content_hash': r.get('content_hash')} for r in kept),
                    key=lambda e: (str(e['published_at']), str(e['evidence_id'])))
    packet = {
        'security_id': str(security_id),
        'decision_cutoff': cutoff,
        'source_version': source_version,
        'price_version': price_version,
        'event_clusters': len({str(r.get('source_record_id') or r.get('content_hash')) for r in kept}),
        'events': events,
        'price_context': price_context or {},
        'evidence_mode': 'strict' if (policy or {}).get('require_observed_at', True)
                         and (policy or {}).get('require_verified', True) else 'diagnostic',
        'exclusions': sorted(exclusions, key=lambda e: (str(e['reason']), str(e['evidence_id']))),
    }
    packet['packet_hash'] = packet_hash(packet)
    return packet, exclusions


def packet_hash(packet: dict) -> str:
    """packet 稳定哈希；任一证据内容/时间变化即变化。"""
    body = {k: v for k, v in packet.items() if k != 'packet_hash'}
    return 'pkt_' + _hash_text(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                          separators=(',', ':')))


def validate_labels(labels: list, packets: dict) -> list:
    """标签必须一对一属于冻结 packet，且引用的 evidence_id 必须在该 packet 内。"""
    errors = []
    seen = set()
    for label in labels:
        setup_id = str(label.get('setup_id'))
        packet = packets.get(setup_id)
        if packet is None:
            errors.append(f'PACKET_MISSING:{setup_id}')
            continue
        if setup_id in seen:
            errors.append(f'DUPLICATE_LABEL:{setup_id}')
        seen.add(setup_id)
        if str(label.get('packet_hash')) != str(packet.get('packet_hash')):
            errors.append(f'PACKET_HASH_MISMATCH:{setup_id}')
        if packet_hash(packet) != packet.get('packet_hash'):
            errors.append(f'PACKET_TAMPERED:{setup_id}')
        allowed = {e['evidence_id'] for e in packet.get('events', [])}
        for cited in label.get('cited_evidence_ids') or []:
            if cited not in allowed:
                errors.append(f'CITATION_OUTSIDE_PACKET:{setup_id}:{cited}')
        if str(label.get('llm_decision')) == 'missing':
            errors.append(f'MISSING_TREATED_AS_DECISION:{setup_id}')
    for setup_id in sorted(set(packets) - seen):
        errors.append(f'LABEL_MISSING:{setup_id}')
    return errors


def audit_source(records: pd.DataFrame, sample=100) -> dict:
    """来源可用性审计（§3.3）：时间字段、时区、修订可见性、批量回填、许可。"""
    d = records.head(sample).copy()
    pub = to_utc_series(d['published_at'])
    obs = to_utc_series(d['observed_at'])
    ing = to_utc_series(d['ingested_at'])
    backfilled = int((pub.notna() & ing.notna() &
                      ((ing - pub) > pd.Timedelta(days=365 * 5)).fillna(False)).sum())
    return {
        'rows_sampled': int(len(d)),
        'published_at_missing': int(pub.isna().sum()),
        'observed_at_missing': int(obs.isna().sum()),
        'timezone_explicit': bool(pub.notna().all()),      # 能解析为 UTC 即视为时区明确
        'revisions_present': bool(d['supersedes_id'].map(
            lambda v: v is not None and str(v).strip() != '').any()),
        'bulk_backfill_rows': backfilled,
        'retention_allowed': bool(d['license_tag'].map(
            lambda v: str(v) in DEFAULT_POLICY['allowed_license_tags']).all()),
    }
