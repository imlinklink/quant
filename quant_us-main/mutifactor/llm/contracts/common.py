"""公共领域模型与版本信息（PR4，技术设计 §4）。

集中定义：
  - 版本常量（feature/rule/permission/prompt/schema）；
  - EvidenceItem 归一与 quality 判定辅助；
  - 公共 JSON 片段（claims 等）。
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional

REASON_REGISTRY_VERSION = 'reason-v2'
FEATURE_VERSION = 'feature-v2'
RULE_VERSION = 'rule-v2'
PERMISSION_VERSION = 'permission-v2'

# Evidence quality 等级
QUALITY_GOOD = 'good'
QUALITY_PARTIAL = 'partial'
QUALITY_STALE = 'stale'
QUALITY_INVALID = 'invalid'


def utc(value=None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('时间必须带时区')
    return value.astimezone(timezone.utc).isoformat()


def build_evidence_item(*, evidence_id: str, subject_code: Optional[str],
                        kind: str, source: str, source_grade: int,
                        summary: str,
                        observed_at, published_at=None, effective_at=None,
                        expires_at=None, cluster_id: Optional[str] = None,
                        content_hash: Optional[str] = None,
                        quality: str = QUALITY_GOOD,
                        quality_reasons=None, payload: Optional[Dict] = None) -> Dict[str, Any]:
    """构造统一 EvidenceItem（技术设计 §4.2）。"""
    observed = utc(observed_at)
    pub = utc(published_at) if published_at else None
    eff = utc(effective_at) if effective_at else observed
    exp = utc(expires_at) if expires_at else None
    if eff > observed:
        raise ValueError('effective_at 不能晚于 observed_at')
    return {
        'evidence_id': evidence_id,
        'subject_code': subject_code,
        'kind': kind,
        'source': source,
        'source_grade': source_grade,
        'summary': summary,
        'published_at': pub,
        'observed_at': observed,
        'effective_at': eff,
        'expires_at': exp,
        'cluster_id': cluster_id,
        'content_hash': content_hash,
        'quality': quality,
        'quality_reasons': list(quality_reasons or []),
        'payload': payload or {},
    }


def claim(text: str, evidence_ids, claim_type: str) -> Dict[str, Any]:
    if claim_type not in ('fact', 'inference', 'counterevidence'):
        raise ValueError(f'非法 claim_type: {claim_type}')
    return {'text': text, 'claim_type': claim_type, 'evidence_ids': list(evidence_ids)}
