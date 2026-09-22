#!/usr/bin/env python3
"""构建决策可视化快照：只读账本 → JSON（需求 `docs/decision-visibility-product-requirements-
2026-09-22.md` §6「各自由各自固定运行版本提供只读导出，Web 消费统一快照」）。

用法::

    python3 ops/build_web_snapshots.py [--base DIR] [--snapshot-dir DIR]
                                       [--only scope_id,scope_id] [--print] [--dry-run]

**退出码**：任一 scope 读取失败返回 1，但**其余 scope 照常写出** —— 需求场景 10 要求
「一个来源坏了，其他卡片仍可使用」。所以失败是「这一张卡是读取失败」，不是「整页空白」。
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.analytics_export import contract as C                      # noqa: E402
from ops.analytics_export import registry as reg                    # noqa: E402
from ops.analytics_export import sections as S                      # noqa: E402
from ops.analytics_export import write as W                         # noqa: E402
from ops.analytics_export.readers import (RawSqlStore, experiment_row,  # noqa: E402
                                          ledger_schema_version, table_counts)
from ops.analytics_export.vocabulary import classify_unknown        # noqa: E402

DEFAULT_BASE = ROOT / 'quant_us-main' / 'data'


def _generation_id(now: datetime) -> str:
    return now.strftime('%Y-%m-%dT%H%M%S%z')


def _paper_scope(base: Path, entry: dict, now: datetime):
    """一个纸面 scope → (envelope, extra_files, decisions)。"""
    ledger = base / entry['ledger']
    schema_on_disk = ledger_schema_version(ledger)
    missing = []
    if schema_on_disk is None:
        raise RuntimeError(f'LEDGER_UNREADABLE:{ledger}')
    declared = entry.get('ledger_schema_expected')
    exp = experiment_row(ledger, entry['experiment_id'])
    store = RawSqlStore(ledger, entry['experiment_id'])

    # 当前价：按账户 last_session 从面板取（持仓体里不含当前价）
    acct_sessions = {}
    sids = set()
    for a in entry['accounts']:
        row = store.latest_state(a['account_id'])
        if row is None:
            continue
        body = row[1]
        acct_sessions[a['account_id']] = body.get('last_session')
        sids.update((body.get('positions') or {}).keys())
    prices = S.panel_closes(base, sids, sorted(set(v for v in acct_sessions.values() if v))[-1]
                            if any(acct_sessions.values()) else None)

    # 回放 / 前向边界：**声明**与账本交叉核对（需求场景 7）
    boundary = {
        'replay_start_session': entry.get('replay_start_session'),
        'replay_end_session': entry.get('replay_end_session'),
        'forward_start_session': entry.get('forward_start_session'),
        'boundary_source': entry.get('boundary_source'),
    }
    nav_sessions = sorted({n['session'] for a in entry['accounts']
                           for n in store.daily_nav(a['account_id'])})
    replay_nav = [s for s in nav_sessions
                  if entry.get('replay_end_session') and s <= entry['replay_end_session']]
    forward_nav = [s for s in nav_sessions
                   if not entry.get('replay_end_session') or s > entry['replay_end_session']]
    boundary['nav_sessions_total'] = len(nav_sessions)
    boundary['nav_replay_sessions'] = len(replay_nav)
    boundary['nav_forward_sessions'] = len(forward_nav)
    # 模型有效样本：**只数前向区间内确实调过真实模型并完成的尝试**
    valid = sum(r['scope'].endswith(':L') and r['valid_real_review'] and r['phase'] == 'forward'
                for r in S.I.decision_rows(store, entry.get('forward_start_session')))
    boundary['model_valid_sample_count'] = valid
    boundary['model_valid_sample_why'] = (
        '只计前向起点之后、真实模型、确实发起调用且完成的评审（夹具与程序侧弃权都不算）')

    data_status = C.DS_OK
    if declared is not None and schema_on_disk != declared:
        data_status = C.DS_INCONSISTENT
        missing.append(C.missing('ledger_schema',
                                 f'登记声明 schema {declared}，盘上是 {schema_on_disk}，'
                                 f'两者矛盾 ⇒ 结论待核对', source=str(ledger)))
    elif not nav_sessions:
        data_status = C.DS_NO_OBJECT
    elif exp is None:
        data_status = C.DS_INCONSISTENT
        missing.append(C.missing('shadow_experiments',
                                 '账本里没有本实验的冻结记录', source=str(ledger)))
    covered = {c for c in _observed_codes(store)}
    for code in classify_unknown(covered):
        missing.append(C.missing(f'reason/action:{code}',
                                 '词表未覆盖该码，页面显示为「未采集（词表未覆盖）」',
                                 status=C.UNCLASSIFIED, source=str(ledger)))

    # sections 在 data_status 之后算：「需要处理」要用它做真实检查（不能写死空待办）
    secs, mv = S.paper_scope_sections(store, entry, prices=prices, base=base,
                                      data_status=data_status)
    from scripts.portfolio_shadow.data_readiness import expected_session
    expected = expected_session(now)
    boundary['expected_completed_session'] = expected
    for scope, actual in acct_sessions.items():
        if expected and actual and actual < expected:
            secs['overview']['todo'].append(
                f'{scope} 结算截至 {actual}，最近已完成会话为 {expected}；'
                '等待对应日作业推进或核对运行记录，不表示应补做过期模型决策')
    secs['overview']['todo_checks'].append('账户结算日期与最近已完成交易会话（既有交易日历）')

    env = C.envelope(
        generation_id=_generation_id(now), scope_id=entry['scope_id'],
        scope_kind=entry['kind'], experiment_id=entry['experiment_id'],
        account_id=None,
        strategy_version=S.strategy_version(mv, dict(entry, manifest_hash=(exp or {}).get('manifest_hash'))),
        as_of=(nav_sessions[-1] if nav_sessions else None), generated_at=now.isoformat(),
        source_ref=S.source_ref(entry, schema_on_disk=schema_on_disk,
                                extra={'run_dir': entry.get('run_dir')}),
        data_status=data_status, missing_reasons=missing,
        sections={**secs, 'boundary': boundary,
                  'diagnostics': {'tables': table_counts(ledger),
                                  'experiment_status': (exp or {}).get('status')}})
    keys = {oid for oid, _ in store.opportunity_rows()}
    keys.update(k for k, _ in store.packets_matching('@pos:'))
    decisions = [(f"{entry['scope_id']}@{oid}", oid) for oid in sorted(keys)]
    return env, decisions


def _observed_codes(store) -> set:
    """账本里实际出现过的码（用于「词表未覆盖」的如实披露，不静默归桶）。"""
    codes = set()
    for a in store.applications():
        codes.add(a.get('action'))
        codes.add(a.get('reason_code'))
    for o in store.opportunities():
        codes.add(o.get('terminal'))
    return codes


def _decision_file(base: Path, scope_entry: dict, decision_key: str, oid: str, now: datetime):
    ledger = base / scope_entry['ledger']
    store = RawSqlStore(ledger, scope_entry['experiment_id'])
    rp = S._report()
    trace = rp.decision_trace(store, oid,
                              tuple(a['account_id'] for a in scope_entry['accounts']))
    packet = store.packet_for_opportunity(oid) or {}
    identity = packet.get('identity') or {}
    parent = store.opportunity(S.I.base_opportunity(oid)) or {}
    trace['security_id'] = identity.get('security_id') or parent.get('security_id')
    trace['signal_session'] = identity.get('reviewed_session') or parent.get('signal_session')
    trace['planned_execution_session'] = identity.get('execution_session') or parent.get('planned_execution_session')
    trace['technical_evidence'] = packet.get('new_evidence') or packet.get('technical_packet') or []
    trace['frozen_packet'] = packet
    all_rows = S.I.decision_rows(store, scope_entry.get('forward_start_session'))
    trace['decision_rows'] = [r for r in all_rows if r['opportunity_id'] == oid]
    trace['lifecycle_events'] = {a['account_id']: [e for e in store.events(a['account_id'])
        if e.get('opportunity_id') == S.I.base_opportunity(oid)] for a in scope_entry['accounts']}
    cases = S.I.contribution_cases(store, [a['account_id'] for a in scope_entry['accounts']], all_rows)
    trace['outcomes'] = [c for c in cases.get('all', [])
                         if c['opportunity_id'] == S.I.base_opportunity(oid)]
    trace['decision_id'] = decision_key
    trace['scope_id'] = scope_entry['scope_id']
    trace['generated_at'] = now.isoformat()
    return trace


def _experiments_index(base: Path, scopes: list, now: datetime) -> tuple:
    """`/experiments` 页的全局数据：研究结论 + 前向进度。**不给完成百分比**。

    报告正文**不塞进这个 JSON**：`report.md` 动辄上百 KB，四份就把页面载荷顶到 2MB
    （实测 2.2MB）。正文单独写成 `reports/<key>.md`，页面按需取。
    """
    cards, reports, artifacts = [], {}, {}
    for entry in scopes:
        if entry['kind'] != 'research':
            continue
        secs = S.research_sections(base, entry)
        r = secs.get('research') or {}
        report_md = r.pop('report', None)
        key = entry['scope_id'].replace(':', '__').replace('/', '_')
        if report_md is not None:
            reports[key] = report_md
            r['has_report'] = True
        for name, payload in (secs.pop('full', {}) or {}).items():
            artifacts[f'{key}__{name}'] = payload
            r.setdefault('full_artifacts', []).append(name)
        cards.append({'scope_id': entry['scope_id'], 'label': entry.get('label'),
                      'kind': 'research', 'run_dir': entry.get('run_dir'),
                      'sections': secs})
    index = {'generated_at': now.isoformat(), 'research_cards': cards,
             'comparison_rows': [S.normalize_study(base, e, (S.research_sections(
                 base, e).get('full') or {}))
                 for e in scopes if e['kind'] == 'research'],
             'note': '研究结论只用于淘汰方向；正式资格需前向登记，本页不生成「策略提升」结论'}
    return index, reports, artifacts


def build(base: Path, snapshot_dir: Path, only=None, now=None) -> tuple:
    now = now or datetime.now().astimezone()
    reg_data = reg.load_registry()
    scopes = reg.expand(base, reg_data)
    if only:
        want = {s.strip() for s in only.split(',') if s.strip()}
        scopes = [s for s in scopes if s['scope_id'] in want]
    files, index_scopes, decisions, failed = {}, [], [], []
    for entry in scopes:
        try:
            if entry['kind'] == 'paper':
                env, decs = _paper_scope(base, entry, now)
                rel = f'scope__{entry["scope_id"].replace(":", "__")}.json'
                files[rel] = env
                index_scopes.append({'scope_id': entry['scope_id'], 'label': entry.get('label'),
                                     'kind': entry['kind'], 'file': rel,
                                     'data_status': env['data_status'], 'as_of': env['as_of']})
                decisions.extend((entry, k, o) for k, o in decs)
            elif entry['kind'] == 'research':
                secs = S.research_sections(base, entry)
                secs.pop('full', None)      # 全量产物另存 artifacts/，不进这一份信封
                env = C.envelope(
                    generation_id=_generation_id(now), scope_id=entry['scope_id'],
                    scope_kind='research', experiment_id=entry['scope_id'],
                    account_id=None, strategy_version={'run_dir': entry.get('run_dir')},
                    as_of=None, generated_at=now.isoformat(),
                    source_ref={'kind': 'research', 'run_dir': entry.get('run_dir'),
                                'read_mode': '只读文件'},
                    data_status=C.DS_OK if not secs['missing_artifacts'] else C.DS_PARTIAL,
                    missing_reasons=secs['missing_artifacts'], sections=secs)
                rel = f'scope__{entry["scope_id"].replace(":", "__")}.json'
                files[rel] = env
                index_scopes.append({'scope_id': entry['scope_id'], 'label': entry.get('label'),
                                     'kind': entry['kind'], 'file': rel,
                                     'data_status': env['data_status'], 'as_of': None})
            elif entry['kind'] == 'live_simulated':
                secs, sv = S.live_sections(base, entry)
                env = C.envelope(
                    generation_id=_generation_id(now), scope_id=entry['scope_id'],
                    scope_kind='live_simulated', experiment_id=entry['scope_id'],
                    account_id=None, strategy_version=sv,
                    as_of=None, generated_at=now.isoformat(),
                    source_ref={'kind': 'live_simulated', 'db': entry['db'],
                                'namespaces': entry['namespaces'],
                                'read_mode': 'sqlite mode=ro（books 表两个 JSON blob）',
                                'note': 'trd_env=SIMULATE ⇒ 模拟成交，不是真实资金'},
                    data_status=C.DS_OK, missing_reasons=[], sections=secs)
                rel = f'scope__{entry["scope_id"].replace(":", "__")}.json'
                files[rel] = env
                index_scopes.append({'scope_id': entry['scope_id'], 'label': entry.get('label'),
                                     'kind': entry['kind'], 'file': rel,
                                     'data_status': env['data_status'], 'as_of': None})
        except Exception as exc:
            failed.append({'scope_id': entry['scope_id'], 'error': repr(exc),
                           'traceback': traceback.format_exc()})
            index_scopes.append({'scope_id': entry['scope_id'], 'label': entry.get('label'),
                                 'kind': entry['kind'], 'file': None,
                                 'data_status': C.DS_READ_FAILED, 'as_of': None,
                                 'error': repr(exc)})
    exp_index, reports, artifacts = _experiments_index(base, scopes, now)
    exp_index['forward_scopes'] = [
        {'scope_id': v['scope_id'], 'as_of': v['as_of'], 'data_status': v['data_status'],
         'boundary': v['sections'].get('boundary', {}),
         'progress': v['sections'].get('overview', {}).get('in_progress', [])}
        for v in files.values() if isinstance(v, dict) and v.get('scope_kind') == 'paper']
    files['experiments.json'] = exp_index
    for key, md in reports.items():
        files[f'reports/{key}.md'] = md
    for key, payload in artifacts.items():
        files[f'artifacts/{key}.json'] = payload
    for entry, key, oid in decisions:
        try:
            files[f'decisions/{key.replace("/", "_")}.json'] = _decision_file(
                base, entry, key, oid, now)
        except Exception as exc:
            failed.append({'scope_id': key, 'error': repr(exc)})
    index = {'generation_id': _generation_id(now), 'generated_at': now.isoformat(),
             'registry_version': reg_data['registry_version'],
             'scopes': index_scopes,
             'unregistered_run_dirs': reg.check_completeness(base, reg_data),
             'failed': failed}
    files['index.json'] = index
    return files, index, failed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--base', default=str(DEFAULT_BASE))
    ap.add_argument('--snapshot-dir', default=None)
    ap.add_argument('--only', default=None)
    ap.add_argument('--print', dest='do_print', action='store_true', help='打印 index 后退出')
    ap.add_argument('--dry-run', action='store_true', help='构建但不写盘')
    args = ap.parse_args(argv)
    base = Path(args.base)
    snapshot_dir = Path(args.snapshot_dir) if args.snapshot_dir else \
        base / 'portfolio_shadow' / 'web_snapshots'
    files, index, failed = build(base, snapshot_dir, args.only)
    if args.do_print:
        print(json.dumps(index, ensure_ascii=False, indent=2))
        return 1 if failed else 0
    if index['unregistered_run_dirs']:
        print(f'⚠️ 盘上有未登记的实验目录（新实验落地要登记）：{index["unregistered_run_dirs"]}')
    if args.dry_run:
        print(f'dry-run：将写 {len(files)} 个文件到 {snapshot_dir}/{index["generation_id"]}')
        return 1 if failed else 0
    gen = W.write_generation(snapshot_dir, index['generation_id'], files)
    removed = W.prune(snapshot_dir)
    print(json.dumps({'generation': gen.name, 'files': len(files), 'pruned': removed,
                      'failed': [f['scope_id'] for f in failed]}, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
