"""只读展示投影。以账本身份关联，不调用模型、不更改实验的正式评估协议。"""
from __future__ import annotations

from . import contract as C
from .vocabulary import PINNED_VOCABULARY as V


def base_opportunity(key):
    return str(key or '').split('@pos:', 1)[0]


def decision_rows(store, forward_start=None):
    rows = []
    events = {s: store.events(s) for s in {a['scope'] for a in store.applications()}}
    for app in store.applications():
        key = app['opportunity_id']
        packet = store.packet_for_opportunity(key) or {}
        identity = packet.get('identity') or {}
        opp = store.opportunity(base_opportunity(key)) or {}
        position = '@pos:' in key
        session = (identity.get('execution_session') or
                   (key.split('@pos:', 1)[1] if position else opp.get('planned_execution_session'))
                   or str(app.get('as_of') or '')[:10])
        attempt = store.job_run(app['decision_id']) if app.get('decision_id') else None
        valid = bool(attempt and attempt.get('model_id')
                     and attempt['model_id'] not in V.FIXTURE_MODEL_IDS
                     and attempt.get('status') == 'COMPLETED'
                     and not attempt.get('gated') and not attempt.get('validation_errors')
                     and not V.is_program_abstain(app.get('reason_code') or ''))
        linked = [e for e in events[app['scope']]
                  if (position and app.get('decision_id') and
                      e.get('decision_id') == app['decision_id']) or
                  (position and e.get('opportunity_id') == key) or
                  (not position and e.get('opportunity_id') == key)]
        fills = [e for e in linked if e.get('type') == 'fill']
        missed = [e.get('reason') for e in linked if e.get('type') == 'missed']
        action = app.get('action') or ''
        changed = (bool(fills) if position else
                   action == 'VETO' and bool(app.get('execution_applied')))
        rows.append({
            **app, 'role': 'position' if position else 'entry',
            'security_id': identity.get('security_id') or opp.get('security_id'),
            'parent_opportunity_id': base_opportunity(key), 'session': session,
            'rule_action': '继续持有（硬退出优先）' if position else '按规则尝试入场',
            'model_action': app.get('raw_action') or (action if valid else None),
            'model_id': (attempt or {}).get('model_id'), 'valid_real_review': valid,
            'phase': ('unknown' if not forward_start or not session else
                      'forward' if session >= forward_start else 'replay'),
            'fills': fills, 'execution_reasons': missed, 'path_changed': changed,
            'model_cost_usd': (None if app.get('cost_uncertain') or app.get('model_cost') is None
                               else app['model_cost'] / 1e6),
            'execution_status': ('已成交' if fills else '执行已处理（不等于成交）'
                                 if app.get('execution_applied') else '等待执行或核对'),
            'maturity': C.PENDING_SETTLEMENT,
        })
    return sorted(rows, key=lambda r: (r['session'], r['scope'], r['opportunity_id']))


def lifecycle_results(events):
    """逐机会成交现金流；拆股和分红按同证券当时唯一存续生命周期关联。"""
    trades, active = {}, {}
    for e in events:
        sid, typ = e.get('security_id'), e.get('type')
        if typ == 'fill':
            oid = e.get('opportunity_id')
            if not oid:
                continue
            if e.get('side') == 'BUY':
                # 同一机会重复入场不猜测生命周期，拒绝归因。
                if oid in trades or sid in active:
                    if oid in trades:
                        trades[oid]['invalid'] = True
                    if sid in active:
                        trades[active[sid]]['invalid'] = True
                    continue
                trades[oid] = {'opportunity_id': oid, 'security_id': sid,
                               'shares': 0, 'pnl_micro': 0, 'invalid': False,
                               'entry_session': e['session'], 'exit_session': None,
                               'closed': False, 'fills': []}
                active[sid] = oid
            t = trades.get(oid)
            if not t:
                continue
            if any(e.get(k) is None for k in ('shares', 'price_micro', 'fee_micro')):
                t['invalid'] = True
                continue
            buy = e['side'] == 'BUY'
            t['shares'] += e['shares'] * (1 if buy else -1)
            t['pnl_micro'] += e['shares'] * e['price_micro'] * (-1 if buy else 1) - e['fee_micro']
            t['fills'].append(e)
            if t['shares'] < 0:
                t['invalid'] = True
            if not buy and t['shares'] == 0:
                t.update(closed=True, exit_session=e['session'])
                if active.get(sid) == oid:
                    del active[sid]
        elif sid in active:
            t = trades[active[sid]]
            if typ == 'dividend_record':
                if e.get('total_micro') is None:
                    t['invalid'] = True
                else:
                    t['pnl_micro'] += e['total_micro']  # 应收计一次，支付日不重复计
            elif typ == 'split':
                ratio = e.get('ratio')
                if not ratio or e.get('kind') not in ('split', 'reverse_split'):
                    t['invalid'] = True
                elif e['kind'] == 'split':
                    t['shares'] *= ratio
                elif t['shares'] % ratio:
                    t['invalid'] = True  # 碎股现金处理不明，不猜收益
                else:
                    t['shares'] //= ratio
    return trades


