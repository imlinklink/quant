"""买入侧第二批：抄底信号 vs B3 的**账户级**对照（规范 §5.3）。

两臂**只差入场包**：

  A = B3（月排名 + 市场门 + 周线 + 日线择时），用 `IncrementalCandidateGenerator`（与基线
      study 同一份实现、同一条慢路径 ⇒ 它必须复现 012 的成交，这是控制项）；
  B = 抄底信号（`BottomSignalGenerator`），替换的是**整个入场包**。

其余逐字相同：同一股票池、质量要求、市场数据、日历、成本、H60 期限退出、初始硬止损公式、
单笔风险预算、五仓上限、公司行动会计、同引擎、同空仓起点。**第一批的利润保护不接入**
（那一批的结论是保留原退出）。

报告口径（§5.3）：共同成交 / 新增机会 / 原策略独有机会 / 账户拒绝机会，**新增盈利与新增亏损
同时呈现**；不强行把不对应的交易配成对；披露两侧信号密度不同。
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

import pandas as pd

from scripts.portfolio_shadow.candidate_adapter import IncrementalCandidateGenerator
from scripts.portfolio_shadow.cli import manifest_from_dict
from scripts.portfolio_shadow.paper_engine import new_account_state
from scripts.portfolio_shadow.schema import to_micro
from scripts.strategy_diagnostics import manifest as study_manifest
from scripts.strategy_diagnostics.experiments import shadow_actions, step_account_session
from scripts.strategy_diagnostics.exit_attribution import summarize, trades_from_events
from scripts.strategy_diagnostics.statistics import annual_returns
from scripts.strategy_research.bottom_signal import BottomSignalGenerator
from scripts.strategy_research.runner import load_study

FEE_STRESS_BP = 20


def _arm_manifest(base, *, experiment_id: str, scope: str, start_session: str,
                  parent_code_hash: str):
    m = replace(base, experiment_id=experiment_id, account_scopes=(scope,), status='FROZEN',
                llm_policy={'overlay': 'fixed_pass', 'use_real_model': False},
                start_session=start_session, parent_code_hash=parent_code_hash)
    errors = m.validate()
    if errors:
        raise ValueError(';'.join(errors))
    return m


def _provider(kind: str, *, arm, data, prices, market, calendar, quality, actions, sessions):
    if kind == 'b3':
        return IncrementalCandidateGenerator(
            prices=prices, market=market, quality=quality, actions=actions, blocked={},
            calendar=calendar, experiment_id=arm['experiment_id'],
            parent_version=arm['manifest'].parent_version,
            exit_policy_id=arm['manifest'].execution_policy['exit_policy_id'],
            top_n=arm['manifest'].risk_policy.get('top_n', 5),
            max_wait_sessions=arm['manifest'].execution_policy.get('max_wait_sessions', 20),
            require_matured=False, parent_strategy_id=arm['manifest'].parent_strategy_id)
    return BottomSignalGenerator(
        prices=prices, calendar=sessions, actions=actions, quality=quality,
        experiment_id=arm['experiment_id'],
        parent_version=arm['manifest'].parent_version,
        exit_policy_id=arm['manifest'].execution_policy['exit_policy_id'])


def run_arm(kind: str, *, study_dir: Path, arm_id: str, fee_bp: int,
            limit: int | None = None) -> dict:
    """跑一条臂的完整账户。返回 {navs, trades, events, states, opportunities, rejections}。"""
    data, prices, market, calendar, quality, actions, _entries = load_study(study_dir)
    start, end = (str(pd.Timestamp(data['research_window'][k]).date()) for k in ('start', 'end'))
    sessions = [s for s in calendar if pd.Timestamp(start) <= s <= pd.Timestamp(end)]
    if limit:
        sessions = sessions[:limit]
    base = manifest_from_dict(
        study_manifest.read(study_dir / data['input_index']['baseline'][0]['path']))
    scope = f'SHADOW:{arm_id}:R'
    manifest = _arm_manifest(base, experiment_id=arm_id, scope=scope,
                             start_session=str(sessions[0].date()),
                             parent_code_hash=data['working_tree_hash'])
    arm = {'experiment_id': arm_id, 'manifest': manifest}
    provider = _provider(kind, arm=arm, data=data, prices=prices, market=market,
                         calendar=calendar, quality=quality, actions=actions, sessions=sessions)
    acts = defaultdict(list)
    converted, _dropped = shadow_actions(actions, universe=set(prices.security_id),
                                         session_range=(start, end))
    for a in converted:
        acts[a['ex_date']].append(a)
    state = new_account_state(scope, manifest.initial_cash)
    schedule, events, navs, opportunities, rejections = defaultdict(list), [], [], [], []
    for session in sessions:
        date = str(session.date())
        for o in provider.opportunities_for(session):
            opportunities.append(o)
            schedule[o.planned_execution_session].append(o)
        due = schedule.pop(date, [])
        result = step_account_session(state, session=session, prices=prices, acts=acts, due=due,
                                     manifest=manifest, fee_bp=fee_bp)
        state = result.state
        events.extend(result.events)
        navs.append(result.nav)
        counts = Counter(e['reason'] for e in result.events if e['type'] == 'missed')
        rejections.append({'session': date, 'rejections': dict(counts), 'due': len(due)})
    trades = trades_from_events(events, state, prices, actions, calendar, sessions[-1])
    return {'kind': kind, 'arm_id': arm_id, 'fee_bp': fee_bp, 'scope': scope,
            'manifest': manifest, 'state': state, 'navs': navs, 'trades': trades,
            'events': events, 'opportunities': opportunities, 'rejections': rejections,
            'signals': getattr(provider, 'signals', None),
            'sessions': sessions, 'prices': prices, 'actions': actions, 'calendar': calendar,
            'data': data}


def _peak_mdd(navs: list[dict], initial_cash: int) -> tuple[float, float]:
    peak, mdd = initial_cash, 0.0
    for nav in navs:
        peak = max(peak, nav['full_cost_equity'])
        mdd = min(mdd, nav['full_cost_equity'] / peak - 1)
    return peak, mdd


def _es(values: list[float], tail: float = 0.05):
    if not values:
        return None
    ordered = sorted(values)
    k = max(1, int(round(len(ordered) * tail)))
    return -sum(ordered[:k]) / k


def arm_metrics(run: dict) -> dict:
    navs, trades = run['navs'], run['trades']
    initial = run['manifest'].initial_cash
    final = navs[-1]['full_cost_equity']
    years = len(navs) / 252
    peak, mdd = _peak_mdd(navs, initial)
    closed = [t for t in trades if t.get('status') == 'closed']
    wins = [t for t in closed if t['net_pnl_micro'] > 0]
    losses = [t for t in closed if t['net_pnl_micro'] <= 0]
    gross_win = sum(t['net_pnl_micro'] for t in wins)
    gross_loss = -sum(t['net_pnl_micro'] for t in losses)
    r_values = [t['net_r'] for t in closed if t.get('net_r') is not None]
    entries = {(t['security_id'], t['entry_session']) for t in trades}
    return {
        'final_equity_usd': final / 1e6, 'total_return': final / initial - 1,
        'cagr': (final / initial) ** (1 / years) - 1 if years > 0 else None,
        'mdd': mdd, 'peak_usd': peak / 1e6,
        'n_trades': len(trades), 'n_closed': len(closed),
        'win_rate': (len(wins) / len(closed)) if closed else None,
        'profit_loss_ratio': (gross_win / gross_loss) if gross_loss > 0 else None,
        'worst_trade_r': min(r_values) if r_values else None,
        'tail_es_r': _es(r_values),
        'fees_usd': navs[-1]['fees'] / 1e6,
        'mean_exposure': sum(n['gross_exposure'] / n['equity'] for n in navs if n['equity'] > 0)
                         / len(navs),
        'mean_cash_share': sum(n['cash_available'] / n['equity'] for n in navs if n['equity'] > 0)
                           / len(navs),
        'annual': annual_returns(navs, initial),
        'entry_keys': entries,
        'rejections': dict(sum((Counter(r['rejections']) for r in run['rejections']),
                               Counter())),
        'n_scheduled': sum(r['due'] for r in run['rejections']),
    }


def _pnl_of(trades: list[dict], keys: set) -> dict:
    picked = [t for t in trades if (t['security_id'], t['entry_session']) in keys]
    wins = [t for t in picked if t['net_pnl_micro'] > 0]
    losses = [t for t in picked if t['net_pnl_micro'] <= 0]
    r = [t['net_r'] for t in picked if t.get('net_r') is not None]
    return {'n': len(picked), 'net_usd': sum(t['net_pnl_micro'] for t in picked) / 1e6,
            'gross_win_usd': sum(t['net_pnl_micro'] for t in wins) / 1e6,
            'gross_loss_usd': sum(t['net_pnl_micro'] for t in losses) / 1e6,
            'n_win': len(wins), 'n_loss': len(losses),
            'sum_r': sum(r) if r else None}


def compare(arm_a: dict, arm_b: dict) -> dict:
    ma, mb = arm_metrics(arm_a), arm_metrics(arm_b)
    only_a = ma['entry_keys'] - mb['entry_keys']
    only_b = mb['entry_keys'] - ma['entry_keys']
    common = ma['entry_keys'] & mb['entry_keys']
    by_sec = defaultdict(float)
    for t in arm_b['trades']:
        if (t['security_id'], t['entry_session']) in only_b:
            by_sec[t['security_id']] += t['net_pnl_micro'] / 1e6
    ordered = sorted(by_sec.items(), key=lambda kv: kv[1], reverse=True)
    total_new = sum(by_sec.values())
    return {
        'a': {k: v for k, v in ma.items() if k != 'entry_keys'},
        'b': {k: v for k, v in mb.items() if k != 'entry_keys'},
        'delta_terminal_return': mb['total_return'] - ma['total_return'],
        'delta_cagr': (None if mb['cagr'] is None or ma['cagr'] is None
                       else mb['cagr'] - ma['cagr']),
        'delta_mdd': abs(mb['mdd']) - abs(ma['mdd']),
        'delta_worst_trade_r': (None if mb['worst_trade_r'] is None or ma['worst_trade_r'] is None
                                else mb['worst_trade_r'] - ma['worst_trade_r']),
        'entries': {'common': len(common), 'only_a': len(only_a), 'only_b': len(only_b),
                    'n_a': len(ma['entry_keys']), 'n_b': len(mb['entry_keys'])},
        'common_pnl': _pnl_of(arm_a['trades'], common),
        'new_entries_pnl': _pnl_of(arm_b['trades'], only_b),
        'dropped_entries_pnl': _pnl_of(arm_a['trades'], only_a),
        'new_entries_by_security': ordered,
        'new_entries_top1_share': (ordered[0][1] / total_new) if ordered and total_new else None,
        'new_entries_leave_one_out': total_new - ordered[0][1] if ordered else None,
    }


def _control_reproduction(arm_a: dict, study_dir: Path, sessions: list) -> list[dict]:
    """控制项：A 臂必须复现基线 study 的成交（按 (证券, 入场日) 的集合与逐笔净损益）。

    这不是形式：A 臂换了驱动方式（这里逐日跑生成器 + 无账本），如果它与冻结 study 的
    成交对不上，那么"两臂只差入场包"就是假的，后面的差额没有意义。
    """
    recorded = {}
    rows = json.loads((Path(study_dir) / 'exit_diagnostics.json').read_text(encoding='utf-8'))
    last = str(pd.Timestamp(sessions[-1]).date())
    truncated = len(sessions) < len(arm_a['calendar'])
    skip = set()
    for r in rows:
        key = (str(r['security_id']), str(r['entry_session']))
        if str(r['entry_session']) > last:
            skip.add(key)      # 截断窗口：这一段还没跑到
        elif truncated and r['exit_session'] is not None and str(r['exit_session']) > last:
            skip.add(key)      # 入场在窗内、出场在窗外：截断运行里它还是未平仓
        else:
            recorded[key] = r
    played = {(t['security_id'], t['entry_session']): t for t in arm_a['trades']}
    bad = []
    # 跳过的键要**从两边一起摘掉**：只从 recorded 摘会让 A 臂那笔看起来像"凭空多出来的成交"，
    # 于是截断运行永远报 ENGINEERING_BLOCKED —— 而真正想看的失败被淹在里面。
    for key in sorted((set(recorded) | set(played)) - skip):
        rec, got = recorded.get(key), played.get(key)
        if rec is None or got is None:
            bad.append({'key': list(key),
                        'recorded': None if rec is None else rec['exit_reason'],
                        'replayed': None if got is None else got['exit_reason']})
            continue
        if (rec['exit_reason'] != got['exit_reason']
                or str(rec['exit_session']) != str(got['exit_session'])
                or rec.get('net_pnl_micro') != got.get('net_pnl_micro')):
            bad.append({'key': list(key), 'recorded': [rec['exit_reason'], rec['exit_session'],
                                                       rec.get('net_pnl_micro')],
                        'replayed': [got['exit_reason'], got['exit_session'],
                                     got.get('net_pnl_micro')]})
    return bad


def verdict(comparison: dict, *, n_min: int = 30) -> dict:
    """按**预登记文件**的门槛判。阈值取自登记，不在这里放宽。"""
    c = comparison
    a, b = c['a'], c['b']
    checks = {
        'reproduces_the_baseline': not c.get('control_reproduction_failures'),
        'enough_trades': a['n_trades'] >= n_min and b['n_trades'] >= n_min,
        'terminal_return_improved': c['delta_terminal_return'] > 0,
        'mdd_within_budget': c['delta_mdd'] <= 0.03,
        'worst_trade_not_worse': (c['delta_worst_trade_r'] or 0) >= 0,
        'tail_es_not_worse': (b['tail_es_r'] or 0) <= (a['tail_es_r'] or 0),
        'not_concentrated': ((c['new_entries_top1_share'] or 1.0) <= 0.5
                             and (c['new_entries_leave_one_out'] or 0) > 0),
        'held_under_2x_cost': c.get('delta_terminal_return_2x', None) is None
                              or c['delta_terminal_return_2x'] > 0,
    }
    risk_improved = checks['mdd_within_budget'] and (
        checks['worst_trade_not_worse'] and checks['tail_es_not_worse'])
    if not checks['reproduces_the_baseline']:
        token = 'ENGINEERING_BLOCKED'
    elif not checks['enough_trades']:
        token = 'INSUFFICIENT_SAMPLE'
    elif not risk_improved:
        token = 'RISK_REJECTED'
    elif not checks['not_concentrated']:
        token = 'CONCENTRATED'
    elif checks['terminal_return_improved'] and checks['held_under_2x_cost']:
        token = 'EVIDENCE_SUPPORTED'
    elif checks['terminal_return_improved']:
        token = 'RISK_REJECTED'      # 收益改善但 2× 成本下不成立
    else:
        token = 'RISK_TRADEOFF'
    return {'token': token, 'checks': checks, 'n_min': n_min,
            'note': 'RISK_TRADEOFF = 风险改善但收益牺牲 ⇒ 只能记为防守选项，不得写成全面优胜。'}


def run_both(study_dir: Path, *, fee_bp: int = 10, limit: int | None = None) -> dict:
    study_dir = Path(study_dir)
    data = json.loads((study_dir / 'study_manifest.json').read_text(encoding='utf-8'))
    tag = f'-{fee_bp}bp' if fee_bp != 10 else ''
    a = run_arm('b3', study_dir=study_dir, arm_id=f'ENTRY-B3{tag}', fee_bp=fee_bp, limit=limit)
    b = run_arm('bottom', study_dir=study_dir, arm_id=f'ENTRY-BOTTOM{tag}', fee_bp=fee_bp,
                limit=limit)
    out = {'study_id': data['study_id'], 'fee_bp': fee_bp, 'limit': limit,
           'a': a, 'b': b, 'comparison': compare(a, b)}
    out['comparison']['control_reproduction_failures'] = _control_reproduction(
        a, study_dir, a['sessions'])
    out['bottom_signals'] = _signal_summary(b)
    return out


def full_study(study_dir: Path, *, limit: int | None = None) -> dict:
    """1× 与 2× 成本两个情景，再按登记判。"""
    base = run_both(study_dir, fee_bp=10, limit=limit)
    stress = run_both(study_dir, fee_bp=FEE_STRESS_BP, limit=limit)
    base['comparison']['delta_terminal_return_2x'] = (
        stress['comparison']['b']['total_return'] - stress['comparison']['a']['total_return'])
    base['stress'] = {
        'fee_bp': FEE_STRESS_BP,
        'a': stress['comparison']['a'], 'b': stress['comparison']['b'],
        'delta_terminal_return': stress['comparison']['delta_terminal_return']}
    base['verdict'] = verdict(base['comparison'])
    base['registration_sha256'] = _registration_digest()
    return base


def _registration_digest() -> str:
    import hashlib
    path = Path(__file__).resolve().parents[2] / 'docs/preregistrations/ENTRY-BOTTOM-20260921.json'
    if not path.exists():
        raise ValueError(f'PREREGISTRATION_MISSING:{path}')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def slim(result: dict) -> dict:
    """去掉无法序列化、且报告用不到的原始对象（逐日行情、事件流、账户状态）。"""
    out = {k: v for k, v in result.items() if k not in ('a', 'b')}
    return out


def main(argv=None) -> int:
    import argparse
    import time

    from scripts.strategy_research import report

    data_root = Path('/Users/wh1817w/quant/quant_us-main/data')
    ap = argparse.ArgumentParser(prog='strategy_research.entry_arms')
    ap.add_argument('--study', default=str(data_root / 'strategy_diagnostics' /
                                          'SD-P0P1-20260921-012'))
    ap.add_argument('--out', default=str(data_root / 'strategy_research' /
                                         'SR-BOTTOM-20260921-001'))
    ap.add_argument('--limit', type=int, default=None)
    args = ap.parse_args(argv)
    started = time.time()
    result = full_study(Path(args.study), limit=args.limit)
    result['elapsed_seconds'] = round(time.time() - started, 1)
    written = report.write_entry_artifacts(slim(result), Path(args.out))
    print(json.dumps({'verdict': result['verdict']['token'],
                      'delta_terminal': round(result['comparison']['delta_terminal_return'], 4),
                      'delta_terminal_2x': round(
                          result['comparison']['delta_terminal_return_2x'], 4),
                      'elapsed_seconds': result['elapsed_seconds'], **written},
                     ensure_ascii=False))
    return 0


def _signal_summary(run: dict) -> dict:
    return {'n_opportunities': len(run['opportunities']),
            'by_security': dict(Counter(o.security_id for o in run['opportunities'])),
            'by_year': dict(sorted(Counter(o.signal_session[:4]
                                           for o in run['opportunities']).items())),
            'state_counts': dict(Counter(s['state'] for s in (run['signals'] or []))),
            'reason_counts': dict(Counter(s['reason'] for s in (run['signals'] or [])))}


if __name__ == '__main__':
    raise SystemExit(main())
