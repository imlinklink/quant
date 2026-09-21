"""P0/P1 baseline replay with a paired observer-off control. No model calls."""
from __future__ import annotations
from collections import Counter, defaultdict
from dataclasses import replace
import fcntl
import json
import sqlite3
from pathlib import Path

import pandas as pd

from scripts.portfolio_shadow.candidate_adapter import IncrementalCandidateGenerator, audit_stamps
from scripts.portfolio_shadow.cli import manifest_from_dict, _manifest_to_public
from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.replay import replay
from scripts.portfolio_shadow.schema import to_micro
from scripts.portfolio_shadow.store import ShadowStore
from scripts.live_trading.decision_ledger.event_store import digest
from . import baseline_parity
from . import manifest as study_manifest
from .inputs import load
from .funnel import Funnel, clean
from .exit_attribution import trades_from_events, summarize
from .statistics import annual_returns, capacity_summary, concentration, robustness

# §9.3 的结果判定枚举（闭集）。**这些是"判定"，不是"运行状态"**：只有把 challenger 与
# baseline 在同一口径上比过之后才谈得上取其中一个。P0/P1 只有基线，所以 `verdict` 是
# `None` —— 按本模块第一条纪律，算不出来就是 `None`，不是挑一个最接近的令牌充数
# （原先写的 `INSUFFICIENT_EVIDENCE` 根本不在这个枚举里，那是 §8.2 给 D1 用的词）。
VERDICT_TOKENS = ('DATA_INVALID', 'ENGINEERING_BLOCKED', 'INSUFFICIENT_SAMPLE',
                  'NO_IMPROVEMENT', 'RISK_REJECTED', 'CONCENTRATED', 'INCONCLUSIVE',
                  'EVIDENCE_SUPPORTED')
# §8.2 的分期结论：证据不足以选出问题 ⇒ D1 结束于此，不为完成项目任意改策略。
PHASE_CONCLUSION = 'INSUFFICIENT_EVIDENCE'


def shadow_actions(actions, universe=None, session_range=None):
    """把行动表转成影子引擎的形状。`universe` / `session_range` 限定**可能影响账户**的部分。

    **为什么要限定范围**：影子引擎要求拆股比是整数（`shares × ratio` 必须仍是整数股），
    非整数比会 `SHADOW_REQUIRES_INTEGER_ACTION_RATIO`。而真实行动表里的非整数比有三类，
    都不是"拆股"那么简单：

    · `2→3`（NVDA 2007）= 真实的 3:2 拆股，比值 1.5，合法但非整数；
    · `10000→19981`（GOOGL 2014）= 富途按实际股数换算记的，比值 1.9981；
    · `reform_type='Spin Off'`（DUK 2007、HON 2025-10-30）/ `'Reverse Split'` ——
      **分拆根本不是拆股**，它分的是另一家公司的股票，价格跌幅反映被分出去实体的价值。
      导入器目前把 `reform_type` 整个丢掉、一律写成 `split`（已知缺陷）。

    两个范围都必须限定，缺一不可 —— 2026-09-20 两次实测各撞到一个：

    1. 只按证券过滤时被 **GOOGL 2014-04-03** 挡死：GOOGL 在宇宙里，但那条拆股在
       **十二年前**，对 2026 年的账户毫无影响；
    2. 不按证券过滤时被 **HON** 挡死：HON 的两条非整数行动在窗口内，但 HON 不在价格面板里。

    这是**收窄到真正相关的范围**，不是放宽守卫：一旦某条行动确实落在被交易的证券与区间内，
    比值照样严格校验（有测试钉死）。丢弃项带原因返回，由调用方记账，不静默消失。
    """
    result, dropped = [], []
    for a in actions.to_dict('records'):
        sid = str(a['security_id'])
        ex_date = str(pd.Timestamp(a['ex_date']).date())
        if universe is not None and sid not in universe:
            dropped.append({'security_id': sid, 'ex_date': ex_date,
                            'reason': 'OUTSIDE_TRADED_UNIVERSE'})
            continue
        if session_range is not None and not (session_range[0] <= ex_date <= session_range[1]):
            # 引擎按 session 逐日查 `acts[date]`，区间外的行动**永远不会被查到** ——
            # 过滤掉它们不改变任何会计结果，只是不再让无关的历史事件阻断运行。
            dropped.append({'security_id': sid, 'ex_date': ex_date,
                            'reason': 'OUTSIDE_TRADED_SESSIONS'})
            continue
        row = {'security_id': sid, 'ex_date': ex_date, 'action_type': a['action_type']}
        if a['action_type'] in ('split', 'reverse_split'):
            ratio = float(a['ratio'])
            if not ratio.is_integer() or ratio < 1:
                raise ValueError(f'SHADOW_REQUIRES_INTEGER_ACTION_RATIO:{sid}:{ex_date}:{ratio}')
            row['ratio'] = int(ratio)
        else:
            row['cash_amount_micro'] = to_micro(a['cash_amount'])
            # 派发日：`pay_date` 优先，回落 `effective_at` —— 富途直取的行动表把
            # `dividend_payable_date` 写在 `effective_at` 里（`ACTION_COLUMNS` 没有
            # `pay_date` 列）。两者都缺就是**真的不知道**，留给下面的守卫中止，
            # **不猜一个日期**：影子引擎拿它当字典键，猜出来的日期会把分红记到错的会话上。
            pay = a.get('pay_date')
            if pd.isna(pay):
                pay = a.get('effective_at')
            row['pay_date'] = str(pd.Timestamp(pay).date()) if pd.notna(pay) else None
        result.append(row)
    return result, dropped


