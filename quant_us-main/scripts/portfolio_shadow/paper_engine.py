"""R/L 双影子账户增量 EOD 模拟引擎（PR3 核心）。

纯逻辑、无网络、确定性（整数微美元运算）。逐日顺序（设计 §6.2）：
公司行动（拆股/除息记应收）→ 资金结算（T+1 卖出款 + 支付日分红）→ 已有持仓开盘
风险退出（跳空止损）→ 候选按序开盘入场（风险定仓 + 五仓）→ 日内硬止损 → 时间退出
（收盘）→ 收盘估值与高水位。

只消费当日已知信息（bars/公司行动/intents），不读未来 exit_matrix；重放一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .risk_policy import budget_bp, entry_allowed, evaluate_ladder
from .schema import AccountState, Position


def initial_stop_micro(entry_price_micro: int, atr14_micro: int) -> int:
    """止损距离 = max(8% × 价格, 2.5 × ATR)；stop = 价格 − 距离。"""
    min_dist = entry_price_micro * 8 // 100
    atr_dist = atr14_micro * 5 // 2
    return entry_price_micro - max(min_dist, atr_dist)


def risk_sized_shares_micro(entry_price_micro: int, stop_micro: int, nav_micro: int,
                            cash_micro: int, *, risk_bp: int, max_weight_bp: int,
                            fee_bp: int) -> int:
    """定仓：min(单笔风险预算 / 止损距离, 单票市值上限, 现金约束)，向下取整。"""
    distance = entry_price_micro - stop_micro
    if distance <= 0 or nav_micro <= 0 or cash_micro < 0:
        return 0
    risk_shares = nav_micro * risk_bp // 10000 // distance
    weight_shares = nav_micro * max_weight_bp // 10000 // entry_price_micro
    cash_shares = cash_micro * 10000 // (entry_price_micro * (10000 + fee_bp))
    return min(risk_shares, weight_shares, cash_shares)


@dataclass
class StepResult:
    state: AccountState
    events: list = field(default_factory=list)  # 已落账事件（fill/split/dividend/nav）
    nav: dict | None = None  # None 表示该 session 已处理（幂等 no-op）


def _fill(session, side, security_id, shares, price_micro, fee, reason, opportunity_id=''):
    return {'type': 'fill', 'session': session, 'side': side, 'security_id': security_id,
            'shares': shares, 'price_micro': price_micro, 'fee_micro': fee, 'reason': reason,
            'opportunity_id': opportunity_id}


def _fee(gross_micro: int, fee_bp: int) -> int:
    return gross_micro * fee_bp // 10000


def step(state: AccountState, *, session: str, bars: dict, corporate_actions: list,
         intents: list, manifest, fee_bp: int = 10, model_cost: int = 0) -> StepResult:
    """执行一个交易日。bars={sid:{open,high,low,close}}（微美元/股）；公司行动用微美元。"""
    if state.last_session is not None and session < state.last_session:
        # 乱序：旧 session 必须拒绝，不得倒退推进账户
        raise ValueError(f'OUT_OF_ORDER_SESSION:{session}<{state.last_session}')
    if state.last_session == session:
        # 幂等：该 session 已处理，不推进 sequence/持有天数/结算
        return StepResult(state=state, events=[], nav=None)
    s = replace(state)
    s.sequence = state.sequence + 1
    s.last_session = session
    events: list = []
    if model_cost:
        s.model_cost += model_cost
        events.append({'type': 'model_cost', 'session': session, 'amount_micro': model_cost})
    horizon = int(manifest.execution_policy.get('horizon', 60))
    base_bp = int(manifest.risk_policy['single_position_risk_bp'])
    risk_bp = budget_bp(s.risk_state, base_bp)
    allowed = entry_allowed(s.risk_state)
    max_weight_bp = int(manifest.risk_policy['max_weight_bp'])
    max_positions = int(manifest.risk_policy['max_positions'])

    # 0. 资金结算：T+1 卖出款 + 支付日分红 → 可用现金
    if s.unsettled_cash:
        events.append({'type': 'settle', 'session': session, 'amount_micro': s.unsettled_cash})
        s.cash_available += s.unsettled_cash
        s.unsettled_cash = 0
    payable = s.dividend_receivable.pop(session, 0)
    if payable:
        s.cash_available += payable
        events.append({'type': 'dividend_pay', 'session': session, 'pay_date': session,
                       'total_micro': payable})

    # 1. 公司行动：拆股调整数量/成本/止损；除息日对前一日持仓记应收
    for act in corporate_actions:
        if act.get('ex_date') != session:
            continue
        sid = act['security_id']
        pos = s.positions.get(sid)
        if pos is None:
            continue
        kind = act.get('action_type')
        if kind in ('split', 'reverse_split'):
            ratio = int(act['ratio'])
            if kind == 'split':
                s.positions[sid] = replace(pos, shares=pos.shares * ratio,
                                           entry_price_micro=pos.entry_price_micro // ratio,
                                           initial_stop_micro=pos.initial_stop_micro // ratio,
                                           stop_micro=pos.stop_micro // ratio)
            else:
                s.positions[sid] = replace(pos, shares=pos.shares // ratio,
                                           entry_price_micro=pos.entry_price_micro * ratio,
                                           initial_stop_micro=pos.initial_stop_micro * ratio,
                                           stop_micro=pos.stop_micro * ratio)
            events.append({'type': 'split', 'session': session, 'security_id': sid,
                           'ratio': ratio, 'kind': kind})
        elif kind == 'cash_dividend':
            per_share = int(act['cash_amount_micro'])
            total = pos.shares * per_share
            pay_date = act.get('pay_date')
            s.dividend_receivable[pay_date] = s.dividend_receivable.get(pay_date, 0) + total
            events.append({'type': 'dividend_record', 'session': session, 'security_id': sid,
                           'per_share_micro': per_share, 'total_micro': total,
                           'pay_date': pay_date})

    # 2. 开盘风险退出（跳空止损：open <= stop）
    for sid, pos in list(s.positions.items()):
        if sid not in bars:
            continue  # 缺行情：PROVISIONAL，不强行退出
        o = bars[sid]['open']
        if o <= pos.stop_micro:
            gross = pos.shares * o
            fee = _fee(gross, fee_bp)
            s.unsettled_cash += gross - fee
            s.fees += fee
            del s.positions[sid]
            events.append(_fill(session, 'SELL', sid, pos.shares, o, fee, 'GAP_STOP',
                                pos.opportunity_id))

    # 3. 开盘入场（风险定仓 + 五仓 + 去重）
    missing_held = [sid for sid in s.positions if sid not in bars]
    valuation_ok = not missing_held
    for intent in sorted(intents, key=lambda o: (o.rank, o.security_id)):
        sid = intent.security_id
        if not allowed:
            events.append({'type': 'missed', 'session': session, 'security_id': sid,
                           'opportunity_id': intent.opportunity_id(), 'reason': 'RISK_PAUSED'})
            continue
        if not valuation_ok:
            events.append({'type': 'missed', 'session': session, 'security_id': sid,
                           'opportunity_id': intent.opportunity_id(), 'reason': 'VALUATION_INCOMPLETE'})
            continue
        if sid not in bars:
            events.append({'type': 'missed', 'session': session, 'security_id': sid,
                           'opportunity_id': intent.opportunity_id(), 'reason': 'DATA_BLOCKED'})
            continue
        if sid in s.positions:
            events.append({'type': 'missed', 'session': session, 'security_id': sid,
                           'opportunity_id': intent.opportunity_id(),
                           'reason': 'DUPLICATE_ACTIVE_SECURITY'})
            continue
        if len(s.positions) >= max_positions:
            events.append({'type': 'missed', 'session': session, 'security_id': sid,
                           'opportunity_id': intent.opportunity_id(), 'reason': 'MAX_POSITIONS'})
            continue
        open_micro = bars[sid]['open']
        stop = initial_stop_micro(open_micro, int(intent.stop_reference['atr14_micro']))
        if stop <= 0:
            events.append({'type': 'missed', 'session': session, 'security_id': sid,
                           'opportunity_id': intent.opportunity_id(),
                           'reason': 'INVALID_INITIAL_STOP'})
            continue
        nav_micro = (s.cash_available + s.unsettled_cash + sum(s.dividend_receivable.values()) +
                     sum(p.shares * bars[p.security_id]['open'] for p in s.positions.values()
                         if p.security_id in bars))
        shares = risk_sized_shares_micro(open_micro, stop, nav_micro, s.cash_available,
                                         risk_bp=risk_bp, max_weight_bp=max_weight_bp,
                                         fee_bp=fee_bp)
        if shares <= 0:
            events.append({'type': 'missed', 'session': session, 'security_id': sid,
                           'opportunity_id': intent.opportunity_id(), 'reason': 'POSITION_SIZE_ZERO'})
            continue
        gross = shares * open_micro
        fee = _fee(gross, fee_bp)
        s.cash_available -= gross + fee
        s.fees += fee
        s.positions[sid] = Position(security_id=sid, shares=shares,
                                    entry_price_micro=open_micro, entry_session=session,
                                    initial_stop_micro=stop, stop_micro=stop,
                                    exit_policy_id=intent.exit_policy_id,
                                    opportunity_id=intent.opportunity_id())
        events.append({**_fill(session, 'BUY', sid, shares, open_micro, fee, 'ENTRY',
                                intent.opportunity_id()),
                       'stop_micro': stop, 'exit_policy_id': intent.exit_policy_id})

    # 4. 日内硬止损（low <= stop → 按止损价卖出）
    for sid, pos in list(s.positions.items()):
        if sid not in bars:
            continue
        if bars[sid]['low'] <= pos.stop_micro:
            px = pos.stop_micro
            gross = pos.shares * px
            fee = _fee(gross, fee_bp)
            s.unsettled_cash += gross - fee
            s.fees += fee
            del s.positions[sid]
            events.append(_fill(session, 'SELL', sid, pos.shares, px, fee, 'STOP',
                                pos.opportunity_id))

    # 5. 时间退出（holding >= horizon → 收盘卖出）
    for sid, pos in list(s.positions.items()):
        pos = replace(pos, holding_sessions=pos.holding_sessions + 1)
        s.positions[sid] = pos
        events.append({'type': 'hold', 'session': session, 'security_id': sid,
                       'holding_sessions': pos.holding_sessions})
        if pos.holding_sessions >= horizon and sid in bars:
            px = bars[sid]['close']
            gross = pos.shares * px
            fee = _fee(gross, fee_bp)
            s.unsettled_cash += gross - fee
            s.fees += fee
            del s.positions[sid]
            events.append(_fill(session, 'SELL', sid, pos.shares, px, fee, 'TIME_EXIT',
                                pos.opportunity_id))

    # 6. 收盘估值 + 高水位 + 回撤阶梯
    mv = sum(p.shares * bars[p.security_id]['close'] for p in s.positions.values()
             if p.security_id in bars)
    missing = [sid for sid in s.positions if sid not in bars]
    equity = s.cash_available + s.unsettled_cash + sum(s.dividend_receivable.values()) + mv
    s.valuation_status = 'PROVISIONAL' if missing else 'OK'
    full_cost_equity = equity - s.model_cost
    if s.valuation_status == 'OK':
        s.high_water = max(s.high_water, full_cost_equity)
        drawdown = (1.0 - full_cost_equity / s.high_water) if s.high_water > 0 else 0.0
        s.risk_state, s.recovery_streak = evaluate_ladder(
            drawdown, s.risk_state, s.recovery_streak, manifest.risk_policy)
    nav = {'session': session, 'equity': equity, 'cash_available': s.cash_available,
           'gross_exposure': mv, 'fees': s.fees, 'valuation_status': s.valuation_status,
           'model_cost': s.model_cost, 'full_cost_equity': full_cost_equity,
           'risk_state': s.risk_state, 'recovery_streak': s.recovery_streak,
           'revision': 1}
    events.append({'type': 'nav', **nav})
    return StepResult(state=s, events=events, nav=nav)


def new_account_state(scope: str, initial_cash: int) -> AccountState:
    return AccountState(scope=scope, cash_available=initial_cash,
                        initial_equity=initial_cash, high_water=initial_cash)