def contribution_cases(store, scopes, rows):
    """按已完成的机会生命周期对照；不把一笔结果分摊给多次模型评审。"""
    r = next((s for s in scopes if s.endswith(':R')), None)
    l = next((s for s in scopes if s.endswith(':L')), None)
    if not r or not l:
        return {'status': C.NOT_APPLICABLE, 'best': [], 'worst': [], 'all': [],
                'why': '没有同实验的 R/L 对照账户'}
    rt, lt = lifecycle_results(store.events(r)), lifecycle_results(store.events(l))
    all_cases, pending, excluded = [], 0, 0
    groups = {}
    for row in rows:
        if row['scope'] == l:
            groups.setdefault(row['parent_opportunity_id'], []).append(row)
    for oid, reviews in groups.items():
        interventions = [a for a in reviews if a['valid_real_review'] and a['path_changed']
                         and a['phase'] == 'forward']
        if not interventions:
            continue
        a, b = rt.get(oid), lt.get(oid)
        veto = any(x['action'] == 'VETO' and x.get('execution_applied') for x in interventions)
        if not a or not a['closed'] or (b and not b['closed']) or (b is None and not veto):
            pending += 1
            continue
        if a['invalid'] or (b and b['invalid']) or any(
                x.get('cost_uncertain') or x.get('model_cost') is None for x in reviews):
            excluded += 1
            continue
        cost = sum(x['model_cost'] for x in reviews)
        lpnl = (b['pnl_micro'] if b else 0) - cost
        difference = (lpnl - a['pnl_micro']) / 1e6
        all_cases.append({
            'opportunity_id': oid, 'security_id': a['security_id'],
            'decision_keys': [x['opportunity_id'] for x in interventions],
            'R_pnl_usd': a['pnl_micro'] / 1e6, 'L_pnl_usd': lpnl / 1e6,
            'delta_usd': difference, 'model_cost_usd': cost / 1e6,
            'R_exit_session': a['exit_session'], 'L_exit_session': b['exit_session'] if b else None,
            'early_exit': bool(b and b['exit_session'] < a['exit_session'] and any(
                f.get('reason') == 'REVIEW_EXIT' for f in b['fills'])),
            'status': C.OK,
        })
    early = [c for c in all_cases if c['early_exit']]
    return {'status': C.OK if all_cases else C.PENDING_SETTLEMENT if pending else C.NO_OBJECT,
            'best': sorted((c for c in all_cases if c['delta_usd'] > 0),
                           key=lambda c: -c['delta_usd'])[:5],
            'worst': sorted((c for c in all_cases if c['delta_usd'] < 0),
                            key=lambda c: c['delta_usd'])[:5],
            'all': all_cases, 'mature_count': len(all_cases), 'pending_count': pending,
            'excluded_count': excluded,
            'why': '仅统计真实前向且改变执行的机会；双方平仓（或 L 否决且 R 平仓）后成熟。'
                   '含交易费、该机会全部已知模型费与应收分红；不是账户超额收益，也不是单次评审因果贡献。'
                   '成本未知或生命周期不完整的案例不排名。',
            'early_exit_harm': {'status': C.OK if early else C.NO_OBJECT,
                                'cases': early,
                                'why': '观察终点固定为同机会双方实际退出完成；只比较已实现生命周期结果，'
                                       '不挑选卖后有利日期，不把机械策略 S1 的结果当 LLM 贡献。'}}


def forward_performance(store, manifest, start, report):
    """展示用前向窗口；以起点前一条净值为分母，不改实验正式账本/评估。"""
    if len(manifest.account_scopes) < 2:
        return {'status': C.NOT_APPLICABLE, 'applicable': False, 'why': '无 R/L 对照'}
    if not start:
        return {'status': C.NOT_COLLECTED, 'common_sessions': 0, 'why': '未声明前向起点'}
    r, l = manifest.account_scopes[:2]
    series = [{n['session']: n for n in store.daily_nav(s)} for s in (r, l)]
    sessions = sorted(s for s in set(series[0]) | set(series[1]) if s >= start)
    anchors = []
    for nav in series:
        before = sorted(s for s in nav if s < start)
        anchor = nav[before[-1]] if before else {}
        anchors.append(anchor.get('full_cost_equity', manifest.initial_cash))
        if before and (anchor.get('valuation_status') != 'OK' or anchors[-1] is None):
            return {'status': C.NOT_COLLECTED, 'common_sessions': 0, 'why': '前向起点估值不完整'}
    common = []
    for s in sessions:
        if any(s not in ns or ns[s].get('valuation_status') != 'OK'
               or ns[s].get('full_cost_equity') is None for ns in series):
            break
        common.append(s)
    result = {'status': C.OK if common else C.NO_OBJECT, 'common_sessions': len(common),
              'total_sessions': len(sessions), 'excluded_after_gap': len(sessions) - len(common),
              'window_start': start, 'window_end': common[-1] if common else None,
              'why': '展示口径：前向起点前净值为各账户分母；无回放时使用初始资金。'
                     '遇单侧缺失或估值不完整停止；不替代登记的正式绩效判定。'}
    if not common or any(v is None or v <= 0 for v in anchors):
        return result
    for idx, label in enumerate(('R', 'L')):
        ns = [series[idx][s] for s in common]
        result[f'{label}_full_cost_return'] = ns[-1]['full_cost_equity'] / anchors[idx] - 1
        result[f'{label}_max_drawdown'] = report._max_drawdown(ns, 'full_cost_equity', anchors[idx])
        result[f'{label}_avg_exposure'] = sum(n.get('gross_exposure', 0) for n in ns) / len(ns) / anchors[idx]
    result['L_minus_R_return'] = result['L_full_cost_return'] - result['R_full_cost_return']
    result['full_cost_is_upper_bound'] = any(series[i][s].get('cost_status') != 'OK'
                                           for i in (0, 1) for s in common)
    result['exposure_diff'] = result['L_avg_exposure'] - result['R_avg_exposure']
    return result
