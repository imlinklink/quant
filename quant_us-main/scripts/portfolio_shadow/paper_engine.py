"""R/L 双影子账户增量 EOD 模拟引擎（PR3 核心）。

纯逻辑、无网络、确定性（整数微美元运算）。逐日顺序（与代码一致，设计 §6.2）：
资金结算（T+1 卖出款 + 支付日分红）→ 公司行动（拆股/除息记应收）→ 已有持仓开盘
风险退出（跳空止损）→ 持仓评审动作（仅 L 账户）→ 候选按序开盘入场（风险定仓 +
五仓）→ 日内硬止损 → 时间退出（收盘）→ 收盘估值与高水位。

只消费当日已知信息（bars/公司行动/intents），不读未来 exit_matrix；重放一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from scripts.medium_term.entry_risk import fee_micro, risk_sized_shares_micro

from .risk_policy import budget_bp, entry_allowed, evaluate_ladder
from .schema import AccountState, Position


#: `next_session` 的「本 session 之后没有会话了」哨兵（窗口末尾）。空串不是日期，
#: 因此与「忘了传」（None）区分得开 —— 后者会直接拒绝。
NO_NEXT_SESSION = ''


def initial_stop_micro(entry_price_micro: int, atr14_micro: int) -> int:
    """止损距离 = max(8% × 价格, 2.5 × ATR)；stop = 价格 − 距离。"""
    min_dist = entry_price_micro * 8 // 100
    atr_dist = atr14_micro * 5 // 2
    return entry_price_micro - max(min_dist, atr_dist)


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
    # 定义已移到 `medium_term.entry_risk.fee_micro`：历史引擎的 `shadow_precision`
    # 分支要用同一份。留这个别名是因为本模块内部 5 处调用点都叫 `_fee`。
    return fee_micro(gross_micro, fee_bp)


def step(state: AccountState, *, session: str, bars: dict, corporate_actions: list,
         intents: list, manifest, fee_bp: int = 10, model_cost: int = 0,
         model_cost_uncertain: tuple = (), model_cost_settlements: dict | None = None,
         position_actions: dict | None = None, protection=None, atr: dict | None = None,
         next_session: str | None = None) -> StepResult:
    """执行一个交易日。bars={sid:{open,high,low,close}}（微美元/股）；公司行动用微美元。

    position_actions 形如 {security_id: {'action': 'reduce'|'exit', 'tier': float,
    'decision_id': str}}，只传给 L 账户；R 传 None 时本阶段完全不生效。

    `protection` 是机械利润保护策略（`strategy_research.exit_policy.ProfitProtection`），
    `atr` 是**当日收盘可得**的 ATR14（{sid: 微美元}），`next_session` 是日历上的下一交易日
    （保护线 T 收盘算、T+1 生效，故必须显式给出，不能由本函数猜）。

    `next_session` 两种写法含义不同，缺一不可：
      · `None`（默认）= **调用方忘了传** ⇒ 直接拒绝，不静默退化成「只有激活、没有线更新」；
      · `NO_NEXT_SESSION`（空串）= **本 session 之后没有会话了**（窗口末尾）⇒ 照常执行
        当日的退出逻辑，只是不排期（排了也没人会执行）。

    **`protection=None`（默认）时本引擎逐事件、逐日行为与本参数引入前完全相同** —— 这是
    隔离版本不变性的前提（规划 §3.1），有测试钉死。
    """
    if protection is not None and next_session is None:
        raise ValueError('PROTECTION_REQUIRES_NEXT_SESSION')
    # 执行日守卫：整批校验必须先于任何状态变更，避免处理一半才报错；一次报全部违规。
    # 计划执行日 ≠ 当前 session 属调度/数据错误，绝不按历史开盘价补成交。
    violations = [(intent.opportunity_id(),
                   getattr(intent, 'planned_execution_session', None)) for intent in intents
                  if getattr(intent, 'planned_execution_session', None) != session]
    if violations:
        detail = ';'.join(f'{oid}:planned={planned}' for oid, planned in violations)
        raise ValueError(f'INTENT_SESSION_MISMATCH:{detail}:session={session}')
    if state.last_session is not None and session < state.last_session:
        # 乱序：旧 session 必须拒绝，不得倒退推进账户
        raise ValueError(f'OUT_OF_ORDER_SESSION:{session}<{state.last_session}')
    if state.last_session == session:
        # 幂等：该 session 已处理，不推进 sequence/持有天数/结算
        return StepResult(state=state, events=[], nav=None)
    s = replace(state, dividend_receivable=dict(state.dividend_receivable))
    s.sequence = state.sequence + 1
    s.last_session = session
    events: list = []
    # 成本记账：已知成本直接累计；不可知成本金额记 0 但必须挂账待补记，
    # 不能被真值判断跳过（0 表示「本次尚未计入费用」，不是「实际免费」）。
    if model_cost:
        s.model_cost += model_cost
        events.append({'type': 'model_cost', 'session': session, 'amount_micro': model_cost})
    for attempt_id in model_cost_uncertain:
        if attempt_id in s.model_cost_unsettled:
            continue  # 已挂账，幂等
        s.model_cost_unsettled = tuple(sorted((*s.model_cost_unsettled, attempt_id)))
        events.append({'type': 'model_cost', 'session': session, 'amount_micro': 0,
                       'uncertain': True, 'attempt_id': attempt_id})
    # 补记：关联原 attempt_id 补扣成本并结清；重复补记幂等，不改动原事件
    for attempt_id, amount in sorted((model_cost_settlements or {}).items()):
        if attempt_id not in s.model_cost_unsettled:
            continue  # 未知或已结清 → no-op，绝不重复扣减
        s.model_cost_unsettled = tuple(x for x in s.model_cost_unsettled if x != attempt_id)
        s.model_cost += amount
        events.append({'type': 'model_cost_settlement', 'session': session,
                       'attempt_id': attempt_id, 'amount_micro': amount})
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
    def settle_dividends():
        # Unknown dates remain receivable; known due dates settle on the first
        # processed session. Preserve the original key for deterministic replay.
        for pay_date in sorted(k for k in s.dividend_receivable if k and k <= session):
            payable = s.dividend_receivable.pop(pay_date)
            if payable:
                s.cash_available += payable
                events.append({'type': 'dividend_pay', 'session': session,
                               'pay_date': pay_date, 'total_micro': payable})

    settle_dividends()

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
            # H 与待生效保护线按与 stop 同一比例调整：只调 stop 会让保护线相对价格失真
            # （2:1 拆股后 H 仍是拆股前的价格水平 ⇒ 保护线永久高于市价 ⇒ 立刻假止损）
            pend = (None if pos.pending_stop_micro is None
                    else (pos.pending_stop_micro // ratio if kind == 'split'
                          else pos.pending_stop_micro * ratio))
            if kind == 'split':
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
            events.append({'type': 'split', 'session': session, 'security_id': sid,
                           'ratio': ratio, 'kind': kind})
        elif kind == 'cash_dividend':
            per_share = int(act['cash_amount_micro'])
            total = pos.shares * per_share
            pay_date = act.get('pay_date')
            s.dividend_receivable[pay_date] = s.dividend_receivable.get(pay_date, 0) + total
            # 除息日止损随分红下调（镜像历史引擎 simulate_fixed_horizon_exits 的 stop -= cash_amount）。
            # H 与待生效保护线同口径下调：不下调的话保护线会按除息前的价格水平停留，
            # 除息当天就把一个正常持仓判成触发。
            s.positions[sid] = replace(pos, stop_micro=max(0, pos.stop_micro - per_share),
                                       highest_completed_close_micro=max(
                                           0, pos.highest_completed_close_micro - per_share),
                                       pending_stop_micro=(
                                           None if pos.pending_stop_micro is None
                                           else max(0, pos.pending_stop_micro - per_share)))
            events.append({'type': 'dividend_record', 'session': session, 'security_id': sid,
                           'per_share_micro': per_share, 'total_micro': total,
                           'pay_date': pay_date})

    # Include entitlements recorded today whose payment is already due.
    settle_dividends()

    # 1.5 应用到期的保护线更新（T 收盘算、T+1 生效）。位置在公司行动之后、跳空止损之前：
    #     拆股/除息已按同一比例调整过 stop 与 pending；抬高后的线若已在今日开盘之上，
    #     由下面阶段 2 按**开盘价**成交（§4.2：不假定能在保护价成交）。
    if protection is not None:
        for sid, pos in list(s.positions.items()):
            if pos.pending_stop_micro is None:
                continue
            if pos.pending_stop_effective_session and pos.pending_stop_effective_session > session:
                continue  # 中间有会话被跳过：在第一个被处理的会话上生效，不提前
            new_stop = max(pos.stop_micro, pos.pending_stop_micro)
            events.append({'type': 'stop_update_applied', 'session': session,
                           'security_id': sid, 'stop_micro': new_stop,
                           'previous_stop_micro': pos.stop_micro,
                           'scheduled_session': pos.pending_stop_effective_session})
            s.positions[sid] = replace(pos, stop_micro=new_stop, pending_stop_micro=None,
                                       pending_stop_effective_session='')

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

    # 2.5 持仓评审动作（仅 L 账户；R 传 None 时整段不生效）
    # 位置在跳空止损之后、开盘入场之前：此时公司行动已调整完 shares/stop（否则评审日
    # 拆股会把减仓数量算错），且释放的仓位槽与现金当日可用，与跳空止损一致。
    # 硬退出优先由此保证：阶段 2 已清仓则本段跳过，阶段 4/5 仍作用于减仓后的剩余。
    for sid in sorted(s.positions):
        act = (position_actions or {}).get(sid)
        if act is None:
            continue
        # 主体键（`{opportunity_id}@pos:{执行日}`）随动作传入，供「为什么没应用」入账。
        # 该键与入场机会 id 不同，故不会与 `_mark_applied`/`_settle_marks` 的 missed 匹配
        # 逻辑相撞（那两处只遍历入场 intents）。
        key = act.get('subject_key') or ''
        if sid not in bars:
            events.append({'type': 'missed', 'session': session, 'security_id': sid,
                           'opportunity_id': key, 'reason': 'POSITION_NO_BARS'})
            continue  # 缺行情不动手；账户在阶段 6 记 PROVISIONAL
        pos = s.positions[sid]
        if act.get('action') == 'exit':
            shares, reason = pos.shares, 'REVIEW_EXIT'
        else:
            # tier 相对：模板里的绝对数量按「评审主体剩余量」算，而本账户剩余量可能更小
            # （已减过仓/曾否决入场），故必须按档位作用于本账户当前剩余量。
            tier = float(act.get('tier') or 0.0)
            shares = int(pos.shares * tier) if tier > 0 else 0
            reason = 'REVIEW_REDUCE'
        if shares <= 0:
            events.append({'type': 'missed', 'session': session, 'security_id': sid,
                           'opportunity_id': key, 'reason': 'POSITION_SIZE_ZERO'})
            continue
        px = bars[sid]['open']
        gross = shares * px
        fee = _fee(gross, fee_bp)
        s.unsettled_cash += gross - fee  # T+1 结算，与既有三条出场路径一致
        s.fees += fee
        if shares >= pos.shares:
            del s.positions[sid]  # 必须 del：invariants() 要求 shares > 0
            reason = 'REVIEW_EXIT'
        else:
            s.positions[sid] = replace(pos, shares=pos.shares - shares)
        events.append({**_fill(session, 'SELL', sid, shares, px, fee, reason,
                               pos.opportunity_id),
                       'decision_id': act.get('decision_id') or ''})

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
        stop = int(intent.stop_reference.get('initial_stop_micro') or
                   initial_stop_micro(open_micro, int(intent.stop_reference['atr14_micro'])))
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
                                    opportunity_id=intent.opportunity_id(),
                                    # 成交时冻结的单笔风险金额（净 R 的分母，§7）。
                                    # 可由事件重放（shares×(成交价−止损)），故不改事件 payload。
                                    initial_risk_micro=shares * (open_micro - stop))
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

    # 5.5 利润保护：用**截至今日收盘**的已知量算 T+1 生效的保护线（保护关闭时整段不生效）。
    #     放在时间退出之后：今日已按 H60 卖出的持仓不再排期（它已经不在了）。
    #     放在估值之前：估值只读个数字，与保护线无关。
    #
    #     每持仓每 session **无条件**写一条 `protection_state`：H 在激活之前也每 session
    #     都在变，只在"有事发生"时写事件会让重放重建不出 H（重放不查行情），
    #     「重放一致」就成了一句空话。激活转换、ATR 不可用、排期都由这一个事件的字段
    #     表达（`reason` / `protection_activated` / `pending_stop_*`）—— 一件事一份定义。
    if protection is not None:
        for sid, pos in list(s.positions.items()):
            if sid not in bars:
                continue  # 缺行情：不拿旧价当收盘价，保持已生效保护线（§4.2）
            close = bars[sid]['close']
            high = max(pos.highest_completed_close_micro, close)
            atr14 = (atr or {}).get(sid)
            decision = protection.evaluate(
                entry_price_micro=pos.entry_price_micro,
                initial_stop_micro=pos.initial_stop_micro,
                current_stop_micro=pos.stop_micro,
                high_close_micro=high, atr14_micro=atr14,
                activated=pos.protection_activated)
            pending = pos.pending_stop_micro
            pending_session = pos.pending_stop_effective_session
            if decision.stop_micro is not None and next_session:
                # next_session 为空串 = 窗口末尾，没有下一会话可排期（见函数头）
                pending, pending_session = decision.stop_micro, next_session
            s.positions[sid] = replace(pos, highest_completed_close_micro=high,
                                       protection_activated=decision.activated,
                                       pending_stop_micro=pending,
                                       pending_stop_effective_session=pending_session)
            events.append({'type': 'protection_state', 'session': session,
                           'security_id': sid, 'high_close_micro': high,
                           'protection_activated': decision.activated,
                           'activation_price_micro': decision.activation_price_micro,
                           'line_micro': decision.line_micro,
                           'stop_micro': decision.stop_micro,
                           'pending_stop_micro': pending,
                           'pending_stop_effective_session': pending_session,
                           'reason': decision.reason})

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
           'model_cost_uncertain_count': s.model_cost_uncertain_count,
           'cost_status': s.cost_status,
           'risk_state': s.risk_state, 'recovery_streak': s.recovery_streak,
           'revision': 1}
    events.append({'type': 'nav', **nav})
    return StepResult(state=s, events=events, nav=nav)


def new_account_state(scope: str, initial_cash: int) -> AccountState:
    return AccountState(scope=scope, cash_available=initial_cash,
                        initial_equity=initial_cash, high_water=initial_cash)
