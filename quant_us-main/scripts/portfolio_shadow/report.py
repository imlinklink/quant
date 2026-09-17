"""日报 + 配对绩效（PR6）。

日报对应设计 §8 最小看板（运行状态/漏斗/账户净值与回撤/持仓风险/VETO·ABSTAIN 原因/成本/异常）；
配对绩效对应 §9（L−R 全成本收益差、MDD 差、暴露差、VETO/ABSTAIN 计数）。金额在报告层转回美元 float。
"""
from __future__ import annotations

import pandas as pd

from .store import ShadowStore, state_from_dict


def _to_dollars(micro) -> float | None:
    return None if micro is None else micro / 1_000_000


def _max_drawdown(navs: list[dict], key: str = 'equity', initial: int | None = None) -> float | None:
    if not navs:
        return None
    # 高点包含初始资金（设计 §2：DD = 1 − NAV / 历史高点，高点含初始资金）
    peak = initial if initial is not None else navs[0][key]
    mdd = 0.0
    for n in navs:
        peak = max(peak, n[key])
        mdd = min(mdd, (n[key] - peak) / peak)
    return mdd


def daily_report(store: ShadowStore, manifest) -> dict:
    report = {'experiment_id': manifest.experiment_id, 'status': manifest.status, 'accounts': {}}
    for scope in manifest.account_scopes:
        row = store.latest_state(scope)
        if row is None:
            report['accounts'][scope] = {'status': 'no_state'}
            continue
        seq, saved = row
        state = state_from_dict(saved)
        navs = store.daily_nav(scope)
        latest = navs[-1] if navs else {}
        high_water = state.high_water
        drawdown = ((latest.get('equity', state.initial_equity) - high_water) / high_water
                    if high_water else None)
        report['accounts'][scope] = {
            'sequence': seq, 'session': state.last_session,
            'cash_available': _to_dollars(state.cash_available),
            'equity': _to_dollars(latest.get('equity')),
            'full_cost_equity': _to_dollars(latest.get('full_cost_equity')),
            'model_cost': _to_dollars(state.model_cost),
            # 成本不可知时全成本净值只是上界，不能宣称已完整扣费
            'cost_status': state.cost_status,
            'model_cost_uncertain_count': state.model_cost_uncertain_count,
            'drawdown': drawdown,
            'valuation_status': state.valuation_status,
            'invariant_violations': state.invariants(),
            'positions': {sid: {'shares': p.shares,
                                'entry_price': _to_dollars(p.entry_price_micro),
                                'stop': _to_dollars(p.stop_micro),
                                'holding_sessions': p.holding_sessions}
                          for sid, p in sorted(state.positions.items())},
        }
    opps = store.opportunities()
    funnel = {}
    for o in opps:
        t = o.get('terminal', 'WAITING')
        funnel[t] = funnel.get(t, 0) + 1
    report['funnel'] = {'total': len(opps), 'by_terminal': funnel}
    apps = store.applications()
    by_scope_action, by_reason = {}, {}
    for a in apps:
        k = f'{a["scope"].rsplit(":", 1)[-1]}:{a["action"]}'
        by_scope_action[k] = by_scope_action.get(k, 0) + 1
        by_reason[a.get('reason_code', '')] = by_reason.get(a.get('reason_code', ''), 0) + 1
    report['applications'] = {'by_scope_action': by_scope_action, 'by_reason': by_reason}
    return report


def _norm_session(s) -> str:
    if isinstance(s, str):
        return s
    return str(pd.Timestamp(s).date())


