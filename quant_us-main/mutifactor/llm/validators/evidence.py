"""证据引用/时间/claim 校验（PR4，技术设计 validators/evidence.py）。

所有角色输出统一走这里：引用必须存在、不得跨股票、不得未来/过期/无效；
fact 必须逐字匹配 evidence.summary；counterevidence 或 missing_information 至少一项。
"""
from typing import Dict, List


def _evidence_index(evidence_items: List[Dict]) -> Dict[str, Dict]:
    return {e['evidence_id']: e for e in evidence_items if e.get('evidence_id')}


def validate_claims(claims: List[Dict], index: Dict[str, Dict],
                    subject_code: str, role: str, as_of,
                    allowed_subject_codes=None) -> List[str]:
    """校验一列 claim。返回错误列表；空列表 = 通过。"""
    errors = []
    as_of = str(as_of)
    for claim in claims:
        ctype = claim.get('claim_type', 'inference')
        ids = claim.get('evidence_ids') or []
        if not ids:
            errors.append(f'{ctype} 缺少证据引用: {claim.get("text", "")[:40]}')
            continue
        for eid in ids:
            ev = index.get(eid)
            if ev is None:
                errors.append(f'引用不存在: {eid}')
                continue
            # subject 隔离：股票级 claim 只允许当前股票、MARKET，以及 packet 声明的板块身份。
            # 角色清单必须**含全部股票级角色**：漏掉一个就等于对该角色静默关闭隔离。
            if role in ('selection', 'entry', 'position', 'portfolio'):
                subj = ev.get('subject_code')
                allowed = {subject_code, 'MARKET'} | set(allowed_subject_codes or ())
                if not subj:
                    errors.append(f'证据缺少归属: {eid}')
                elif subj not in allowed:
                    errors.append(f'跨股票引用: {eid} -> {subj}')
            if ev.get('quality') in ('invalid', 'stale'):
                errors.append(f'引用 {ev.get("quality")} 证据: {eid}')
            if ev.get('expires_at') and str(ev['expires_at']) < as_of:
                errors.append(f'引用已过期证据: {eid}')
            # 未来证据（时间无泄漏）：effective/published/observed 任一早于 as_of 即拒绝
            for field in ('effective_at', 'published_at', 'observed_at'):
                ts = ev.get(field)
                if ts and str(ts) > as_of:
                    errors.append(f'未来证据({field}): {eid}')
                    break
            if ctype == 'fact' and claim.get('text') != ev.get('summary'):
                errors.append(f'fact 未逐字匹配证据摘要: {eid}')
    return errors


def require_counterevidence_or_missing(claims: List[Dict], missing: List[str]) -> List[str]:
    """技术设计 §4.3：counterevidence 至少一项，或明确 missing_information。"""
    if not missing and not any(c.get('claim_type') == 'counterevidence' for c in claims):
        return ['须提供反对证据或明确资料缺口']
    return []