def audit(path):
    data = study_manifest.verify(path)
    prices, market, calendar, quality, actions = load(data, Path(path).resolve().parent)
    w = data['research_window']
    sessions = calendar[(calendar >= w['start']) & (calendar <= w['end'])]
    missing = sorted(set(prices.security_id) - set(quality.loc[quality.quality_status.eq('verified'), 'security_id']))
    return {'status': 'AUDIT_COMPLETE', 'sessions': len(sessions),
            'securities': prices.security_id.nunique(), 'unverified_securities': missing,
            'window': w, 'source_kind': 'historical_reconstruction',
            'warnings': ['历史研究仅支持所选存续股票池，未证明无幸存者偏差。',
                         '未建模滑点；P0/P1不进行收益晋级。',
                         '公司行动覆盖取自冻结输入；缺支付日的持仓分红会阻断账户运行。',
                         '历史重建不代表生产运行成功；未载入真实调度日志。']}


def _uncovered(navs, trades, state, sessions, manifest):
    """对账**覆盖不到**的组件，逐条点名并量化。

    §2.2 的告诫是双向的：对账通过只证明两套引擎在同一件事上一致，不证明那件事是对的，
    更不证明**没被对账的组件**是对的。把它们列出来、给出占比，读的人才不会把
    `baseline_parity: 0 diffs` 读成"全口径对齐"。
    """
    risk = [n.get('risk_state') for n in navs]
    states = Counter(risk)
    prior = {str(navs[i - 1]['session']): navs[i - 1].get('risk_state')
             for i in range(1, len(navs))}
    entries = len(trades)
    under_reduced = sum(1 for t in trades
                        if prior.get(str(t['entry_session'])) == 'REDUCED')
    last = str(sessions[-1].date())
    outstanding = {k: v for k, v in (state.dividend_receivable or {}).items() if v}
    stuck = sorted(k for k in outstanding if k is not None and str(k) <= last)
    censored = sorted(k for k in outstanding if k is None or str(k) > last)
    return {
        # 历史引擎里没有回撤阶梯（`grep ladder|high_water portfolio_engine.py` 为空），
        # 故它只能被单元测试覆盖，拿不到第二实现的对账。
        'drawdown_ladder': {
            'covered_by_parity': False, 'sessions_by_state': dict(states),
            'entries_under_reduced_budget': under_reduced, 'entries': entries,
        },
        # 同理，下面这条只有一份实现（两个引擎同键同错），只有对比"应收是否结了"才看得见。
        'dividend_receivable_outstanding': {
            'stuck_within_window': stuck, 'censored_after_window': censored,
            'amount_micro': sum(outstanding.values()),
            'note': '两个引擎都按**精确日期**匹配支付日，故支付日落在非交易日的分红'
                    '永远转不成可用现金（钱仍在 NAV 里，只是不能再拿去建仓）。',
        },
        'config_divergences': [
            '行动覆盖门：本 study 未启用（`blocked={}`），历史研究基线启用了 audited blocked。',
            '窗口：本 study 用冻结输入的覆盖交集，历史研究基线用 P1/B2/B3 条目的 union 窗口。',
            '回撤阶梯：本 study 启用（取自冻结父 manifest），历史研究基线没有这个概念。',
            '条目来源：本 study 用增量生成器，对账用的是同输入的批处理矩阵（接受机制不同）。',
        ],
        'note': '以上各项**没有**第二实现可对账；在此如实列出，不得读成"已对齐"。',
    }