def paired_performance(store: ShadowStore, manifest, calendar=None) -> dict:
    r_scope, l_scope = manifest.account_scopes[0], manifest.account_scopes[1]
    r_navs = {n['session']: n for n in store.daily_nav(r_scope)}
    l_navs = {n['session']: n for n in store.daily_nav(l_scope)}
    all_sessions = sorted(set(r_navs) | set(l_navs))
    # 连续完整前缀：从首个 session 起双方都是 OK；遇缺口/暂定即停，不跨过缺口继续
    if calendar is not None:
        # 提供交易日历时，按日历识别「双方同时漏掉的交易日」；缺即停
        cal = sorted(_norm_session(s) for s in calendar)
        if all_sessions:
            all_sessions = [s for s in cal if all_sessions[0] <= s <= all_sessions[-1]]
    common = []
    for s in all_sessions:
        r, l = r_navs.get(s), l_navs.get(s)
        if r is None or l is None or \
                r.get('valuation_status') == 'PROVISIONAL' or \
                l.get('valuation_status') == 'PROVISIONAL':
            break
        common.append(s)
    if not common:
        return {'common_sessions': 0, 'total_sessions': len(all_sessions),
                'excluded_after_gap': len(all_sessions)}
    r_series = [r_navs[s] for s in common]
    l_series = [l_navs[s] for s in common]
    initial = manifest.initial_cash
    r_final = r_series[-1].get('full_cost_equity', r_series[-1].get('equity', initial))
    l_final = l_series[-1].get('full_cost_equity', l_series[-1].get('equity', initial))
    r_ret = (r_final - initial) / initial
    l_ret = (l_final - initial) / initial
    r_exp = sum(n.get('gross_exposure', 0) for n in r_series) / len(r_series)
    l_exp = sum(n.get('gross_exposure', 0) for n in l_series) / len(l_series)

    def _count(scope, action):
        return sum(1 for a in store.applications(scope) if a['action'] == action)

    return {
        'common_sessions': len(common),
        'total_sessions': len(all_sessions),
        'excluded_after_gap': len(all_sessions) - len(common),
        'R_full_cost_return': r_ret,
        'L_full_cost_return': l_ret,
        'L_minus_R_return': l_ret - r_ret,
        'R_max_drawdown': _max_drawdown(r_series, 'full_cost_equity', initial=initial),
        'L_max_drawdown': _max_drawdown(l_series, 'full_cost_equity', initial=initial),
        'R_avg_exposure': r_exp / initial,
        'L_avg_exposure': l_exp / initial,
        'exposure_diff': (l_exp - r_exp) / initial,
        'L_VETO': _count(l_scope, 'VETO'), 'L_ABSTAIN': _count(l_scope, 'ABSTAIN'),
        'L_PASS': _count(l_scope, 'PASS'),
        'R_model_cost': _to_dollars(r_series[-1].get('model_cost', 0)),
        'L_model_cost': _to_dollars(l_series[-1].get('model_cost', 0)),
        # 成本口径：PROVISIONAL 时收益/MDD 都是「同一已执行交易路径下、待扣非负费用后的
        # 净值上界」，不是策略真实表现的上界 —— 注意区分，不要泛称。
        'R_cost_status': r_series[-1].get('cost_status', 'OK'),
        'L_cost_status': l_series[-1].get('cost_status', 'OK'),
        'L_model_cost_uncertain_count': l_series[-1].get('model_cost_uncertain_count', 0),
        'full_cost_is_upper_bound': (l_series[-1].get('cost_status') == 'PROVISIONAL' or
                                     r_series[-1].get('cost_status') == 'PROVISIONAL'),
    }


def _pct(value) -> str:
    """None 安全格式化：paired_performance 在无公共 session 时走早退分支，缺键。"""
    return 'n/a' if value is None else f'{value:.2%}'


def render_markdown(report: dict, paired: dict) -> str:
    lines = [f"# 实验 {report['experiment_id']} 日报（status {report['status']}）", '']
    for scope, acct in report['accounts'].items():
        if acct.get('status') == 'no_state':
            lines.append(f'- {scope}: 无状态')
            continue
        lines.append(f"## {scope}")
        lines.append(f"- session={acct['session']} 净值={acct['equity']:.2f} "
                     f"全成本净值={acct['full_cost_equity']:.2f} "
                     f"回撤={_pct(acct['drawdown'])}")
        cost_note = ''
        if acct.get('cost_status') == 'PROVISIONAL':
            cost_note = ('（暂定：扣除已知成本；同一已执行交易路径下、待扣非负费用后的净值上界）')
        lines.append(f"- model_cost={acct['model_cost']} 成本口径={acct.get('cost_status', 'OK')}"
                     f" 未结清={acct.get('model_cost_uncertain_count', 0)}{cost_note}")
        lines.append(f"- 估值={acct['valuation_status']} "
                     f"异常={acct['invariant_violations'] or '无'}")
        pos_str = ', '.join(f"{sid}×{p['shares']}" for sid, p in acct['positions'].items()) or '空'
        lines.append(f"- 持仓 {len(acct['positions'])}: {pos_str}")
        lines.append('')
    lines.append('## 漏斗')
    lines.append(f"- total={report['funnel']['total']} {report['funnel']['by_terminal']}")
    lines.append('## 应用动作')
    lines.append(f"- {report['applications']['by_scope_action']}")
    lines.append(f"- 原因 {report['applications']['by_reason']}")
    lines.append('')
    lines.append('## 配对绩效')
    lines.append(f"- 共同 session={paired.get('common_sessions')}")
    lines.append(f"- L−R 全成本收益差={_pct(paired.get('L_minus_R_return'))}")
    lines.append(f"- R MDD={_pct(paired.get('R_max_drawdown'))} "
                 f"L MDD={_pct(paired.get('L_max_drawdown'))}")
    lines.append(f"- VETO={paired.get('L_VETO')} ABSTAIN={paired.get('L_ABSTAIN')} "
                 f"PASS={paired.get('L_PASS')}")
    if paired.get('full_cost_is_upper_bound'):
        lines.append('- 成本口径 PROVISIONAL：上列收益/MDD 为「同一已执行交易路径下、'
                     '待扣非负费用后的净值上界」，不是策略真实表现的上界')
    return '\n'.join(lines)
