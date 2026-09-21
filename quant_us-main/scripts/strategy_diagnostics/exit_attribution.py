"""Trade accounting from actual fills; post-exit labels are descriptive, not alpha."""
from collections import Counter, defaultdict
import pandas as pd

# 引擎实际产出的 SELL 原因。**两种写法都要扫**：
#   · 字面量：`_fill(session, 'SELL', ..., 'GAP_STOP', ...)` —— 三条规则出场；
#   · 变量：意图侧的减仓路径先算 `reason = 'REVIEW_EXIT' / 'REVIEW_REDUCE'` 再传给 `_fill`。
# 原先只扫字面量，于是 `REVIEW_REDUCE`（部分减仓，仓位未清）**从来没有出现在这里**，
# 而扫描用的正则也看不见它 —— "引擎新增原因时不会静默漏掉"那句保证当时是空的。
# 现在测试两种写法都扫（§14「证明在注入对应缺陷时失败」）。
KNOWN_EXIT_REASONS = ('STOP', 'GAP_STOP', 'TIME_EXIT', 'REVIEW_EXIT', 'REVIEW_REDUCE')
STOP_REASONS = ('STOP', 'GAP_STOP')


def followup(trade, prices, actions, calendar, end, horizons=(5, 20, 60)):
    """Buy-and-hold path of ONE exit-date share, with splits and dividend credits."""
    if not trade.get('exit_session'):
        return {}
    day = pd.Timestamp(trade['exit_session'])
    sessions = calendar[(calendar > day) & (calendar <= pd.Timestamp(end))]
    stock = prices[prices.security_id.eq(trade['security_id'])].set_index('session')
    acts = actions[actions.security_id.eq(trade['security_id'])] if not actions.empty else actions
    quantity, dividends, values, recovery = 1., 0., [], None
    for i, session in enumerate(sessions[:max(horizons)], 1):
        if session not in stock.index:
            break  # Reference calendar gap: don't silently jump over a missing session.
        for a in acts.to_dict('records'):
            if pd.Timestamp(a['ex_date']) != session:
                continue
            if a['action_type'] == 'split':
                quantity *= float(a['ratio'])
            elif a['action_type'] == 'reverse_split':
                quantity /= float(a['ratio'])
            elif a['action_type'] == 'cash_dividend':
                dividends += quantity * float(a['cash_amount'])
        value = quantity * float(stock.loc[session, 'raw_close']) + dividends
        ret = value / trade['exit_price'] - 1
        values.append(ret)
        if recovery is None and value >= trade['entry_price_exit_basis']:
            recovery = i
    return {str(h): {'status': 'mature' if len(values) >= h else 'pending_or_missing',
                    'total_return': values[h-1] if len(values) >= h else None,
                    'max_close_return': max(values[:h]) if len(values) >= h else None,
                    'min_close_return': min(values[:h]) if len(values) >= h else None,
                    'sessions_to_entry_recovery': (recovery if recovery is not None and recovery <= h else None)}
            for h in horizons}


def trades_from_events(events, state, prices, actions, calendar, end):
    active, trades = {}, []
    for e in events:
        sid = e.get('security_id')
        if e['type'] == 'fill' and e['side'] == 'BUY':
            risk = e['shares'] * (e['price_micro'] - e['stop_micro'])
            active[sid] = {'trade_id': e['opportunity_id'], 'security_id': sid,
                           'entry_session': e['session'], 'shares': e['shares'],
                           'entry_price_exit_basis': e['price_micro'] / 1e6,
                           'initial_risk_micro': risk, 'entry_cost_micro': e['shares'] * e['price_micro'],
                           'cashflows_micro': -e['shares'] * e['price_micro'] - e['fee_micro'],
                           'fees_micro': e['fee_micro'], 'dividends_micro': 0,
                           'exit_session': None, 'exit_reason': None, 'holding_sessions': 0}
        elif sid in active:
            t = active[sid]
            if e['type'] == 'split':
                factor = e['ratio'] if e['kind'] == 'split' else 1 / e['ratio']
                t['shares'] = int(t['shares'] * factor)
                t['entry_price_exit_basis'] /= factor
            elif e['type'] == 'dividend_record':
                t['dividends_micro'] += e['total_micro']
                t['cashflows_micro'] += e['total_micro']
            elif e['type'] == 'hold':
                t['holding_sessions'] = e['holding_sessions']
            elif e['type'] == 'fill' and e['side'] == 'SELL':
                t['cashflows_micro'] += e['shares'] * e['price_micro'] - e['fee_micro']
                t['fees_micro'] += e['fee_micro']
                t['shares'] -= e['shares']
                if t['shares'] == 0:
                    t.update(exit_session=e['session'], exit_reason=e['reason'],
                             exit_price=e['price_micro'] / 1e6, status='closed',
                             net_pnl_micro=t['cashflows_micro'])
                    trades.append(active.pop(sid))
    marks = prices[prices.session.eq(pd.Timestamp(end))].set_index('security_id').raw_close
    for sid, t in active.items():
        t['status'] = 'right_censored'
        t['net_pnl_micro'] = (t['cashflows_micro'] + state.positions[sid].shares * round(marks[sid] * 1e6)
                              if sid in marks else None)
        trades.append(t)
    for t in trades:
        t['net_r'] = (t['net_pnl_micro'] / t['initial_risk_micro']
                      if t['net_pnl_micro'] is not None and t['initial_risk_micro'] > 0 else None)
        t['followup'] = followup(t, prices, actions, calendar, end)
    return trades


