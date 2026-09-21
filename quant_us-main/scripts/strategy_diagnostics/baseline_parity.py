"""P0 的「基线对齐」：同输入下把**另一套引擎**跑一遍，逐日对账。

设计 §8.1：「基线必须与已有冻结实验在共同输入与日期上对齐；不一致先解释，不开始寻优。」
§15 的 P0 验收把它写成「同输入与旧基线一致；确认没有数据/时序/会计阻塞」。

**为什么必须跨实现**：本模块自己的 `checks`（重放一致、账户不变量、NAV 恒等式）只能证明
"本引擎自洽" —— 一个两边共有的会计错误会同时通过全部自查。§2.2 已把这条写成对
`gs_backtest` 的告诫：「对账通过只能证明与冻结基准一致，不能排除双方共享的会计或时序错误」。
"历史研究矩阵"由 `simulate_multi_asset_portfolio` 产出，"影子账户"由 `paper_engine.step`
产出 —— 两个独立实现，把它们喂同一批 entry、同一批冻结输入，逐日比净值，才是"对齐"。

**口径必须声明，不能靠默认值**：本模块只从冻结的父 manifest 读参数（§4.1「缺失不得靠代码
默认补成可交易实验」），并把每一处与历史研究基线的**有意差异**逐条记进 `checks`。
"""
from __future__ import annotations

import pandas as pd

from scripts.medium_term.p2_selection_check import (build_entries, build_matrix,
                                                   market_frame, trading_calendar)
from scripts.medium_term.stock_cross_section import generate_monthly_candidates
from scripts.medium_term.timed_entries import build_timed_entries
from scripts.portfolio_shadow.verify_parity import verify_matrix_parity

# §15 P0 的验收是"一致"，不是"在容差内一致"。实测两引擎在 H60 单元上**逐日完全相等**
# （2631 个会话、0 分歧，tolerance 取 0.0）。故这里不给容差：任何非零分歧都应当是
# 一个待解释的发现，而不是被一个事先挑好的带宽吸收掉。
TOLERANCE_USD = 0.0


def evaluate(data, root, prices, quality, actions, trades, *, risk_policy, horizon,
             initial_cash_micro):
    """P0 的「基线对齐」一步到位：造批处理矩阵 → 跨引擎对账 → 条目对账。

    **不静默降级**：批处理管线跑不出条目时（夹具数据太短等）不是"跳过"，而是
    `status='NOT_EVALUATED'`，并由调用方把它翻成 `phase_conclusion='ENGINEERING_BLOCKED'`
    —— 一份没能证明"与旧基线一致"的 study 不构成 P0 基线（§15「失败则停在工程修复」）。
    """
    try:
        cell, info = batch_b3_matrix(data, root, prices, quality, actions,
                                     top_n=int(risk_policy.get('top_n', 5)), horizon=horizon)
    except ValueError as exc:
        return ({'status': 'NOT_EVALUATED', 'reason': str(exc),
                 'note': '批处理管线未产出可对账的条目 ⇒ 本 study 不能作为 P0 基线。'},
                {'status': 'NOT_EVALUATED'})
    record = compare(cell, prices, actions, horizon=horizon, risk_policy=risk_policy,
                     initial_cash_micro=initial_cash_micro)
    record.update(info)
    record['status'] = 'VERIFIED'
    entries = reconcile_entries(
        {(t['security_id'], str(pd.Timestamp(t['entry_session']).date())) for t in trades},
        cell)
    return record, entries


