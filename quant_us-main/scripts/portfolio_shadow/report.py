"""日报 + 配对绩效（PR6）。

日报对应设计 §8 最小看板（运行状态/漏斗/账户净值与回撤/持仓风险/VETO·ABSTAIN 原因/成本/异常）；
配对绩效对应 §9（L−R 全成本收益差、MDD 差、暴露差、VETO/ABSTAIN 计数）。金额在报告层转回美元 float。
"""
from __future__ import annotations

import pandas as pd

from .llm_overlay import is_program_abstain, model_call_expected, program_abstain_class
from .store import SHADOW_SCHEMA_VERSION, ShadowStore, state_from_dict


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
    report = {'experiment_id': manifest.experiment_id, 'status': manifest.status, 'accounts': {},
              # 披露模型训练数据截止：它决定了哪些 session 上的 LLM 决策是可信的
              'llm_policy': dict(manifest.llm_policy), 'schema_version': SHADOW_SCHEMA_VERSION}
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
    report['entry_metrics'] = entry_metrics(store, manifest)
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

    l_apps = store.applications(l_scope)
    l_program = sum(1 for a in l_apps if is_program_abstain(a.get('reason_code') or ''))

    return {
        'common_sessions': len(common),
        'total_sessions': len(all_sessions),
        'excluded_after_gap': len(all_sessions) - len(common),
        # 「模型从未参与」的应用数：这些机会上 L 走的是 ABSTAIN=采用父策略，与 R **同路**，
        # 配对为**退化**、不携带任何模型信息。单列出来，否则一次调度缺口会被读成「模型无价值」。
        'L_program_abstain_applications': l_program,
        'L_model_informed_applications': len(l_apps) - l_program,
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
    llm = report.get('llm_policy') or {}
    lines = [f"# 实验 {report['experiment_id']} 日报（status {report['status']}）",
             f"- 账本 schema=v{report.get('schema_version')} overlay={llm.get('overlay')} "
             f"证据等级={llm.get('evidence_mode') or 'n/a'} "
             f"模型知识截止={llm.get('knowledge_cutoff') or '未声明（非真实模型路径）'}",
             '']
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
    if paired.get('L_program_abstain_applications'):
        lines.append(f"- **L 有 {paired['L_program_abstain_applications']} 个应用是程序侧弃权"
                     f"（模型从未参与，L 采用父策略、与 R 同路）—— 这些配对是**退化**的，"
                     f"不能用来判断模型的贡献；模型知情的应用 "
                     f"{paired.get('L_model_informed_applications')} 个")
    lines.append(f"- R MDD={_pct(paired.get('R_max_drawdown'))} "
                 f"L MDD={_pct(paired.get('L_max_drawdown'))}")
    lines.append(f"- VETO={paired.get('L_VETO')} ABSTAIN={paired.get('L_ABSTAIN')} "
                 f"PASS={paired.get('L_PASS')}")
    if paired.get('full_cost_is_upper_bound'):
        lines.append('- 成本口径 PROVISIONAL：上列收益/MDD 为「同一已执行交易路径下、'
                     '待扣非负费用后的净值上界」，不是策略真实表现的上界')
    metrics = report.get('entry_metrics')
    if metrics:
        lines.append('')
        lines.append('## 入场三项指标（设计 §8）')
        lines.append(f"- 有效真实评审覆盖率="
                     f"{_pct(metrics.get('real_review_coverage'))} "
                     f"（真实评审 {metrics.get('real_model_reviews')} / 可评审 "
                     f"{metrics.get('callable_for_model')}）")
        lines.append(f"- 实际交易计划改变率="
                     f"{_pct(metrics.get('plan_change_rate'))} "
                     f"（改变 {metrics.get('plan_change_count')} / 模型知情 "
                     f"{metrics.get('model_informed_applications')}）")
        lines.append(f"- 决策到执行可追踪完成率="
                     f"{_pct(metrics.get('trackable_completion_rate'))} "
                     f"（完成 {metrics.get('execution_completed')} + 明确失败 "
                     f"{metrics.get('execution_failed_terminal')} / 已批准 "
                     f"{metrics.get('approved_applications')}）")
        lines.append(f"- 漏斗：应评审 {metrics.get('eligible_for_review')} = 数据拦截 "
                     f"{metrics.get('data_blocked')} + 质量弃权 {metrics.get('quality_abstain')} "
                     f"+ 可评审 {metrics.get('callable_for_model')}；已评审 "
                     f"{metrics.get('reviewed')}（实际调用 {metrics.get('actual_calls')}，"
                     f"夹具 {metrics.get('fixture_reviews')}）；故障降级 "
                     f"{metrics.get('failure_abstains')}")
        if metrics.get('veto_unapplied'):
            lines.append(f"- **注意**：{metrics['veto_unapplied']} 次 VETO 已冻结但未落到执行 "
                         f"（账户照样买入）—— 否决必须兑现，否则等于没发生")
        if metrics.get('note'):
            lines.append(f"- {metrics['note']}")
    return '\n'.join(lines)


# 夹具模型不是"真实评审"：覆盖率统计必须把它们排除，否则会用一个假数字冒充已接入真实模型
FIXTURE_MODEL_IDS = ('', 'fixture', None)


def _is_real_review(attempt: dict | None, reason_code: str = '') -> bool:
    """有效真实评审：真实模型、确实发起了调用（非质量门短路）、且**判断真的生效**。

    「网络成功」不等于「按期取得有效判断」：被降级成程序侧 ABSTAIN 的尝试（超时、失败、
    无效输出、晚到、评审窗口已过）不得计入覆盖率 —— 那是故障，不是模型的一次表态。
    """
    if not attempt:
        return False
    if attempt.get('model_id') in FIXTURE_MODEL_IDS:
        return False
    if attempt.get('gated'):
        return False
    if is_program_abstain(reason_code):
        return False
    return attempt.get('status') == 'COMPLETED'


def decision_trace(store, opportunity_id: str, scopes=()) -> dict:
    """单项决策追踪（设计 §8）：规则原计划 → 证据 → 模型动作 → 最终动作 → 执行结果 → 费用。"""
    opp = store.opportunity(opportunity_id) or {}
    packet = store.packet_for_opportunity(opportunity_id) or {}
    trace = {
        'opportunity_id': opportunity_id,
        'security_id': opp.get('security_id'),
        'signal_session': opp.get('signal_session'),
        'planned_execution_session': opp.get('planned_execution_session'),
        'rule_plan': packet.get('rule_plan', {}),
        'market_context': packet.get('market_context', {}),
        'evidence_mode': (packet.get('evidence') or {}).get('evidence_mode'),
        'data_quality': packet.get('data_quality', {}),
        'events': [{'summary': e.get('summary'), 'excerpt': e.get('excerpt'),
                    'source_url': e.get('source_url'),
                    'published_at': e.get('published_at'),
                    'observed_at': e.get('observed_at')}
                   for e in packet.get('events', [])],
        'terminal': store.opportunity_terminals().get(opportunity_id, 'READY'),
        'accounts': {},
    }
    for scope in scopes:
        app = store.application(scope, opportunity_id) or {}
        attempt = store.job_run(app['decision_id']) if app.get('decision_id') else None
        raw = app.get('raw_action') or ''
        trace['accounts'][scope] = {
            'action': app.get('action'),
            'reason_code': app.get('reason_code'),
            'raw_action': raw,
            # 降级原因：模型原始动作 ≠ 最终动作，说明被校验/超时/迟到降级了
            'degraded_from': raw if raw and raw != app.get('action') else None,
            'late_response_observed': app.get('late_response_observed', False),
            'decision_frozen': app.get('decision_frozen', False),
            'execution_applied': app.get('execution_applied', False),
            'model_cost': _to_dollars(app.get('model_cost', 0)),
            'cost_uncertain': app.get('cost_uncertain', False),
            'attempt_status': (attempt or {}).get('status'),
            'attempt_errors': (attempt or {}).get('validation_errors'),
            'model_id': (attempt or {}).get('model_id'),
        }
    return trace


def entry_metrics(store, manifest) -> dict:
    """设计 §8 的三项首版指标（入场），按 §4.3 要求把口径分列。

    无候选或全 PASS **不是失败**，也不能冒充已经观察到真实 VETO 价值 —— 所以覆盖率与
    影响率在分母为 0 时返回 None 而不是 0，「没有对象可评」和「评了但全放行」必须能区分。

    核心是把**模型有没有参与**与**执行上发生了什么**分成两套数：

      · 模型可评审分母 = 应评审 − 数据拦截 − 质量弃权。BLOCK 是数据问题、不是模型的失败，
        算进分母等于让数据质量冒充模型表现（§4.3：BLOCK 排除出分母，但单列数据阻断率）。
      · 计划改变率的分母 = **模型真正参与过判断**的应用，不是全部应用。
      · 执行侧「放行 / 成交」照实统计，另列其中「模型知情」的那一份。

    否则一次调度缺口（评审窗口错过 ⇒ 程序侧 ABSTAIN ⇒ L 采用父策略）在账上与「模型自己
    弃权」完全一样，会被读成「模型没有价值」—— 而真相是模型从未参与。
    """
    l_scope = next((s for s in manifest.account_scopes if s.endswith(':L')), None)
    terminals = store.opportunity_terminals()
    n = dict(eligible=0, data_blocked=0, quality_abstain=0, callable=0, reviewed=0,
             calls=0, real_reviews=0, fixture_reviews=0, failure_abstains=0,
             model_informed=0, changed=0, veto_applied=0, veto_unapplied=0,
             approved=0, completed=0, failed_terminal=0)
    for oid, _body in store.opportunity_rows():
        packet = store.packet_for_opportunity(oid)
        if packet is None:
            continue                      # 还没进入 prepare 阶段，不算「应评审」
        n['eligible'] += 1
        app = store.application(l_scope, oid) if l_scope else None
        reason = (app or {}).get('reason_code') or ''
        kind = program_abstain_class(reason)
        level = (packet.get('data_quality') or {}).get('level')
        # 机会级分类：包的质量门是「模型能不能被调用」的权威，与账户级 reason 取并集且不重复计
        if level == 'BLOCK' or kind == 'data_blocked':
            n['data_blocked'] += 1
        elif level == 'LLM_INSUFFICIENT' or kind == 'quality_abstain':
            n['quality_abstain'] += 1
        else:
            n['callable'] += 1
        if kind == 'failure':
            n['failure_abstains'] += 1
        if app is None:
            continue                      # 包已冻结但还没评审
        n['reviewed'] += 1
        attempt = store.job_run(app['decision_id']) if app.get('decision_id') else None
        if attempt and not attempt.get('gated'):
            n['calls'] += 1
        if _is_real_review(attempt, reason):
            n['real_reviews'] += 1
        elif attempt and attempt.get('model_id') in FIXTURE_MODEL_IDS:
            n['fixture_reviews'] += 1
        if not app.get('decision_frozen'):
            continue
        action = app.get('action')
        if not is_program_abstain(reason):
            n['model_informed'] += 1
            if action == 'VETO':
                n['changed'] += 1         # 因 LLM 而改变最终计划
                # VETO 必须落到执行上：冻结了否决而账户照样买入，是最该看见的形态
                if app.get('execution_applied'):
                    n['veto_applied'] += 1
                else:
                    n['veto_unapplied'] += 1
        if action not in ('VETO', 'BLOCK', 'DATA_BLOCKED'):
            n['approved'] += 1
            if app.get('execution_applied'):
                n['completed'] += 1
            elif terminals.get(oid) == 'MISSED_EXECUTION':
                n['failed_terminal'] += 1  # 明确失败终态也算「可追踪完成」
    return {
        # 漏斗（相加为应评审：data_blocked + quality_abstain + callable == eligible）
        'eligible_for_review': n['eligible'],
        'data_blocked': n['data_blocked'],
        'quality_abstain': n['quality_abstain'],
        'callable_for_model': n['callable'],
        'reviewed': n['reviewed'],
        'actual_calls': n['calls'],
        'real_model_reviews': n['real_reviews'],
        'fixture_reviews': n['fixture_reviews'],
        'failure_abstains': n['failure_abstains'],
        'model_informed_applications': n['model_informed'],
        # 比率：分母一律是「该有判断的对象」，不是「碰过的对象」
        'real_review_coverage': (n['real_reviews'] / n['callable']) if n['callable'] else None,
        'data_block_rate': (n['data_blocked'] / n['eligible']) if n['eligible'] else None,
        'plan_change_count': n['changed'],
        'plan_change_rate': ((n['changed'] / n['model_informed'])
                             if n['model_informed'] else None),
        # 执行侧
        'approved_applications': n['approved'],
        'veto_applied': n['veto_applied'],
        'veto_unapplied': n['veto_unapplied'],
        'execution_completed': n['completed'],
        'execution_failed_terminal': n['failed_terminal'],
        'trackable_completion_rate': (((n['completed'] + n['failed_terminal']) / n['approved'])
                                      if n['approved'] else None),
        'note': ('无候选或全部为夹具评审：这不构成失败，也不能冒充已观察到真实 VETO 价值'
                 if n['real_reviews'] == 0 else ''),
    }


def render_trace(trace: dict) -> str:
    """单项决策追踪的 Markdown（设计 §8：规则原计划 → 证据 → 模型动作 → 执行结果）。"""
    plan = trace.get('rule_plan') or {}
    lines = [f"### 决策追踪 {trace.get('security_id')} "
             f"{trace.get('signal_session')} → {trace.get('planned_execution_session')}",
             f"- 规则原计划：{plan.get('parent_strategy_id')}/{plan.get('parent_version')} "
             f"入场={plan.get('entry_rule')} 原因={plan.get('rule_reason_codes')} "
             f"止损基准={plan.get('stop_reference')} 退出={plan.get('exit_policy_id')} "
             f"截止={plan.get('decision_deadline')}",
             f"- 证据（{trace.get('evidence_mode')}，{len(trace.get('events') or [])} 条）："
             + ('；'.join(f"{(e.get('summary') or '')[:40]}"
                          f"（{e.get('source_url') or '无链接'}）"
                          for e in (trace.get('events') or [])) or '无'),
             f"- 数据质量：{trace.get('data_quality')}",
             f"- 机会终态：{trace.get('terminal')}"]
    for scope, acct in (trace.get('accounts') or {}).items():
        lines.append(f"- {scope}：最终动作={acct.get('action')} "
                     f"原因={acct.get('reason_code')} "
                     f"模型原始动作={acct.get('raw_action') or '(未调用)'} "
                     f"降级自={acct.get('degraded_from') or '无'} "
                     f"尝试状态={acct.get('attempt_status')} "
                     f"错误={acct.get('attempt_errors') or '无'}")
        lines.append(f"  冻结={acct.get('decision_frozen')} 成交={acct.get('execution_applied')} "
                     f"费用={acct.get('model_cost')}"
                     f"{'（成本未知，待补记）' if acct.get('cost_uncertain') else ''}")
    return '\n'.join(lines)