def run(path, variant='baseline'):
    if variant != 'baseline':
        raise ValueError('P0_P1_ONLY_BASELINE:challengers require a separately frozen protocol')
    path = Path(path).resolve()
    data = study_manifest.verify(path)
    with (path.parent / '.run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            (path.parent / 'complete.json').unlink(missing_ok=True)
            result = _run(path, data)
        except Exception as exc:
            with (path.parent / 'trial_registry.jsonl').open('a') as f:
                f.write(json.dumps({'event': 'run_failed', 'variant': variant,
                                    'reason': str(exc)}, ensure_ascii=False) + '\n')
            raise
    return result


def _run(path, data):
    root = path.parent
    prices, market, calendar, quality, actions = load(data, root)
    start, end = (pd.Timestamp(data['research_window'][k]) for k in ('start', 'end'))
    sessions = calendar[(calendar >= start) & (calendar <= end)]
    base = manifest_from_dict(study_manifest.read(root / data['input_index']['baseline'][0]['path']))
    exp_id = data['study_id'] + '-baseline'
    scope = f'SHADOW:{exp_id}:R'
    m = replace(base, experiment_id=exp_id, account_scopes=(scope,), status='FROZEN',
                llm_policy={'overlay': 'fixed_pass', 'use_real_model': False},
                start_session=str(sessions[0].date()), parent_code_hash=data['working_tree_hash'])
    if errors := m.validate():
        raise ValueError(';'.join(errors))
    ledger = root / 'variants/baseline/ledger.sqlite3'
    store = ShadowStore(ledger, exp_id)
    store.save_experiment(m)
    study_manifest.write_json(ledger.parent / 'manifest.json', _manifest_to_public(m))
    funnel = Funnel(data['study_id'], root / 'diagnostics.sqlite3')
    kwargs = dict(prices=prices, market=market, quality=quality, actions=actions, blocked={},
                  calendar=calendar, experiment_id=exp_id, parent_version=m.parent_version,
                  exit_policy_id=m.execution_policy['exit_policy_id'],
                  top_n=m.risk_policy.get('top_n', 5),
                  max_wait_sessions=m.execution_policy.get('max_wait_sessions', 20),
                  require_matured=False, parent_strategy_id=m.parent_strategy_id)
    gen = IncrementalCandidateGenerator(**kwargs, observation_sink=funnel)
    control = IncrementalCandidateGenerator(**kwargs)
    state = new_account_state(scope, m.initial_cash)
    ref_state = new_account_state(scope, m.initial_cash)
    schedule, events, navs, capacity = defaultdict(list), [], [], []
    # Earlier input bars are indicator warmup only. Both accounts begin flat.
    acts = defaultdict(list)
    converted, excluded_actions = shadow_actions(
        actions, universe=set(prices.security_id),
        session_range=(str(sessions[0].date()), str(sessions[-1].date())))
    for a in converted:
        acts[a['ex_date']].append(a)
    with sqlite3.connect(ledger) as con:
        saved = dict(con.execute('SELECT sequence,state_hash FROM shadow_account_state '
                                 'WHERE experiment_id=? AND scope=?', (exp_id, scope)).fetchall())
    checks = {'observer_opportunities_equal': True, 'observer_states_equal': True,
              'replay_equal': True, 'invariants': True, 'accounting_identity': True,
              # 这五个都是**本引擎自洽**的证明。跨实现的「基线对齐」由下面的
              # `baseline_parity` 给出 —— 两者都通过才谈得上 §15 P0。
              'baseline_definition': '同一冻结父策略、共同空仓起点、observer-off控制；'
                                     '跨引擎对账见 baseline_parity，未覆盖项见 uncovered_by_parity'}
    for session in sessions:
        date = str(session.date())
        fresh, reference = gen.opportunities_for(session), control.opportunities_for(session)
        if fresh != reference:
            raise ValueError(f'OBSERVER_CHANGED_OPPORTUNITIES:{date}')
        for o in fresh:
            store.put_opportunity(o)
            schedule[o.planned_execution_session].append(o)
        bars = {str(r.security_id): {k: to_micro(getattr(r, 'raw_' + k)) for k in ('open','high','low','close')}
                for r in prices[prices.session.eq(session)].itertuples(index=False)}
        for a in acts[date]:
            if a['security_id'] in state.positions and a['action_type'] == 'cash_dividend' and not a['pay_date']:
                raise ValueError(f'HELD_DIVIDEND_PAY_DATE_MISSING:{date}:{a["security_id"]}')
        due = schedule.pop(date, [])
        call = dict(session=date, bars=bars, corporate_actions=acts[date], intents=due,
                    manifest=m, fee_bp=data['cost_policy']['fee_bp'])
        result, ref = step(state, **call), step(ref_state, **call)
        if result.state.state_hash() != ref.state.state_hash():
            raise ValueError(f'OBSERVER_CHANGED_STATE:{date}')
        if result.state.invariants():
            raise ValueError(f'ACCOUNT_INVARIANTS:{result.state.invariants()}')
        if result.nav['valuation_status'] != 'OK':
            raise ValueError(f'HELD_PRICE_MISSING:{date}')
        mv = sum(p.shares * bars[sid]['close'] for sid, p in result.state.positions.items())
        cash = (result.state.cash_available + result.state.cash_reserved + result.state.unsettled_cash
                + sum(result.state.dividend_receivable.values()))
        if cash + mv != result.nav['equity']:
            raise ValueError('NAV_ACCOUNTING_MISMATCH')
        if result.state.sequence in saved:
            if saved[result.state.sequence] != result.state.state_hash():
                raise ValueError('RESUME_STATE_CONFLICT')
        else:
            store.save_state(scope, result.state, result.nav, result.events)
        events.extend(result.events)
        navs.append(result.nav)
        reasons = {e['opportunity_id']: e['reason'] for e in result.events if e['type'] == 'missed'}
        bought = {e['opportunity_id'] for e in result.events if e['type'] == 'fill' and e['side'] == 'BUY'}
        for o in due:
            reason = reasons.get(o.opportunity_id(), '')
            executed = o.opportunity_id() in bought
            if not executed and not reason:
                raise ValueError('UNEXPLAINED_UNFILLED_INTENT')
            funnel({'candidate_id': o.source_candidate_id, 'security_id': o.security_id,
                    'candidate_round': gen.round_for(o.source_candidate_id),
                    'session': date, 'stage': 'account_execution', 'result': 'pass' if executed else 'reject',
                    'primary_reason': reason, 'all_reasons': [reason] if reason else [],
                    'rule_version': m.parent_version, 'feature_values': {'opportunity_id': o.opportunity_id()},
                    'candidate_state': None, **audit_stamps(session)})
        capacity.append({'session': date, 'held_positions': len(result.state.positions),
                         'max_positions': m.risk_policy['max_positions'],
                         # equity / mv 记原始值：`capacity_summary` 要把风险占用折算成 bp，
                         # 而没有权益分母就只能靠两个比率反推，反推会在 equity=0 时炸。
                         'equity_micro': result.nav['equity'],
                         'market_value_micro': mv,
                         'cash_fraction': cash / result.nav['equity'] if result.nav['equity'] > 0 else None,
                         'exposure': mv / result.nav['equity'] if result.nav['equity'] > 0 else None,
                         'rejections': dict(Counter(reasons.values())),
                         'risk_used_micro': sum(p.shares * max(0, bars[sid]['close'] - p.stop_micro)
                                                for sid, p in result.state.positions.items())})
        state, ref_state = result.state, ref.state
    rebuilt = replay(scope, m.initial_cash, store.events(scope))
    if rebuilt.state_hash() != state.state_hash():
        raise ValueError('REPLAY_MISMATCH')
    trades = trades_from_events(events, state, prices, actions, calendar, sessions[-1])
    pnl = sum(t['net_pnl_micro'] for t in trades)
    if pnl != navs[-1]['equity'] - m.initial_cash:
        raise ValueError('TRADE_ACCOUNT_PNL_MISMATCH')
    peak, mdd = m.initial_cash, 0.
    for nav in navs:
        peak = max(peak, nav['full_cost_equity'])
        mdd = min(mdd, nav['full_cost_equity'] / peak - 1)
    # §7.2 容量与资金、§6.2 集中度、§12 稳健性。这些数据原先逐日采集了却从未聚合，
    # 于是报告里那几段是空的 —— 采集与报出是两件事。
    stats = {'capacity': capacity_summary(capacity, m.risk_policy),
             'concentration': concentration(trades, m.initial_cash),
             'robustness': robustness(trades),
             # 年度账户收益按**逐日净值**算；`robustness.by_exit_year` 是交易损益按退出年归集，
             # 跨年持仓会把整笔压到退出年，两者不是一回事，必须分开呈现。
             'annual': annual_returns(navs, m.initial_cash)}
    # §8.1 / §15 P0「基线对齐」：用**另一套引擎**在同一批冻结输入上重算一遍并逐日对账。
    # 上面那些自查（重放、不变量、NAV 恒等式）都只能证明"本引擎自洽"—— 一个两边共有的
    # 会计错误会同时通过全部自查。对账非零即抛，见 `baseline_parity.TOLERANCE_USD`。
    parity, baseline_entries = baseline_parity.evaluate(
        data, root, prices, quality, actions, trades,
        risk_policy=m.risk_policy, horizon=int(m.execution_policy['horizon']),
        initial_cash_micro=m.initial_cash)   # Manifest 存整数微美元，compare 内部折成美元
    checks['baseline_parity'] = parity
    checks['baseline_entries'] = baseline_entries
    # 对账**覆盖不到**的东西必须点名，否则"对齐了"会被读成全口径对齐。
    checks['uncovered_by_parity'] = _uncovered(navs, trades, state, sessions, m)
    # 没能证明"与旧基线一致"的 study 不构成 P0 基线（§15「失败则停在工程修复，不运行收益寻优」）。
    parity_ok = parity.get('status') == 'VERIFIED'
    result = {'study_id': data['study_id'], 'manifest_hash': data['manifest_hash'],
              'phase': 'P0_P1', 'status': 'DIAGNOSTIC_COMPLETE',
              # 只有基线 ⇒ §9.3 的判定还没轮到（不是"判定为样本不足"）。见 VERDICT_TOKENS。
              'verdict': None,
              # §8.2 的分期结论。**对账没通过就不是"证据不足"，是"工程未就绪"** ——
              # 前者是研究结论，后者是必须先修的东西，两者不能共用一个标签。
              'phase_conclusion': PHASE_CONCLUSION if parity_ok else 'ENGINEERING_BLOCKED',
              'checks': checks,
              'sessions': len(sessions), 'source_kind': 'historical_reconstruction',
              'full_cost_return': navs[-1]['full_cost_equity'] / m.initial_cash - 1,
              'max_drawdown': mdd, 'initial_cash_micro': m.initial_cash,
              'final_equity_micro': navs[-1]['equity'], 'fees_micro': state.fees,
              'funnel': funnel.summary(), 'exits': summarize(trades, m.initial_cash),
              'statistics': stats,
              'capacity': capacity, 'trades': trades,
              'pending_execution_at_window_end': sum(map(len, schedule.values())),
              # 被范围排除的行动**记账而不是丢弃**：它们对账户无影响，但读的人应该知道
              # "这份行动表里有多少条没被消费、为什么"，而不是以为全部生效了。
              'excluded_actions': {
                  'count': len(excluded_actions),
                  'by_reason': dict(Counter(d['reason'] for d in excluded_actions)),
                  'securities': sorted({d['security_id'] for d in excluded_actions})},
              'audit': audit(path),
              'limitations': ['单一基线不能证明过滤无效或退出过早；尚无challenger增量。',
                              '数据截止后的机会保持待执行，不标记调度失败。',
                              '未进行真实模型调用；没有LLM收益或晋级结论。']}
    study_manifest.write_json(root / 'candidate_summary.json', clean(result['funnel']))
    study_manifest.write_json(root / 'funnel_observations.json', funnel.observations())
    study_manifest.write_json(root / 'exit_diagnostics.json', clean(trades))
    study_manifest.write_json(root / 'statistics.json', clean(stats))
    study_manifest.write_json(root / 'checks.json', checks)
    study_manifest.write_json(root / 'comparison.json', clean(result))
    from .report import render
    (root / 'report.md').write_text(render(result), encoding='utf-8')
    study_manifest.write_json(root / 'complete.json', {'manifest_hash': data['manifest_hash'],
                                                      'state_hash': state.state_hash(),
                                                      'comparison_hash': study_manifest.file_hash(root / 'comparison.json')})
    with (root / 'trial_registry.jsonl').open('a') as stream:
        stream.write(json.dumps({'event': 'run_complete', 'variant': 'baseline',
                                 'manifest_hash': data['manifest_hash'], 'state_hash': state.state_hash()}) + '\n')
    return result
