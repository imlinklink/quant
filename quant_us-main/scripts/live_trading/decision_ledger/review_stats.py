"""Review 的统计构造器（设计 §6.5 的输入侧）。

Review 的输入是**程序算好的统计**，不是行情判断。本模块把账本里的
决策 / 应用 / 结果按 §6.5 要求分层，产出 `sample_groups` 与 `role_stats`，
供 `build_review_packet` 冻结进包。

三条纪律：

1. **只统计已结算的结果**（`data_quality='good'`）。未成熟的行不进统计 —— 把
   `pending_future_bars` 当成 0 收益会把"还没发生"读成"没有收益"。
2. **关联不上的结果行如实报出**（`unjoined_outcomes`）。账本里存在 `decision_id`
   不是 `decision_*` 形状的结果行（研究批次直接以批次 id 落账），它们关联不到运行表，
   因而拿不到角色与动作。静默丢掉会让样本数看起来比实际更"干净"。
3. **算不出的分层维度显式标注**（`unavailable_dimensions`）。§6.5 要求按证据来源、
   动作、**市场状态**和风险组分层；其中"市场状态"在账本里没有对应字段，故不出现在
   分层键里，并在包里明确标注 —— 而不是杜撰一个状态标签。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

# §6.5 要求的分层维度 → 账本里是否有对应字段
UNAVAILABLE_DIMENSIONS = {
    'market_state': '账本没有市场状态（regime）字段；不得杜撰标签，故不参与分层',
}


def _con(registry):
    con = sqlite3.connect(str(registry.path))
    con.row_factory = sqlite3.Row
    return con


def _settled_rows(con, horizon: str) -> List[sqlite3.Row]:
    return con.execute(
        "SELECT decision_id, subject_key, label_as_of, return_pct, benchmark_return_pct, "
        "excess_return_pct, mae_pct, mfe_pct FROM decision_outcomes_v2 "
        "WHERE horizon=? AND data_quality='good' AND excess_return_pct IS NOT NULL",
        (horizon,)).fetchall()


def _packet_for(con, decision_id: str) -> Optional[dict]:
    row = con.execute(
        'SELECT s.body FROM decision_snapshots s JOIN llm_decision_runs r '
        'ON r.input_snapshot_id = s.id WHERE r.decision_id=? '
        'ORDER BY s.version DESC LIMIT 1', (decision_id,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def _packet_facets(packet: Optional[dict]) -> dict:
    """从冻结包里取分层所需的两个维度：主要证据来源与风险组。"""
    if not packet:
        return {'evidence_source': 'UNKNOWN', 'risk_group': ''}
    body = packet.get('packet') or packet
    events = body.get('events') or []
    sources: Dict[str, int] = {}
    for event in events:
        source = str(event.get('source') or event.get('source_id') or 'unknown')
        sources[source] = sources.get(source, 0) + 1
    identity = body.get('identity') or {}
    return {'evidence_source': (max(sources, key=sources.get) if sources else 'NONE'),
            'risk_group': str(identity.get('risk_group') or '')}


def _actions(con) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for row in con.execute("SELECT body FROM decision_events "
                           "WHERE event_type='decision_effective_action'"):
        event = json.loads(row[0])
        payload = event.get('payload') or {}
        if event.get('decision_id'):
            out[event['decision_id']] = str(payload.get('effective_action') or '')
    return out


def _mean(values: List[float]) -> Optional[float]:
    return statistics.fmean(values) if values else None


def horizon_availability(registry) -> List[dict]:
    """各期限的可用样本量（可关联 / 不可关联）。

    **期限不能事后挑**：只报"最好的那个期限"等于给自己留了挑肥拣瘦的口子。
    这里把全部期限的可用量列出来，并把选定结果一并写进包，使选择可复现、可复核。
    """
    con = _con(registry)
    try:
        rows = con.execute(
            'SELECT o.horizon, '
            'SUM(CASE WHEN r.decision_id IS NOT NULL THEN 1 ELSE 0 END) AS joined, '
            'SUM(CASE WHEN r.decision_id IS NULL THEN 1 ELSE 0 END) AS unjoined '
            "FROM decision_outcomes_v2 o LEFT JOIN llm_decision_runs r "
            "ON r.decision_id = o.decision_id WHERE o.data_quality='good' "
            'GROUP BY o.horizon ORDER BY o.horizon').fetchall()
        return [{'horizon': r['horizon'], 'joined': r['joined'] or 0,
                 'unjoined': r['unjoined'] or 0} for r in rows]
    finally:
        con.close()


def pick_horizon(availability: List[dict]) -> Optional[str]:
    """取**可关联样本最多**的期限；并列时按设计自身的期限顺序取更短的那个。

    并列的取舍必须**有据可依**：按字符串长度或字母序都是随意的，而期限顺序在设计里
    是有序的（1/3/5/10/20）。取更短的期限也更保守 —— 它离"用长窗把噪声平均掉"更远。
    """
    order = {name: i for i, name in enumerate(('1d', '3d', '5d', '10d', '20d'))}
    usable = [a for a in availability if a['joined'] > 0]
    if not usable:
        return None
    return min(usable, key=lambda a: (-a['joined'], order.get(a['horizon'], len(order)))
               )['horizon']


def sample_groups(registry, *, horizon: str = '5d') -> dict:
    """按 §6.5 的分层产出样本组与每角色的汇总。"""
    con = _con(registry)
    try:
        rows = _settled_rows(con, horizon)
        runs = {r['decision_id']: dict(r) for r in con.execute(
            'SELECT decision_id, role FROM llm_decision_runs')}
        actions = _actions(con)
        buckets: Dict[tuple, List[sqlite3.Row]] = {}
        unjoined = 0
        packet_cache: Dict[str, Optional[dict]] = {}
        for row in rows:
            run = runs.get(row['decision_id'])
            if run is None:
                unjoined += 1
                continue
            if row['decision_id'] not in packet_cache:
                packet_cache[row['decision_id']] = _packet_for(con, row['decision_id'])
            facets = _packet_facets(packet_cache[row['decision_id']])
            key = (run['role'], actions.get(row['decision_id'], ''), facets['evidence_source'],
                   facets['risk_group'])
            buckets.setdefault(key, []).append(row)

        groups = []
        for (role, action, source, group), items in sorted(buckets.items()):
            excess = [float(i['excess_return_pct']) for i in items]
            groups.append({
                'name': f'{role}|{action or "NA"}|{source}|{group or "NA"}',
                'role': role, 'action': action, 'evidence_source': source,
                'risk_group': group, 'n': len(items),
                'mean_excess_pct': _mean(excess),
                'mean_l_return_pct': _mean([float(i['return_pct']) for i in items]),
                'mean_r_return_pct': _mean([float(i['benchmark_return_pct']) for i in items]),
                'mean_mae_pct': _mean([float(i['mae_pct']) for i in items
                                       if i['mae_pct'] is not None]),
                'win_rate': (sum(1 for x in excess if x > 0) / len(excess)),
            })
        return {'horizon': horizon, 'groups': groups,
                'settled_rows': len(rows), 'unjoined_outcomes': unjoined,
                'unavailable_dimensions': dict(UNAVAILABLE_DIMENSIONS)}
    finally:
        con.close()


def role_stats(registry, *, horizon: str = '5d') -> dict:
    """每角色的汇总：样本数、平均超额、回撤代理、单一贡献占比。"""
    con = _con(registry)
    try:
        rows = _settled_rows(con, horizon)
        roles = {r['decision_id']: r['role'] for r in con.execute(
            'SELECT decision_id, role FROM llm_decision_runs')}
        by_role: Dict[str, List[sqlite3.Row]] = {}
        for row in rows:
            role = roles.get(row['decision_id'])
            if role:
                by_role.setdefault(role, []).append(row)
        out = {}
        for role, items in sorted(by_role.items()):
            excess = [float(i['excess_return_pct']) for i in items]
            by_subject: Dict[str, float] = {}
            for item in items:
                by_subject[item['subject_key']] = (by_subject.get(item['subject_key'], 0.0)
                                                   + float(item['excess_return_pct']))
            total = sum(by_subject.values())
            out[role] = {
                'n': len(items),
                'mean_excess_pct': _mean(excess),
                'win_rate': (sum(1 for x in excess if x > 0) / len(excess)),
                'mean_mae_pct': _mean([float(i['mae_pct']) for i in items
                                       if i['mae_pct'] is not None]),
                'distinct_subjects': len(by_subject),
                # 单一贡献占比：|最大贡献标的| / Σ|各标的贡献|。用来回答 §12 的
                # 「不依赖单一证券或单一事件贡献」。分母用绝对值之和，避免正负相消后失真。
                'top_contributor_share': (
                    max(abs(v) for v in by_subject.values())
                    / sum(abs(v) for v in by_subject.values())
                    if total != 0 and by_subject else None),
            }
        return {'horizon': horizon, 'roles': out}
    finally:
        con.close()


def _caveats(groups: List[dict], unjoined: int) -> List[str]:
    """把"这些数字**不能**说明什么"写清楚。

    统计最容易被误读的方式不是算错，而是被当成另一件事的证据。以下三条在真实账本上
    都成立，缺任何一条都会让人把研究篮子的收益读成 LLM 的贡献。
    """
    caveats = []
    if unjoined:
        caveats.append(
            f'{unjoined} 条已结算结果行关联不到运行表（`decision_id` 不是 `decision_*` '
            f'形状，研究批次直接以批次 id 落账），因而拿不到角色与动作 —— 已排除，未静默丢弃')
    if groups and all(g['evidence_source'] in ('NONE', 'UNKNOWN') for g in groups):
        caveats.append(
            '**证据来源这一层目前不携带信息**：所有样本组的 evidence_source 都是 '
            'NONE/UNKNOWN，即关联到的冻结包里没有事件。不得据此说"不同证据来源表现不同"')
    if groups and any(not g['action'] for g in groups):
        no_action = sum(g['n'] for g in groups if not g['action'])
        caveats.append(
            f'{no_action} 个样本没有 effective_action（决策校验失败，L 采用父策略）'
            f'—— 与"模型选择了中性动作"是两回事，见 `promotion_review` 的输出有效率')
    caveats.append(
        '**这些结果行衡量的是研究篮子相对基准的收益，不是 L/R 影子路径之差**。'
        '把它们当作"LLM 的贡献"是错的；L−R 角色贡献须来自 portfolio_shadow 的实验账本'
        '（`paired_performance`），本包不提供该口径的数字')
    return caveats


def build_review_packet(registry, *, protocol_version: str, account_scope: str,
                        subject_id: str, as_of: str, horizon: Optional[str] = None,
                        window: Optional[dict] = None) -> dict:
    """组包。统计全部由程序算好；模型只能读。

    `horizon` 留空时按 `pick_horizon` 自动选择，并把**全部期限的可用量**与选定结果
    写进包 —— 期限选择必须可复核，不能只留下"选了哪个"。
    """
    from scripts.live_trading.decision_bridge import build_review_packet as _build
    availability = horizon_availability(registry)
    chosen = horizon or pick_horizon(availability)
    groups = sample_groups(registry, horizon=chosen) if chosen else {
        'groups': [], 'settled_rows': 0, 'unjoined_outcomes': 0,
        'unavailable_dimensions': dict(UNAVAILABLE_DIMENSIONS)}
    stats = role_stats(registry, horizon=chosen) if chosen else {'roles': {}}
    packet = _build(
        sample_groups=groups['groups'], role_stats=stats['roles'],
        protocol_version=protocol_version, account_scope=account_scope,
        subject_id=subject_id, as_of=as_of, window=window or {
            'horizon': chosen, 'horizon_availability': availability,
            'settled_rows': groups['settled_rows'],
            'unjoined_outcomes': groups['unjoined_outcomes'],
            'unavailable_dimensions': groups['unavailable_dimensions'],
            'caveats': _caveats(groups['groups'], groups['unjoined_outcomes'])})
    # packet_id 由 `decision_bridge.build_review_packet` 内容寻址算出，**这里不再算第二份**
    return packet


def render(packet: dict) -> str:
    window = packet.get('window') or {}
    lines = [f"# 协议复盘包（协议版本 {packet.get('protocol_version')}）", '',
             f"选定期限：{window.get('horizon') or '（无可关联样本）'}", '']
    availability = window.get('horizon_availability') or []
    if availability:
        lines.append('各期限可用量（期限选择必须可复核）：' + '；'.join(
            f"{a['horizon']}: 可用 {a['joined']} / 排除 {a['unjoined']}"
            for a in availability))
    lines.append(f"已结算样本行：{window.get('settled_rows')}；"
                 f"关联不到运行表而排除：{window.get('unjoined_outcomes')}")
    for name, reason in (window.get('unavailable_dimensions') or {}).items():
        lines.append(f"- ⚠️ 分层维度 `{name}` 不可用：{reason}")
    caveats = window.get('caveats') or []
    if caveats:
        lines.append('')
        lines.append('## 这些数字不能说明什么')
        for caveat in caveats:
            lines.append(f'- ⚠️ {caveat}')
    if not window.get('horizon'):
        lines.append('- ⚠️ **没有任何期限存在可关联的样本**：复盘没有统计基础，'
                     '模型不应被调用（调用也只能得到"样本不足"）')
    lines.append('')
    lines.append('## 分层样本组')
    for group in packet.get('sample_groups') or []:
        lines.append(f"- {group['name']}: n={group['n']} "
                     f"平均超额={_pct(group.get('mean_excess_pct'))} "
                     f"胜率={_pct(group.get('win_rate'))}")
    lines.append('')
    lines.append('## 角色汇总')
    for role, stats in (packet.get('role_stats') or {}).items():
        lines.append(f"- {role}: n={stats['n']} "
                     f"平均超额={_pct(stats.get('mean_excess_pct'))} "
                     f"标的数={stats.get('distinct_subjects')} "
                     f"单一贡献占比={_pct(stats.get('top_contributor_share'))}")
    return '\n'.join(lines)


def _pct(value) -> str:
    return 'n/a' if value is None else f'{value * 100:.2f}%'


def main(argv=None):
    import yaml
    from scripts.live_trading.position_registry import registry_for
    parser = argparse.ArgumentParser(description='§6.5 协议复盘（默认只准备包，不调用模型）')
    parser.add_argument('--config', default=None)
    parser.add_argument('--registry', default=None)
    parser.add_argument('--scope', default=None, help='账本 namespace；缺省取配置的 account_scope')
    parser.add_argument('--horizon', default=None,
                        help='留空则自动选可关联样本最多的期限（各期限可用量会写进包）')
    parser.add_argument('--model', choices=('fixture', 'real'), default=None,
                        help='缺省则只准备包；给了才发起模型调用')
    parser.add_argument('--output', default=None, help='写出 JSON 包/结果的路径')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[3]
    config = yaml.safe_load(Path(args.config or root / 'config.yaml').read_text()) or {}
    registry = registry_for(config, args.registry, args.scope)
    from scripts.live_trading.decision_ledger.event_store import utc
    packet = build_review_packet(
        registry, protocol_version=str(config.get('_protocol_version') or 'unversioned'),
        account_scope=config.get('llm_decision', {}).get('engine_v2', {}).get(
            'account_scope', 'DRY-RUN'),
        subject_id=f'review:{utc()[:10]}', as_of=utc(), horizon=args.horizon)
    print(render(packet))
    if args.output:
        Path(args.output).write_text(json.dumps(packet, ensure_ascii=False, indent=2,
                                                sort_keys=True), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