def summarize(trades, initial_cash_micro=None):
    """§6.2 的退出指标。

    原先只报笔数/净损益/平均净 R，而 §6.2 还要**持有期**、**对账户收益贡献**、
    **成熟比例**、**止损后恢复的比例与耗时**。持有期其实早就算在 `t['holding_sessions']` 里，
    只是一直没被报出来 —— 采集了不等于报出来了（同 §7.2 的 capacity）。
    """
    reasons = defaultdict(lambda: {'count': 0, 'net_pnl_micro': 0, 'net_r': [], 'holding': []})
    for t in trades:
        key = t['exit_reason'] or 'RIGHT_CENSORED'
        row = reasons[key]
        row['count'] += 1
        if t['net_pnl_micro'] is not None:
            row['net_pnl_micro'] += t['net_pnl_micro']
        if t['net_r'] is not None:
            row['net_r'].append(t['net_r'])
        if t.get('holding_sessions'):
            row['holding'].append(t['holding_sessions'])
    for row in reasons.values():
        values, holding = row.pop('net_r'), row.pop('holding')
        row['mean_net_r'] = sum(values) / len(values) if values else None
        row['mean_holding_sessions'] = sum(holding) / len(holding) if holding else None
        # 对账户收益贡献：以初始资金为分母，与 full_cost_return 同口径
        row['share_of_account_return'] = (row['net_pnl_micro'] / initial_cash_micro
                                          if initial_cash_micro else None)
    closed = [t for t in trades if t['status'] == 'closed']

    # 成熟比例：每个期限有多少笔真的走完了那么久（右删失/窗口尾部的不能算）
    maturity = {}
    for horizon in sorted({h for t in trades for h in t['followup']}, key=int):
        rows = [t['followup'][horizon] for t in trades if horizon in t['followup']]
        mature = sum(1 for r in rows if r['status'] == 'mature')
        maturity[horizon] = {'mature': mature, 'pending_or_missing': len(rows) - mature,
                             'mature_share': (mature / len(rows)) if rows else None}

    # 止损后恢复：只统计**成熟**的期限，否则"未恢复"会把"还没走完"混进来
    stop_trades = [t for t in trades if (t.get('exit_reason') in STOP_REASONS)]
    recovery = {}
    for horizon in sorted({h for t in trades for h in t['followup']}, key=int):
        considered = [t['followup'][horizon] for t in stop_trades
                      if horizon in t['followup'] and t['followup'][horizon]['status'] == 'mature']
        recovered = [r['sessions_to_entry_recovery'] for r in considered
                     if r.get('sessions_to_entry_recovery') is not None]
        recovery[horizon] = {
            'stop_trades_mature': len(considered),
            'recovered_to_entry': len(recovered),
            'recovered_share': (len(recovered) / len(considered)) if considered else None,
            'mean_sessions_to_recovery': (sum(recovered) / len(recovered)) if recovered else None,
            'unrecovered': len(considered) - len(recovered),
        }

    # **从 `KNOWN_EXIT_REASONS` 派生**，不再另写一份。原先这里手写三项 + RIGHT_CENSORED，
    # 与上面的常量各自漂移 ⇒ 引擎自己产出的 `REVIEW_EXIT`（模型减仓/退出）会被报成
    # 「未归类」。那是一条会喊狼来了的告警：它一旦喊错，真正的未知原因就没人看了。
    # 仍未归类的只应是引擎新加、而这里还不知道的原因。
    classified = set(KNOWN_EXIT_REASONS) | {'RIGHT_CENSORED'}
    return {'count': len(trades), 'closed': len(closed),
            'right_censored': len(trades) - len(closed),
            'realized_win_rate': (sum(t['net_pnl_micro'] > 0 for t in closed) / len(closed) if closed else None),
            'exit_reasons': dict(reasons),
            'followup_mature': dict(Counter(h for t in trades for h, row in t['followup'].items()
                                           if row['status'] == 'mature')),
            'followup_maturity': maturity,
            'stop_recovery': recovery,
            # 引擎新增一个退出原因而这里没归类时，它必须**看得见**，否则会静默漏出统计
            'unclassified_reasons': sorted({t['exit_reason'] for t in trades
                                            if t.get('exit_reason') and t['exit_reason'] not in classified}),
            'note': '退出后路径仅作描述；最高收盘收益不是可实现卖点。未平仓计入账户净值，不计入已实现胜率。'}
