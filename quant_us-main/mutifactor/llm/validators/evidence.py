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


def downgrade_nonverbatim_facts(raw: Dict, index: Dict[str, Dict],
                                fields=('facts', 'inferences', 'counterevidence')) -> Dict:
    """把 text 与所引摘要**不全等**的 `fact` 降级为 `inference`；返回副本。

    为什么要降级而不是让它去失败：`validate_claims` 的 `fact 未逐字匹配` 是 fail-closed 的
    （不允许把释义当事实），**这条判据本身是对的**；但直接判失败会让**整条决策作废**。
    实测：真实模型引的是 6000 字市场日报里的**片段**（"📉 逆势：LITE −2.81% · CRWV −4.16%"），
    与整段摘要全等**在长度上就不可能**，于是每个 fact 都失败 ⇒ 整批降级成 ABSTAIN，
    闭环永远走不通。夹具里摘要是 `'测试用缺口声明'` 这种一句话，所以**测试全都看不见**。

    降级只**降低声明强度**：不动证据引用、不动置信度、不动权限 —— 与 selection 的
    `normalize_selection_output` 同一做法（那条 2026-09 就修过，entry/position 漏了这一课）。

    判据取"**每一条**所引摘要都全等"而不是"至少一条"：`validate_claims` 是**逐条 eid**
    比对的，引两条就得同时等于两条 —— 按"至少一条"保留仍会被判失败，等于没修。
    """
    import copy
    out = copy.deepcopy(raw)
    for field in fields:
        for claim in out.get(field) or []:
            if claim.get('claim_type') != 'fact':
                continue
            summaries = [index[eid].get('summary') for eid in claim.get('evidence_ids') or []
                         if eid in index]
            if not summaries or any(s != claim.get('text') for s in summaries):
                claim['claim_type'] = 'inference'
    return out
