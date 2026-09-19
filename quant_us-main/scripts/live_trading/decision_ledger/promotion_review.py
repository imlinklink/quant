"""§12 晋级资格的事实来源：从决策账本算出各角色的晋级指标，并给出可审计判定。

**只读**。本模块不切换任何权限级别、不改变执行路径 —— 满足门槛只表示"具备晋级资格"，
晋级仍须人工批准新 permission version（设计 §12）。

设计原则：**算不出来的指标一律返回 None 并在判定里标 `unavailable`**。把"算不出来"
读成"通过了"是这类门槛最危险的失效方式，因此宁可让门槛长期为红并写明原因。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.live_trading.llm_permission import (OPERATOR_ATTESTED, PERMISSIONS,
                                                 ROLE_PERMISSIONS, level_for,
                                                 promotion_report, validate_permissions)
from mutifactor.llm.validators.action import EFFECTIVE_BASELINE

# 角色 → 与执行侧一致的动作基线。**直接从执行侧派生，不另抄一份**：
# 原先这里手写 `{'selection','entry','position'}` 三项，而 `EFFECTIVE_BASELINE` 有五项 ——
# `portfolio`/`review` 取不到值 ⇒ `baseline=None` ⇒ 越权判据退化成
# `effective_action not in (None, None)`，即「动作非空就算越权」。后果是**完全正确的
# `no_change` 被报成硬风控越权**，把一个本该达标的角色永久挡在晋级之外。
# 而当时的一致性测试写成 `for role, baseline in ROLE_BASELINE.items()` —— 只遍历小字典，
# 少掉的角色永远测不到，两份字典不一致照样绿。现改为派生（不可能漂移）+ 测试改成
# **双向集合相等**。
ROLE_BASELINE = dict(EFFECTIVE_BASELINE)


def _events(con, event_type):
    """整条事件（含顶层链接字段）。

    **不能只取 `payload`**：`decision_id` 是 `make_event(**links)` 写在事件顶层的，
    payload 里没有它。按 payload 过滤会把所有事件静默滤空，让"账本能区分
    model/effective"算成 False —— 这个错我犯过一次，留此注释。
    """
    rows = con.execute('SELECT body FROM decision_events WHERE event_type=?',
                       (event_type,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def _runs(con, role):
    return [dict(r) for r in con.execute(
        'SELECT decision_id, subject_id, as_of, status, input_snapshot_id, '
        'selected_attempt_id FROM llm_decision_runs WHERE role=?', (role,)).fetchall()]


def _settled_groups(con, snapshot_by_decision, *, role='', scope=None) -> tuple:
    """按冻结决策的证券、主要事件簇、交易周去重；Position 按整笔 trade。

    不使用结算时间。缺少必要分组信息时返回 unavailable，避免高估独立样本。
    """
    if not snapshot_by_decision:
        return None, 0
    marks = ','.join('?' * len(snapshot_by_decision))
    query = (f'SELECT DISTINCT decision_id, subject_key FROM decision_outcomes_v2 '
             f"WHERE decision_id IN ({marks}) AND data_quality='good'")
    params = list(snapshot_by_decision)
    if scope is not None:
        query += ' AND account_scope=?'
        params.append(scope)
    rows = con.execute(query, params).fetchall()
    if not rows:
        return None, 0
    groups, missing, cache = set(), 0, {}
    for decision_id, subject_key in rows:
        if decision_id not in cache:
            row = con.execute('SELECT body FROM decision_snapshots WHERE id=? '
                              'ORDER BY version DESC LIMIT 1',
                              (snapshot_by_decision[decision_id],)).fetchone()
            cache[decision_id] = json.loads(row[0]) if row else None
        snapshot = cache[decision_id]
        if snapshot is None:
            missing += 1
            continue
        packet = snapshot.get('packet') or snapshot
        context = packet.get('context') or {}
        packet_role = role or context.get('role') or ''
        account = scope or context.get('account_scope') or ''
        trade = packet.get('trade') or {}
        if packet_role == 'position':
            if trade.get('trade_id'):
                groups.add((account, packet_role, trade['trade_id']))
            else:
                missing += 1
            continue
        stock = next((s for s in packet.get('stocks') or []
                      if s.get('code') == subject_key), None)
        identity = packet.get('identity') or {}
        code = ((stock or {}).get('code') or identity.get('security_id')
                or identity.get('code') or packet.get('code'))
        source = stock if stock is not None else packet
        cluster = source.get('primary_event_cluster')
        if not cluster:
            evidence = (source.get('evidence') or source.get('new_evidence')
                        or source.get('events') or [])
            clusters = {e['cluster_id'] for e in evidence if e.get('cluster_id')}
            # 唯一事件簇可直接确定；多个事件簇不能按字典序猜主要事件。
            cluster = next(iter(clusters)) if len(clusters) == 1 else None
        try:
            as_of = datetime.fromisoformat(str(context.get('as_of') or packet.get('as_of'))
                                          .replace('Z', '+00:00'))
            if as_of.tzinfo is None:
                raise ValueError('missing timezone')
            week = as_of.astimezone(ZoneInfo('America/New_York')).isocalendar()[:2]
        except (ValueError, TypeError):
            week = None
        if not code or not cluster or week is None:
            missing += 1
            continue
        groups.add((account, packet_role, code, cluster, week))
    return (None if missing else len(groups)), missing


def _role_stat(runs, role) -> dict:
    """tier-1 指标。指标名 → 取不到值时的**准确原因**（判定侧据此说明为什么不是通过）。"""
    finished = [r for r in runs if r['status'] in ('validated', 'failed')]
    validated = [r for r in finished if r['status'] == 'validated']
    stats = {
        'total_runs': len(runs),
        'finished_runs': len(finished),
        'validated_runs': len(validated),
        # 分母用"已出终态的运行"：仍在 requested 的不算失败，也不该算作有效
        'output_validity': (len(validated) / len(finished)) if finished else None,
        'unreplayable': sum(1 for r in validated
                            if not r['input_snapshot_id'] or not r['selected_attempt_id']),
        '_unavailable': {},
    }
    if not finished:
        stats['_unavailable']['output_validity'] = '该角色尚无已出终态的决策'
    return stats


def promotion_stats(registry, role) -> dict:
    """某角色的晋级指标。取不到的键返回 None（判定侧一律判不合格）。"""
    from scripts.live_trading.decision_ledger.event_store import EventStore
    EventStore(registry)                       # 确保 schema 已迁移
    import sqlite3
    con = sqlite3.connect(str(registry.path))
    con.row_factory = sqlite3.Row
    try:
        runs = _runs(con, role)
        stats = _role_stat(runs, role)
        unavailable = stats['_unavailable']
        baseline = ROLE_BASELINE.get(role)
        # 顶层 decision_id 是链接字段；payload 里没有它（见 `_events` 的说明）
        effective = _events(con, 'decision_effective_action')
        role_ids = {r['decision_id'] for r in runs}
        role_effective = [e for e in effective if e.get('decision_id') in role_ids]
        payloads = [e.get('payload') or {} for e in role_effective]
        # 越权：等级为 shadow（不执行任何动作）时，effective_action 却不等于基线
        stats['hard_risk_overreach'] = sum(
            1 for p in payloads
            if p.get('permission_level') == 'shadow'
            and p.get('effective_action') not in (None, baseline))
        # 无权限裁决记录时返回 **None**（走 unavailable 分支）而不是 False：
        # 「该角色尚无决策」与「账本无法区分 model/effective」是两回事，原因必须可分辨。
        stats['model_effective_distinguishable'] = (
            all(p.get('model_action') is not None and p.get('effective_action') is not None
                for p in payloads) if payloads else None)
        if not payloads:
            unavailable['model_effective_distinguishable'] = '该角色尚无权限裁决记录'
        groups, missing = _settled_groups(
            con, {r['decision_id']: r['input_snapshot_id'] for r in runs},
            role=role, scope=registry.namespace)
        stats['independent_mature_samples'] = groups
        stats['_snapshot_missing'] = missing
        if groups is None:
            unavailable['independent_mature_samples'] = (
                '已结算样本缺少快照或必要分组信息' if missing
                else '该角色尚无已结算的期限结果')
        # 以下三项当前无法从账本算出：成本与回撤差未进 outcome 投影；样本外窗口与人工
        # 批准属人工声明。**返回 None 而不是 0** —— 判定侧标 unavailable 并拒绝晋级。
        stats['excess_return_after_cost'] = None
        stats['mdd_delta'] = None
        stats['top_contributor_share'] = None
        for name in ('excess_return_after_cost', 'mdd_delta', 'top_contributor_share'):
            unavailable[name] = '交易成本/回撤差未进入 outcome 投影，尚不可计算'
        for name in OPERATOR_ATTESTED:
            stats[name] = None
            unavailable[name] = '须人工声明（无法从账本推出）'
        return stats
    finally:
        con.close()


def review_promotions(registry, config) -> dict:
    """全部角色的晋级资格报告（可审计）。"""
    stats = {role: promotion_stats(registry, role) for role in sorted(ROLE_PERMISSIONS)}
    report = promotion_report(config, stats)
    report['stats'] = stats
    report['config_errors'] = validate_permissions(config)
    return report


def render(report: dict) -> str:
    lines = ['# LLM 权限晋级资格（设计 §12）', '',
             f"配置校验：{'通过' if not report['config_errors'] else report['config_errors']}",
             f"— {report['note']}", '']
    for role, verdict in report['roles'].items():
        lines.append(f"## {role}（当前 {verdict['current_level']} → "
                     f"{verdict['target_level'] or '无更高台阶'}）")
        if not verdict['checks']:
            lines.append(f"- {verdict['note']}")
            continue
        lines.append(f"- 结论：{'**具备晋级资格**' if verdict['eligible'] else '未达标'}"
                     f"（{verdict['note']}）")
        for check in verdict['checks']:
            mark = '✅' if check['passed'] else '❌'
            lines.append(f"  - {mark} {check['name']}: 实际 {check['actual']} / "
                         f"门槛 {check['required']}"
                         + (f" —— {check['reason']}" if check['reason'] else ''))
        lines.append('')
    lines.append('当前等级：' + ', '.join(f'{k}={v}' for k, v in report['levels'].items()))
    return '\n'.join(lines)


def main(argv=None):
    from scripts.live_trading.position_registry import registry_for
    parser = argparse.ArgumentParser(description='§12 晋级资格判定（只读，不切换等级）')
    parser.add_argument('--config', default=None, help='默认 quant_us-main/config.yaml')
    parser.add_argument('--registry', default=None, help='默认 data/execution.sqlite3')
    parser.add_argument('--scope', default=None, help='账本 namespace；缺省取配置的 account_scope')
    parser.add_argument('--output', default=None, help='写出 JSON 报告的路径')
    args = parser.parse_args(argv)
    import yaml
    root = Path(__file__).resolve().parents[3]
    cfg = yaml.safe_load(Path(args.config or root / 'config.yaml').read_text()) or {}
    registry = registry_for(cfg, args.registry, args.scope)
    report = review_promotions(registry, cfg)
    print(render(report))
    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding='utf-8')
    return 0 if not report['config_errors'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
