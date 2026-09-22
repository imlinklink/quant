"""重放：只消费已记录事件重建账户状态，与落库状态哈希对比（不查行情、不调模型）。"""
from __future__ import annotations

from dataclasses import replace

from .paper_engine import new_account_state
from .schema import AccountState, Position


def apply_event(state: AccountState, event: dict) -> AccountState:
    """把单个事件应用到账户状态（与 paper_engine.step 内联逻辑一致，测试兜底防漂移）。"""
    s = replace(state)
    t = event['type']
    if t == 'settle':
        s.cash_available += event['amount_micro']
        s.unsettled_cash -= event['amount_micro']
    elif t == 'model_cost':
        s.model_cost += event['amount_micro']
        if event.get('uncertain'):
            aid = event['attempt_id']
            if aid not in s.model_cost_unsettled:
                s.model_cost_unsettled = tuple(sorted((*s.model_cost_unsettled, aid)))
    elif t == 'model_cost_settlement':
        aid = event['attempt_id']
        if aid in s.model_cost_unsettled:
            s.model_cost_unsettled = tuple(x for x in s.model_cost_unsettled if x != aid)
            s.model_cost += event['amount_micro']
    elif t == 'hold':
        sid = event['security_id']
        s.positions[sid] = replace(s.positions[sid], holding_sessions=event['holding_sessions'])
    elif t == 'protection_state':
        # 每持仓每 session 无条件写：H 在激活前也每 session 在变，重放不查行情，
        # 只看「有事发生」的事件是重建不出 H 的。
        sid = event['security_id']
        s.positions[sid] = replace(s.positions[sid],
                                   highest_completed_close_micro=event['high_close_micro'],
                                   protection_activated=event['protection_activated'],
                                   pending_stop_micro=event.get('pending_stop_micro'),
                                   pending_stop_effective_session=event.get(
                                       'pending_stop_effective_session', ''))
    elif t == 'stop_update_applied':
        sid = event['security_id']
        s.positions[sid] = replace(s.positions[sid], stop_micro=event['stop_micro'],
                                   pending_stop_micro=None,
                                   pending_stop_effective_session='')
    elif t == 'dividend_pay':
        s.cash_available += event['total_micro']
        pay_date = event['pay_date']
        s.dividend_receivable[pay_date] = s.dividend_receivable.get(pay_date, 0) - event['total_micro']
        if s.dividend_receivable[pay_date] == 0:
            del s.dividend_receivable[pay_date]
    elif t == 'dividend_record':
        pay_date = event['pay_date']
        s.dividend_receivable[pay_date] = s.dividend_receivable.get(pay_date, 0) + event['total_micro']
        # 除息日止损随分红下调（与 paper_engine.step 一致）；H 与待生效保护线同口径下调，
        # 否则保护线会按除息前的价格水平停留（除息当天把正常持仓判成触发）。
        sid = event['security_id']
        pos = s.positions[sid]
        per_share = event['per_share_micro']
        s.positions[sid] = replace(pos, stop_micro=max(0, pos.stop_micro - per_share),
                                   highest_completed_close_micro=max(
                                       0, pos.highest_completed_close_micro - per_share),
                                   pending_stop_micro=(
                                       None if pos.pending_stop_micro is None
                                       else max(0, pos.pending_stop_micro - per_share)))
    elif t == 'split':
        sid = event['security_id']
        pos = s.positions[sid]
        ratio = int(event['ratio'])
        pend = (None if pos.pending_stop_micro is None
                else (pos.pending_stop_micro // ratio if event['kind'] == 'split'
                      else pos.pending_stop_micro * ratio))
        if event['kind'] == 'split':
            s.positions[sid] = replace(pos, shares=pos.shares * ratio,
                                       entry_price_micro=pos.entry_price_micro // ratio,
                                       initial_stop_micro=pos.initial_stop_micro // ratio,
                                       stop_micro=pos.stop_micro // ratio,
                                       highest_completed_close_micro=(
                                           pos.highest_completed_close_micro // ratio),
                                       pending_stop_micro=pend)
        else:
            s.positions[sid] = replace(pos, shares=pos.shares // ratio,
                                       entry_price_micro=pos.entry_price_micro * ratio,
                                       initial_stop_micro=pos.initial_stop_micro * ratio,
                                       stop_micro=pos.stop_micro * ratio,
                                       highest_completed_close_micro=(
                                           pos.highest_completed_close_micro * ratio),
                                       pending_stop_micro=pend)
    elif t == 'fill':
        sid = event['security_id']
        if event['side'] == 'BUY':
            gross = event['shares'] * event['price_micro']
            s.cash_available -= gross + event['fee_micro']
            s.fees += event['fee_micro']
            stop = event.get('stop_micro', 0)
            s.positions[sid] = Position(
                security_id=sid, shares=event['shares'],
                entry_price_micro=event['price_micro'], entry_session=event['session'],
                initial_stop_micro=stop,
                stop_micro=stop,
                exit_policy_id=event.get('exit_policy_id', ''),
                opportunity_id=event['opportunity_id'],
                # 与 paper_engine.step 的入场完全同式（无需新事件字段）
                initial_risk_micro=event['shares'] * (event['price_micro'] - stop))
        else:  # SELL
            gross = event['shares'] * event['price_micro']
            s.unsettled_cash += gross - event['fee_micro']
            s.fees += event['fee_micro']
            pos = s.positions.get(sid)
            if pos is not None and event['shares'] < pos.shares:
                # 部分卖出（持仓评审减仓）：保留剩余持仓，与 paper_engine.step 一致。
                # 全量卖出行为不变——仍是删除持仓。
                s.positions[sid] = replace(pos, shares=pos.shares - event['shares'])
            else:
                s.positions.pop(sid, None)
    return s


def replay(scope: str, initial_cash: int, events: list[dict]) -> AccountState:
    """按事件顺序重放，返回重建状态（与落库状态的 state_hash 对比）。"""
    state = new_account_state(scope, initial_cash)
    for event in events:
        if event.get('type') in ('settle', 'dividend_pay', 'dividend_record', 'split', 'fill',
                                 'model_cost', 'model_cost_settlement', 'hold',
                                 'protection_state', 'stop_update_applied'):
            state = apply_event(state, event)
        elif event.get('type') == 'nav':
            kwargs = {'sequence': state.sequence + 1, 'last_session': event['session'],
                      'valuation_status': event['valuation_status'],
                      'risk_state': event.get('risk_state', 'NORMAL'),
                      'recovery_streak': event.get('recovery_streak', 0)}
            if event['valuation_status'] == 'OK':
                kwargs['high_water'] = max(state.high_water, event['full_cost_equity'])
            state = replace(state, **kwargs)
    return state
