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
    elif t == 'hold':
        sid = event['security_id']
        s.positions[sid] = replace(s.positions[sid], holding_sessions=event['holding_sessions'])
    elif t == 'dividend_pay':
        s.cash_available += event['total_micro']
        pay_date = event['pay_date']
        s.dividend_receivable[pay_date] = s.dividend_receivable.get(pay_date, 0) - event['total_micro']
        if s.dividend_receivable[pay_date] == 0:
            del s.dividend_receivable[pay_date]
    elif t == 'dividend_record':
        pay_date = event['pay_date']
        s.dividend_receivable[pay_date] = s.dividend_receivable.get(pay_date, 0) + event['total_micro']
    elif t == 'split':
        sid = event['security_id']
        pos = s.positions[sid]
        ratio = int(event['ratio'])
        if event['kind'] == 'split':
            s.positions[sid] = replace(pos, shares=pos.shares * ratio,
                                       entry_price_micro=pos.entry_price_micro // ratio,
                                       initial_stop_micro=pos.initial_stop_micro // ratio,
                                       stop_micro=pos.stop_micro // ratio)
        else:
            s.positions[sid] = replace(pos, shares=pos.shares // ratio,
                                       entry_price_micro=pos.entry_price_micro * ratio,
                                       initial_stop_micro=pos.initial_stop_micro * ratio,
                                       stop_micro=pos.stop_micro * ratio)
    elif t == 'fill':
        sid = event['security_id']
        if event['side'] == 'BUY':
            gross = event['shares'] * event['price_micro']
            s.cash_available -= gross + event['fee_micro']
            s.fees += event['fee_micro']
            s.positions[sid] = Position(
                security_id=sid, shares=event['shares'],
                entry_price_micro=event['price_micro'], entry_session=event['session'],
                initial_stop_micro=event.get('stop_micro', 0),
                stop_micro=event.get('stop_micro', 0),
                exit_policy_id=event.get('exit_policy_id', ''),
                opportunity_id=event['opportunity_id'])
        else:  # SELL
            gross = event['shares'] * event['price_micro']
            s.unsettled_cash += gross - event['fee_micro']
            s.fees += event['fee_micro']
            s.positions.pop(sid, None)
    return s


def replay(scope: str, initial_cash: int, events: list[dict]) -> AccountState:
    """按事件顺序重放，返回重建状态（不含 holding_sessions，state_hash 已排除它）。"""
    state = new_account_state(scope, initial_cash)
    for event in events:
        if event.get('type') in ('settle', 'dividend_pay', 'dividend_record', 'split', 'fill',
                                 'model_cost', 'hold'):
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
