"""历史引擎 vs 影子引擎逐日奇偶校验（第1条）。

把同一批冻结 entry 分别喂给历史引擎（`simulate_multi_asset_portfolio`，影子会计）与影子引擎
（`paper_engine.step`，增量、回撤阶梯关闭），逐日对齐 equity 并返回分歧清单。金额在历史侧为
float 美元、影子侧为 int 微美元，diff 时统一到美元。
"""
from __future__ import annotations

import pandas as pd

from scripts.medium_term.entry_risk import medium_initial_stop
from scripts.medium_term.portfolio_engine import simulate_multi_asset_portfolio
from .paper_engine import new_account_state, step
from .schema import Manifest, Opportunity, to_micro


def _hist_inputs(sessions, bars, entries, horizon):
    """构造历史引擎的 prices / matrix（entry 的 exit 用固定持有期时间退出，无止损触发）。"""
    prices_rows = []
    for session in sessions:
        for sid, b in bars[session].items():
            prices_rows.append({'security_id': sid, 'session': session,
                                'raw_open': b['open'], 'raw_high': b['high'],
                                'raw_low': b['low'], 'raw_close': b['close']})
    prices = pd.DataFrame(prices_rows)
    prices['session'] = pd.to_datetime(prices.session).dt.normalize()
    matrix_rows = []
    for i, e in enumerate(entries):
        sid, entry_sess = e['security_id'], pd.Timestamp(e['entry_session']).normalize()
        stock = prices[prices.security_id.eq(sid)].sort_values('session').reset_index(drop=True)
        entry_idx = int(stock.index[stock.session.eq(entry_sess)][0])
        entry_price = float(stock.raw_open.iloc[entry_idx])
        stop = medium_initial_stop(entry_price, e['atr14'])
        exit_idx = entry_idx + horizon - 1  # 持有第 horizon 个 session 收盘退出
        matrix_rows.append({'security_id': sid, 'entry_session': entry_sess,
                            'entry_price': entry_price, 'initial_stop': stop,
                            'exit_session': stock.session.iloc[exit_idx],
                            'exit_price': float(stock.raw_close.iloc[exit_idx]),
                            'exit_phase': 'CLOSE', 'exit_reason': 'TIME_EXIT',
                            'portfolio_accepted': True, 'rank': i})
    return prices, pd.DataFrame(matrix_rows)


def _shadow_manifest(risk_bp, horizon):
    return Manifest(
        experiment_id='parity', status='DRAFT', parent_strategy_id='B3', parent_version='1',
        parent_code_hash='abc', universe_id='u', universe_hash='uh',
        account_scopes=('SHADOW:parity:R', 'SHADOW:parity:L'), initial_cash=to_micro(100_000),
        risk_policy={'single_position_risk_bp': risk_bp, 'max_weight_bp': 2000,
                     'max_positions': 5,
                     'drawdown_ladder': {'limit_breach': 1.0, 'review_required': 1.0,
                                         'paused_entry': 1.0, 'reduced': 1.0}},
        execution_policy={'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': horizon},
        llm_policy={'overlay': 'fixed_pass'}, calendar_version='v1',
        evaluation_protocol={'main_metric': 'L_minus_R_return', 'enrollment_window': '3-6 months',
                             'review_date': '2026-12-31', 'cost_allocation': 'L_pays_model_cost'}
    ).freeze('2026-01-02')


def verify_parity(sessions, bars, entries, corporate_actions, *, horizon=2, risk_bp=100,
                  initial_cash=100_000.0, tol=0.02) -> dict:
    """两引擎逐日 diff，返回 {'diffs': [...], 'hist': [...], 'shadow': [...]}。"""
    prices, matrix = _hist_inputs(sessions, bars, entries, horizon)
    # 历史引擎（影子会计）
    hist = simulate_multi_asset_portfolio(
        prices, matrix, initial_cash=initial_cash, risk_fraction=risk_bp / 10000,
        max_weight=.20, round_trip_cost=.002, actions=_hist_actions(corporate_actions),
        allow_fractional=False, max_positions=5,
        t1_settlement=True, dividend_receivable=True)
    hist_navs = {str(r.session.date()): r.equity for r in hist.equity.itertuples()}

    # 影子引擎（增量，回撤阶梯关闭）
    manifest = _shadow_manifest(risk_bp, horizon)
    state = new_account_state('SHADOW:parity:R', manifest.initial_cash)
    shadow_navs = {}
    for session in sessions:
        intents = [Opportunity(
            experiment_id='parity', security_id=e['security_id'],
            source_candidate_id=e['security_id'], parent_version='1',
            signal_session=session, observed_at=f'{session}T00:00:00+00:00',
            planned_execution_session=session, rank=0, entry_rule='b3',
            stop_reference={'atr14_micro': to_micro(e['atr14'])}, exit_policy_id='H60',
            input_hash='h', terminal='READY')
            for e in entries if e['entry_session'] == session]
        res = step(state, session=session,
                   bars={sid: {k: to_micro(v) for k, v in b.items()}
                         for sid, b in bars[session].items()},
                   corporate_actions=[a for a in corporate_actions
                                      if a.get('ex_date') == session or a.get('pay_date') == session],
                   intents=intents, manifest=manifest)
        state = res.state
        shadow_navs[session] = res.nav['equity'] / 1e6 if res.nav else None

    diffs = []
    for session in sessions:
        h = hist_navs.get(session)
        s = shadow_navs.get(session)
        if h is not None and s is not None and abs(h - s) > tol:
            diffs.append({'session': session, 'hist_equity': h, 'shadow_equity': s,
                          'diff': h - s})
    return {'diffs': diffs, 'hist': hist_navs, 'shadow': shadow_navs}