def batch_b3_matrix(data, root, prices, quality, actions, *, top_n, horizon):
    """用**本 study 的冻结输入**跑一遍批处理管线，产出 B3 矩阵（历史研究口径的候选）。

    与 `risk_rule_experiment._build_p2_matrices` 是同一套函数，但输入是 study 的冻结快照、
    参数取自冻结的父 manifest —— 所以它是"同输入"，不是"照抄一份别人的配置"。
    `blocked={}` 与 study 的增量生成器一致（行动覆盖门在本 study 里未启用）。
    """
    view = prices[['security_id', 'session', 'raw_open', 'raw_high', 'raw_low',
                   'raw_close', 'volume']].rename(columns={
        'raw_open': 'open', 'raw_high': 'high', 'raw_low': 'low', 'raw_close': 'close'})
    etf_path = root / data['input_index']['market'][0]['path']
    candidates = generate_monthly_candidates(view, market_frame(etf_path), top_n=top_n,
                                             actions=actions)
    selected = candidates[candidates.selected.astype(bool)].copy()
    timed = build_timed_entries(
        selected[['security_id', 'decision_session', 'execution_session', 'rank']],
        prices[['security_id', 'session', 'raw_open', 'raw_high', 'raw_low',
                'raw_close', 'volume']],
        trading_calendar(etf_path), actions=actions)
    spec = timed[timed.entry_type.ne('EXPIRED') & timed.execution_session.notna()]
    prepared, funnel = build_entries(spec, prices, quality, {},
                                     atr_session_col='signal_session')
    if prepared.empty:
        raise ValueError('BASELINE_PARITY_NO_PREPARED_ENTRIES')
    matrix = build_matrix(prepared, prices, actions)
    cell = matrix[matrix.holding_sessions.eq(horizon)].copy()
    if cell.empty:
        raise ValueError(f'BASELINE_PARITY_NO_CELL:H{horizon}')
    return cell, {'candidates': int(len(candidates)), 'selected': int(len(selected)),
                  'prepared': int(len(prepared)), 'funnel': funnel,
                  'cell_rows': int(len(cell)),
                  'cell_accepted': int(cell.portfolio_accepted.sum()),
                  'exit_reason': {str(k): int(v) for k, v in
                                  cell.exit_reason.value_counts().items()}}


def compare(matrix, prices, actions, *, horizon, risk_policy, initial_cash_micro):
    """逐日对账，返回可直接进 `checks` 的记录。非零分歧即抛（§15 P0 是"一致"）。

    `initial_cash_micro` —— **单位写进参数名**，因为这里踩过一次：`Manifest.initial_cash`
    存的是**整数微美元**，而 `simulate_multi_asset_portfolio` 收的是**美元**。两者混用会让
    试算账户以 1000 亿美元起步、而影子账户是 10 万 ⇒ 每个会话都对不上（实测
    `2631/2631`，最大差 $4.58e11）。命名带单位比注释可靠。
    """
    result = verify_matrix_parity(
        matrix, prices, actions, horizon=horizon,
        risk_bp=int(risk_policy['single_position_risk_bp']),
        initial_cash=float(initial_cash_micro) / 1_000_000, tol=TOLERANCE_USD)
    diffs = result['diffs']
    record = {
        'method': 'cross_engine_daily_equity',
        'hist_engine': 'medium_term.simulate_multi_asset_portfolio'
                       '(t1_settlement, dividend_receivable, shadow_precision)',
        'shadow_engine': 'portfolio_shadow.paper_engine.step',
        'n_sessions': int(result['n_sessions']),
        'n_diffs': int(result['n_diffs']),
        'tolerance_usd': TOLERANCE_USD,
        'entries': 'batch_b3_matrix_from_frozen_inputs',
        # 回撤阶梯在历史引擎里**不存在**（`grep ladder|high_water portfolio_engine.py` 为空），
        # 故对账只能在"阶梯关闭"下进行。它对本 study 的实际影响见 `uncovered_by_parity`。
        'drawdown_ladder': 'disabled_on_the_trial_account',
    }
    if diffs:
        worst = max(diffs, key=lambda d: abs(d['diff']))
        raise ValueError(f'BASELINE_PARITY_DIVERGED:{result["n_diffs"]}/{result["n_sessions"]}:'
                         f'{worst["session"]}:{worst["diff"]:.6f}USD')
    return record


def reconcile_entries(study_executions, matrix):
    """study 的增量条目 vs 批处理矩阵条目：两套接受机制的差集要**看得见**。

    §5.1 明令「不得把账户容量拒绝伪装为策略入场拒绝」，两份清单本就该不同：
    批处理按 rank 做静态五仓限制，影子引擎做运行时拒绝（DUPLICATE/MAX_POSITIONS）。
    把它们当成"不一致"是误读，静默不报也不对 —— 读的人需要知道对账覆盖的是哪一批 entry。
    """
    batch = {(str(r.security_id), str(pd.Timestamp(r.entry_session).date()))
             for r in matrix[matrix.portfolio_accepted.astype(bool)].itertuples()}
    study = set(study_executions)
    return {'batch_accepted': len(batch), 'study_executed': len(study),
            'in_both': len(batch & study),
            'only_batch': sorted(batch - study)[:20], 'n_only_batch': len(batch - study),
            'only_study': sorted(study - batch)[:20], 'n_only_study': len(study - batch),
            'note': '两者接受机制不同（批处理静态五仓 vs 引擎运行时拒绝），差异属预期；'
                    '对账覆盖的是批处理那一批 entry，不是 study 自己成交的那一批。'}