def _hist_actions(corporate_actions):
    """影子格式（cash_amount_micro）→ 历史格式（cash_amount 美元 + ex_date + pay_date）。"""
    rows = []
    for a in corporate_actions:
        rows.append({'security_id': a['security_id'], 'ex_date': a['ex_date'],
                     'action_type': a['action_type'],
                     'ratio': a.get('ratio'),
                     'cash_amount': (a.get('cash_amount_micro') or 0) / 1e6,
                     'pay_date': a.get('pay_date')})
    return pd.DataFrame(rows)


def _shadow_actions(actions):
    """历史 actions DataFrame（cash_amount 美元）→ 影子 actions list。

    **必须带上 `pay_date`**：影子引擎拿它当 `dividend_receivable` 的字典键，丢了它分红就
    永远转不成现金 —— 对拍会"通过"，但通过的原因是两边都算错了同一件事。
    """
    out = []
    for r in actions.itertuples():
        d = {'security_id': str(r.security_id),
             'ex_date': str(pd.Timestamp(r.ex_date).date()),
             'action_type': str(r.action_type).lower()}
        if getattr(r, 'ratio', None) is not None:
            d['ratio'] = r.ratio
        if getattr(r, 'cash_amount', None) is not None:
            d['cash_amount_micro'] = to_micro(r.cash_amount)
        pay = getattr(r, 'pay_date', None)
        d['pay_date'] = None if pay is None or pd.isna(pay) else str(pd.Timestamp(pay).date())
        out.append(d)
    return out


def _with_pay_date(actions):
    """给行动表补统一的 `pay_date` 列（缺则取 `effective_at`）。

    富途直取的行动表把派发日写在 `effective_at` 里（`ACTION_COLUMNS` 没有 `pay_date`），
    而两个引擎都按 `pay_date` 做"应收 → 现金"的结算键：历史侧读 `event['pay_date']`，
    影子侧读 `act['pay_date']`。不统一这一列，两边都把分红**永远挂在应收里**，
    等于对拍**根本没有检验过支付日那条路径**（实测 study 里有 63 条记应收 / 60 条支付）。
    """
    if actions is None or actions.empty:
        return actions
    if 'pay_date' in actions.columns:
        return actions
    out = actions.copy()
    out['pay_date'] = out['effective_at'] if 'effective_at' in out.columns else None
    return out


def verify_matrix_parity(matrix, prices, actions, *, horizon, risk_bp,
                         initial_cash=100_000.0, tol=0.02, scope='SHADOW:parity:R') -> dict:
    """真实矩阵奇偶校验：同一 matrix（entry/exit）+ prices + actions 喂两引擎，逐日 diff。"""
    actions = _with_pay_date(actions)
    matrix = matrix[matrix.portfolio_accepted.astype(bool)].copy()
    matrix['entry_session'] = pd.to_datetime(matrix.entry_session).dt.normalize()
    prices = prices.copy()
    prices['session'] = pd.to_datetime(prices.session).dt.normalize()
    prices['security_id'] = prices.security_id.astype(str)

    # 1. 历史引擎（影子会计 + 整数微定仓/费用）
    hist = simulate_multi_asset_portfolio(
        prices, matrix, initial_cash=initial_cash, risk_fraction=risk_bp / 10000,
        max_weight=.20, round_trip_cost=.002, actions=actions,
        allow_fractional=False, max_positions=5,
        t1_settlement=True, dividend_receivable=True, shadow_precision=True)
    hist_navs = {str(r.session.date()): r.equity for r in hist.equity.itertuples()}

    # 2. 影子引擎（增量，回撤阶梯关闭）
    manifest = _shadow_manifest(risk_bp, horizon)
    state = new_account_state(scope, manifest.initial_cash)
    shadow_actions = _shadow_actions(actions)
    sessions = sorted(prices.session.unique())
    shadow_navs = {}
    for session in sessions:
        sess_str = str(pd.Timestamp(session).date())
        bars = {sid: {'open': to_micro(r.raw_open), 'high': to_micro(r.raw_high),
                      'low': to_micro(r.raw_low), 'close': to_micro(r.raw_close)}
                for sid, r in prices[prices.session.eq(session)].set_index('security_id').iterrows()}
        intents = [Opportunity(
            experiment_id='parity', security_id=str(r.security_id),
            source_candidate_id=str(getattr(r, 'entry_id', '')), parent_version='1',
            signal_session=sess_str, observed_at=f'{sess_str}T00:00:00+00:00',
            planned_execution_session=sess_str, rank=int(getattr(r, 'rank', 0)),
            entry_rule='b3', stop_reference={'initial_stop_micro': to_micro(r.initial_stop)},
            exit_policy_id='H60', input_hash='h', terminal='READY')
            for r in matrix[matrix.entry_session.eq(session)].itertuples()]
        acts = [a for a in shadow_actions if a.get('ex_date') == sess_str]
        res = step(state, session=sess_str, bars=bars, corporate_actions=acts,
                   intents=intents, manifest=manifest)
        state = res.state
        shadow_navs[sess_str] = res.nav['equity'] / 1e6 if res.nav else None

    # 3. diff
    diffs = []
    for sess_str, h in hist_navs.items():
        s = shadow_navs.get(sess_str)
        if s is not None and abs(h - s) > tol:
            diffs.append({'session': sess_str, 'hist_equity': h, 'shadow_equity': s,
                          'diff': h - s})
    return {'diffs': diffs, 'n_sessions': len(hist_navs), 'n_diffs': len(diffs)}
