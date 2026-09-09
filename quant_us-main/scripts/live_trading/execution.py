"""统一审批执行入口。受理≠成交；未知结果保留占用，禁止盲目重试。"""
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from .position_registry import REGISTRY
from .decision_ledger.event_store import enqueue, stable_id, EventStore

ACTIVE = {'intent', 'submitting', 'submitted', 'partially_filled', 'unknown', 'reconciling'}
TERMINAL = {'filled', 'cancelled', 'rejected'}


def finite_positive(value):
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (ValueError, TypeError):
        return False


def risk_quantity(price, stop, equity, cash, positions, orders, cfg, group, cap):
    if not all(finite_positive(v) for v in (price, stop, equity, cash, cap)) or stop >= price:
        raise ValueError('入场价、初始止损或账户资金无效')
    if not group or group not in cfg.get('group_limits', {}):
        raise ValueError('缺少产业链风险组或上限')
    if any(not finite_positive(p.get('initial_risk')) or not p.get('risk_group') for p in positions):
        raise ValueError('有未定义风险的持仓，禁止新增风险')
    fractions = [cfg.get('per_trade', .0025), cfg.get('total', .015),
                 cfg.get('max_position_fraction', .15), cfg['group_limits'][group]]
    if any(not finite_positive(v) or float(v)>1 for v in fractions):
        raise ValueError('风险比例必须在0到1之间')
    cost = float(cfg.get('cost_per_share', .05))
    if not math.isfinite(cost) or cost < 0:
        raise ValueError('成本预算无效')
    per_share = price - stop + cost
    used = sum(float(p['initial_risk']) for p in positions)
    group_used = sum(float(p['initial_risk']) for p in positions if p['risk_group'] == group)
    reserved_cash = 0.0
    for order in orders:
        if order['side'] != 'buy' or order['status'] not in ACTIVE:
            continue
        fraction = max(0.0, 1 - order.get('filled_qty', 0) / order['qty'])
        reserved = order['risk'] * fraction
        used += reserved
        if order['metadata']['risk_group'] == group:
            group_used += reserved
        reserved_cash += order['qty'] * fraction * order['price']
    allowance = min(equity * float(cfg.get('per_trade', 0.0025)),
                    equity * float(cfg.get('total', 0.015)) - used,
                    equity * float(cfg['group_limits'][group]) - group_used)
    qty = math.floor(min(allowance / per_share, max(0, cash-reserved_cash) / (price + float(cfg.get('cost_per_share', .05))),
                         cap / price, equity * float(cfg.get('max_position_fraction', .15)) / price))
    if qty < 1:
        raise ValueError('账户/产业链/现金风险额度不足')
    return qty, qty * per_share


class ExecutionService:
    def __init__(self, pool, config, store, dry_run=False, registry=None):
        self.pool, self.config, self.store, self.dry_run = pool, config, store, dry_run
        self.registry = registry or REGISTRY
        self.env = 'DRY-RUN' if dry_run else config.get('live_manager', {}).get('trd_env', 'SIMULATE')
        self.acc_id = int(config.get('live_manager', {}).get('acc_id', 0))
        if dry_run:
            self.registry.configure('DRY-RUN')

    def account(self, ctx):
        from futu import RET_OK
        if not self.acc_id:
            ret, data = ctx.get_acc_list()
            if ret != RET_OK:
                raise ValueError('无法确认交易账户')
            candidates = [r for r in data.to_dict('records') if str(r['trd_env']) == self.env
                          and 'US' in str(r.get('trdmarket_auth', ''))
                          and str(r.get('sim_acc_type', '')) not in ('OPTION', 'FUTURES')]
            if len(candidates) != 1:
                raise ValueError('美股账户不唯一，请配置 live_manager.acc_id')
            self.acc_id = int(candidates[0]['acc_id'])
        self.registry.configure(f'{self.env}:{self.acc_id}')
        return {'trd_env': self.env, 'acc_id': self.acc_id}

    def reconcile(self):
        """只查询，不重发。每次执行前和每轮审批轮询执行。"""
        if self.dry_run:
            EventStore(self.registry).export()
            return
        from futu import RET_OK
        with self.pool.get_trade_ctx() as ctx:
            args = self.account(ctx)
            with self.registry.transaction() as book:
                pending = [dict(o) for o in book['orders'].values() if o['status'] in ACTIVE]
            for order in pending:
                ret, rows = ctx.order_list_query(order_id=order.get('order_id', ''),
                                                code=order['code'], refresh_cache=True, **args)
                if ret != RET_OK:
                    continue
                records = rows.to_dict('records')
                if not order.get('order_id'):
                    records = [r for r in records if r.get('remark') == order['id']]
                if len(records) == 1:
                    self.apply_report(order['id'], records[0])
            # 发布持久化订单状态到当前进程审批页。
            with self.registry.transaction() as book:
                reports = list(book['orders'].values())
            for order in reports:
                self.publish(order)
        EventStore(self.registry).export()

    def publish(self, order):
        if self.store is not None:
            status = {'filled': 'executed', 'cancelled': 'failed', 'rejected': 'failed',
                      'submitting': 'unknown', 'intent': 'unknown'}.get(order['status'], order['status'])
            self.store.recover_order(order, status, self.env)
            self.store.mark(order['id'], status,
                note=f"订单 {order.get('order_id', '')}：{order['status']}，已成交 {order.get('filled_qty', 0)}/{order['qty']}")

    def apply_report(self, pid, report):
        status = str(report.get('order_status', ''))
        filled = float(report.get('dealt_qty') or 0)
        avg = float(report.get('dealt_avg_price') or 0)
        with self.registry.transaction() as book:
            order = book['orders'][pid]
            if order.get('status') == 'reconciling':
                # 待核对期间：普通回报不能更新经济数据或解除限制，只能等 apply_correction
                return 'reconciling'
            if not math.isfinite(filled) or filled > order['qty']:
                raise ValueError('订单累计成交数量异常')
            if filled < order.get('filled_qty', 0):
                # 累计数量倒退：进入待核对，不静默改写；券商更正由 apply_correction 应用
                self._flag_reconciling(book, pid, '累计成交数量倒退')
                return 'reconciling'
            delta = filled - order.get('filled_qty', 0)
            if delta and order['status'] in TERMINAL:
                # 终结订单新增成交：进入待核对，禁止静默改写已冻结 R0
                self._flag_reconciling(book, pid, '终结订单出现新增成交')
                return 'reconciling'
            if delta and not finite_positive(avg):
                raise ValueError('成交均价缺失，保留订单等待对账')
            previous_amount = float(order.get('filled_amount', order.get('filled_qty', 0)*order.get('avg_price', 0)))
            amount = filled*avg if filled and avg else previous_amount
            delta_amount = amount-previous_amount
            if not math.isfinite(amount) or (delta and delta_amount <= 0) or (not delta and abs(delta_amount) > 1e-6):
                raise ValueError('累计成交金额异常，等待对账')
            fee = report.get('cumulative_fee')
            if fee is not None:
                fee = float(fee)
                if not math.isfinite(fee) or fee < 0:
                    raise ValueError('累计费用异常')
            previous_fee = order.get('cumulative_fee')
            fee_delta = (fee - (previous_fee or 0)) if fee is not None else 0
            links = {k: order.get('proposal', {}).get(k) for k in ('signal_id','plan_id','plan_version','review_id')}
            links.update(proposal_id=pid, order_intent_id=order.setdefault('order_intent_id', stable_id('intent', self.registry.namespace, pid)),
                         broker_order_id=str(report.get('order_id') or order.get('order_id', '')))
            pos = book['positions'].get(order['code'])
            if delta and order['side']=='sell' and pos and delta>float(pos['qty'])+1e-8:
                raise ValueError('卖出增量超过本地持仓，等待完整对账')
            trades = book.setdefault('trades', {})
            if 'trade_id' not in order:
                order['trade_id'] = ((pos or {}).get('trade_id') if order['side'] == 'sell' else None) or stable_id(
                    'trade', self.registry.namespace, pid if order['side'] == 'buy' else [order['code'], (pos or {}).get('opened_at')])
            tid = order['trade_id']
            links['trade_id'] = tid
            trade = trades.get(tid)
            if trade is None and (delta or pos):
                legacy = order['side'] == 'sell'
                trade = dict(trade_id=tid, code=order['code'], direction=(pos or {}).get('direction', order['metadata'].get('direction','long')),
                    entry_qty=float((pos or {}).get('qty', 0)) if legacy else 0,
                    entry_amount=float((pos or {}).get('qty', 0))*float((pos or {}).get('entry_price',0)) if legacy else 0,
                    exit_qty=0, exit_amount=0, fees=0, initial_r0=None, provisional_r=None,
                    entry_terminal=legacy, legacy=legacy, fee_complete=False,
                    basis_known=not legacy or bool(pos and finite_positive(pos.get('entry_price'))),
                    initial_stop=order['metadata'].get('initial_stop') if not legacy else None,
                    opened_at=time.time(), status='open')
                trades[tid] = trade
            if delta:
                fill_id = stable_id('fill', self.registry.namespace, links['broker_order_id'] or pid, filled)
                fill = dict(fill_id=fill_id, order_intent_id=links['order_intent_id'],
                            broker_order_id=links['broker_order_id'], proposal_id=pid, trade_id=tid,
                            code=order['code'], side=order['side'], qty=delta, amount=delta_amount,
                            price=delta_amount/delta, cumulative_qty=filled, cumulative_amount=amount,
                            fee_delta=fee_delta if fee is not None else None)
                book.setdefault('fills', []).append(fill)
                enqueue(book, self.registry.namespace, 'fill_received', fill_id, fill, fill_id=fill_id, **links)
                prefix = 'entry' if order['side'] == 'buy' else 'exit'
                trade[prefix+'_qty'] += delta
                trade[prefix+'_amount'] += delta_amount
            if delta and order['side'] == 'buy':
                meta = order['metadata']
                if pos is None:
                    pos = dict(meta, code=order['code'], opened_at=time.time(), qty=0)
                    book['positions'][order['code']] = pos
                pos.update(qty=filled, entry_price=avg, last_reconciled_at=time.time(),
                           trade_id=tid, initial_risk=filled * (abs(avg-meta['initial_stop']) + order['cost_per_share']))
                if trade['initial_r0'] is None:
                    trade['provisional_r'] = filled * abs(avg-meta['initial_stop'])
            elif delta and pos:
                left = max(0, pos['qty'] - delta)
                if left:
                    pos['initial_risk'] = float(pos.get('initial_risk', 0)) * left / pos['qty']
                    pos['qty'] = left
                else:
                    del book['positions'][order['code']]
            previous_status = order['status']
            order.update(filled_qty=filled, filled_amount=amount, avg_price=amount/filled if filled else 0,
                         order_id=str(report.get('order_id') or order.get('order_id', '')),
                         updated_at=time.time())
            if status == 'FILLED_ALL' and filled == order['qty']:
                order['status'] = 'filled'
            elif status in ('CANCELLED_ALL', 'CANCELLED_PART', 'DELETED'):
                order['status'] = 'cancelled'
            elif status in ('FAILED', 'SUBMIT_FAILED', 'DISABLED'):
                order['status'] = 'rejected'
            elif filled:
                order['status'] = 'partially_filled'
            else:
                order['status'] = 'submitted'
            if previous_status in TERMINAL and not delta:
                order['status'] = previous_status  # late acknowledgement cannot reopen an order
            if fee is not None:
                order['cumulative_fee'] = fee
                if trade:
                    trade['fees'] += fee_delta
                if previous_fee != fee:
                    order['fee_revision'] = int(order.get('fee_revision',0))+1
                    enqueue(book, self.registry.namespace, 'fee_adjusted', [pid, order['fee_revision']],
                            {'cumulative_fee': fee, 'delta': fee_delta}, **links)
            if trade:
                if order['side'] == 'buy' and order['status'] in TERMINAL:
                    trade['entry_terminal'] = True
                    if trade['initial_r0'] is None:
                        trade['initial_r0'] = trade['provisional_r']
                self._recompute_trade_fields(book, order, trade, links)
                if delta or previous_status != order['status'] or previous_fee != fee and fee is not None:
                    enqueue(book, self.registry.namespace, 'trade_accounted',
                            [tid, pid, filled, order['status'], fee, order.get('fee_revision',0)], dict(trade), **links)
            if previous_status != order['status'] or delta:
                event_type = {'unknown':'order_unknown','rejected':'order_rejected'}.get(order['status'], 'order_submitted')
                enqueue(book, self.registry.namespace, event_type, [pid, order['status'], filled],
                        {'status': order['status'], 'filled_qty': filled, 'quantity': order['qty']}, **links)
            saved = dict(order)
        self.publish(saved)

    def _recompute_trade_fields(self, book, order, trade, links):
        """重算交易经济字段（费用完整度 / 毛净已实现盈亏 / 剩余数量与风险 / 交易状态）。

        常规回报与更正共用，保证账本与重建报表一致。
        """
        if not trade:
            return
        tid = trade['trade_id']
        related = [o for o in book['orders'].values() if o.get('trade_id') == tid and o.get('filled_qty')]
        trade['fee_complete'] = (not trade['legacy'] and all(o.get('cumulative_fee') is not None for o in related))
        qty = trade['entry_qty']
        basis = trade['entry_amount'] / qty if qty else 0
        sign = -1 if trade['direction'] == 'short' else 1
        trade['gross_realized_pnl'] = sign * (trade['exit_amount'] - basis * trade['exit_qty']) if trade.get('basis_known', True) else None
        trade['net_realized_pnl'] = trade['gross_realized_pnl'] - trade['fees'] if trade['fee_complete'] and trade['gross_realized_pnl'] is not None else None
        trade['remaining_qty'] = qty - trade['exit_qty']
        trade['remaining_initial_risk'] = (trade['initial_r0'] * trade['remaining_qty'] / qty
                                           if qty and trade['initial_r0'] is not None else None)
        if trade['exit_qty'] and trade['remaining_qty'] == 0 and trade['status'] != 'closed':
            trade.update(status='closed', closed_at=time.time())
            enqueue(book, self.registry.namespace, 'trade_closed', tid, dict(trade), **links)
        elif trade['exit_qty'] and trade['remaining_qty'] > 0:
            trade['status'] = 'partially_closed'

    def _flag_reconciling(self, book, pid, reason):
        """发现差异时标记待核对并记录事件；不静默修成表面一致。"""
        order = book['orders'][pid]
        # 保留最后可信状态（第一次标记时），避免后续恢复用丢失的 cancelled/filled
        if order.get('status') != 'reconciling':
            order['last_trusted_status'] = order.get('status')
        order['status'] = 'reconciling'
        order['reconcile_reason'] = reason
        order['reconcile_flagged_at'] = time.time()
        links = {k: order.get('proposal', {}).get(k) for k in ('signal_id', 'plan_id', 'plan_version', 'review_id')}
        links.update(proposal_id=pid, order_intent_id=order.get('order_intent_id', ''))
        enqueue(book, self.registry.namespace, 'correction_needed', [pid, reason],
                {'reason': reason, 'status': 'reconciling'}, **links)

    def apply_correction(self, pid, correction):
        """人工对账确认后应用券商成交更正。事务一致，追加事件保留原记录。

        correction: {correction_id, reason, dealt_qty?, dealt_avg_price?, cumulative_fee?}
        - dealt_qty/dealt_avg_price: 更正后的累计成交数量/均价（终结订单新增成交或数量倒退）
        - cumulative_fee: 更正后的累计费用（缺失后补齐 / 上调 / 下调 / 退款）
        R0 一旦冻结不再被重写；更正只追加事件与修订计数。
        """
        cid = str(correction.get('correction_id') or '').strip()
        reason = str(correction.get('reason') or '').strip()
        if not cid or not reason:
            raise ValueError('更正必须带 correction_id 与理由')
        with self.registry.transaction() as book:
            order = book['orders'].get(pid)
            if not order:
                raise ValueError('订单不存在')
            applied = set(order.get('applied_corrections') or [])
            if cid in applied:
                return order['status']  # 幂等：同一 correction_id 重复回放不重复应用
            links = {k: order.get('proposal', {}).get(k) for k in ('signal_id', 'plan_id', 'plan_version', 'review_id')}
            links.update(proposal_id=pid, order_intent_id=order.get('order_intent_id', ''))
            before = dict(filled_qty=order.get('filled_qty', 0), avg_price=order.get('avg_price', 0),
                          cumulative_fee=order.get('cumulative_fee'), status=order['status'],
                          fee_revision=order.get('fee_revision', 0))
            trades = book.setdefault('trades', {})
            trade = trades.get(order.get('trade_id'))

            # ---- 数量更正：重算成交增量（终结订单新增成交 / 数量倒退） ----
            if 'dealt_qty' in correction:
                new_qty = float(correction['dealt_qty'])
                if not math.isfinite(new_qty) or new_qty < 0 or new_qty > order['qty']:
                    raise ValueError('更正数量非法')
                old_qty = float(order.get('filled_qty', 0))
                delta_qty = new_qty - old_qty
                if delta_qty:
                    new_avg = float(correction.get('dealt_avg_price') or order.get('avg_price', 0))
                    if not finite_positive(new_avg):
                        raise ValueError('成交均价缺失，无法应用数量更正')
                    amount = new_qty * new_avg
                    delta_amount = amount - float(order.get('filled_amount', old_qty * float(order.get('avg_price', 0))))
                    fill_id = stable_id('fill', self.registry.namespace, order.get('order_id', '') or pid, new_qty)
                    fill = dict(fill_id=fill_id, order_intent_id=links['order_intent_id'],
                                broker_order_id=order.get('order_id', ''), proposal_id=pid,
                                trade_id=order.get('trade_id'), code=order['code'], side=order['side'],
                                qty=delta_qty, amount=delta_amount, price=delta_amount / delta_qty,
                                cumulative_qty=new_qty, cumulative_amount=amount, fee_delta=None,
                                correction_id=cid)
                    book.setdefault('fills', []).append(fill)
                    enqueue(book, self.registry.namespace, 'fill_received', fill_id, fill, fill_id=fill_id, **links)
                    order.update(filled_qty=new_qty, filled_amount=amount, avg_price=new_avg, updated_at=time.time())
                    if trade:
                        prefix = 'entry' if order['side'] == 'buy' else 'exit'
                        trade[prefix + '_qty'] = float(trade.get(prefix + '_qty', 0)) + delta_qty
                        trade[prefix + '_amount'] = float(trade.get(prefix + '_amount', 0)) + delta_amount
                        # 更正不得重写已冻结 R0，只记可追溯的修订计数
                        if trade.get('initial_r0') is not None:
                            trade['r0_revision'] = int(trade.get('r0_revision', 0)) + 1
                    # 按 trade_id + 成交账本重算当前剩余（entry_qty - exit_qty），
                    # 不把历史入场累计数量当作当前剩余；已部分退出/已清仓都要正确。
                    remaining = (float(trade.get('entry_qty', 0)) - float(trade.get('exit_qty', 0))
                                 if trade else new_qty)
                    pos = book['positions'].get(order['code'])
                    # 防止旧交易周期更正应用到同标的新周期
                    if pos is not None and pos.get('trade_id') != order.get('trade_id'):
                        pos = None
                    if remaining > 0:
                        if pos is None:
                            pos = dict(order['metadata'], code=order['code'], opened_at=time.time(), qty=0)
                            book['positions'][order['code']] = pos
                        pos.update(qty=remaining, entry_price=new_avg, trade_id=order.get('trade_id'))
                    elif pos is not None:
                        del book['positions'][order['code']]
                    order['status'] = 'filled' if new_qty == order['qty'] else 'partially_filled'

            # ---- 费用更正 ----
            if 'cumulative_fee' in correction:
                new_fee = float(correction['cumulative_fee'])
                if not math.isfinite(new_fee) or new_fee < 0:
                    raise ValueError('更正费用非法')
                fee_delta = new_fee - float(order.get('cumulative_fee') or 0)
                order['cumulative_fee'] = new_fee
                order['fee_revision'] = int(order.get('fee_revision', 0)) + 1
                if trade:
                    trade['fees'] = float(trade.get('fees', 0)) + fee_delta
                enqueue(book, self.registry.namespace, 'fee_adjusted', [pid, order['fee_revision']],
                        {'cumulative_fee': new_fee, 'delta': fee_delta, 'correction_id': cid}, **links)

            # 解除待核对：恢复最后可信状态，或按更正后数量重算
            if order.get('status') == 'reconciling':
                if 'dealt_qty' in correction:
                    order['status'] = 'filled' if float(correction['dealt_qty']) == order['qty'] else 'partially_filled'
                else:
                    order['status'] = order.get('last_trusted_status') or order['status']
                order.pop('last_trusted_status', None)
                order.pop('reconcile_reason', None)
                order.pop('reconcile_flagged_at', None)

            # 重算交易经济字段 + 追加新版交易快照（保留历史）
            if trade:
                self._recompute_trade_fields(book, order, trade, links)
                enqueue(book, self.registry.namespace, 'trade_accounted',
                        [trade['trade_id'], pid, order.get('filled_qty'), order['status'],
                         order.get('cumulative_fee'), order.get('fee_revision', 0)], dict(trade), **links)

            # 记录已应用更正（幂等键，重复回放不重复应用）
            applied.add(cid)
            order['applied_corrections'] = list(applied)
            # 追加更正事件（保留 before 快照，可审计；correction_id 幂等）
            enqueue(book, self.registry.namespace, 'correction_applied', cid,
                    {'correction_id': cid, 'reason': reason, 'before': before,
                     'after': dict(filled_qty=order.get('filled_qty', 0), avg_price=order.get('avg_price', 0),
                                   cumulative_fee=order.get('cumulative_fee'), status=order['status'])},
                    **links)
            saved = dict(order)
        self.publish(saved)
        return saved['status']

    def fresh_quote(self, code):
        import pandas as pd
        from futu import RET_OK
        with self.pool.get_quote_ctx() as ctx:
            ret, state = ctx.get_market_state([code])
            if ret != RET_OK or state.empty or str(state.iloc[0]['market_state']) not in ('MORNING', 'AFTERNOON'):
                raise ValueError('市场不在已支持的常规交易时段')
            ret, snapshot = ctx.get_market_snapshot([code])
            if ret != RET_OK or snapshot.empty:
                raise ValueError('无法取得最新行情快照')
        row = snapshot.iloc[0]
        timestamp = pd.Timestamp(row.get('update_time'))
        if pd.isna(timestamp):
            raise ValueError('报价时间戳缺失')
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize('America/New_York')
        age = (pd.Timestamp.now(tz='UTC')-timestamp).total_seconds()
        if not -5 <= age <= float(self.config.get('risk_budget', {}).get('quote_max_age_seconds', 30)):
            raise ValueError('报价过期')
        return float(row['last_price']), float(row.get('volume') or 0), time.time()

    def submit_system_exit(self, *, exit_id, code, qty, price, reason, category='hard_risk'):
        """系统硬退出（PR1）：不依赖 proposal/llm_ready/人工确认，直达真实卖单。

        只用于 hard_risk 类退出（固定/移动止损、组合熔断、券商风险）。仍执行
        账户作用域、持仓数量、活跃卖单、交易时段、幂等与对账校验。
        DRY-RUN 直接按可成交价冲正；真实环境走常规时段市价/可成交限价。
        """
        if not finite_positive(price):
            raise ValueError('硬退出报价无效')
        if not finite_positive(qty):
            raise ValueError('硬退出数量无效')
        side = 'sell'
        cfg = self.config.get('risk_budget', {})
        with self.registry.transaction() as book:
            pos = book['positions'].get(code)
            if not pos or float(pos.get('qty', 0)) <= 0:
                raise ValueError('无可退出持仓')
            if exit_id in book['orders']:
                return book['orders'][exit_id]['status']
            # 活跃卖单占用：不超卖
            if any(o['code'] == code and o['side'] == 'sell' and o['status'] in ACTIVE
                   for o in book['orders'].values()):
                raise ValueError('该股票已有活跃卖单，禁止重复硬退出')
            direction = pos.get('direction', 'long')
            sell_qty = min(float(qty), float(pos.get('qty', 0)))
            if sell_qty <= 0:
                raise ValueError('可退出数量为 0')
            tid = pos.get('trade_id') or exit_id
            meta = dict(pos.get('metadata') or {})
            meta.update(direction=direction, hard_exit=True, category=category, reason=reason)
            order = dict(proposal={}, id=exit_id, order_intent_id=stable_id('intent', self.registry.namespace, exit_id),
                         code=code, side=side, price=price, qty=sell_qty, risk=0.0,
                         metadata=meta, status='submitting', filled_qty=0,
                         cost_per_share=float(cfg.get('cost_per_share', .05)),
                         trade_id=tid, created_at=time.time())
            book['orders'][exit_id] = order
            enqueue(book, self.registry.namespace, 'hard_exit_intent_created', exit_id,
                    {'code': code, 'qty': sell_qty, 'price': price, 'reason': reason,
                     'category': category},
                    proposal_id=exit_id, order_intent_id=order['order_intent_id'], trade_id=tid)
        if self.dry_run:
            self.apply_report(exit_id, dict(order_id='dry-' + exit_id, order_status='FILLED_ALL',
                                            dealt_qty=sell_qty, dealt_avg_price=price,
                                            cumulative_fee=0.0))
            return 'filled'
        from futu import RET_OK, OrderType, TrdSide, TimeInForce
        try:
            with self.pool.get_trade_ctx() as ctx:
                args = self.account(ctx)
                et = datetime.now(ZoneInfo('America/New_York'))
                if et.weekday() >= 5 or not (570 <= et.hour * 60 + et.minute < 960):
                    with self.registry.transaction() as book:
                        book['orders'][exit_id]['status'] = 'rejected'
                    raise ValueError('硬退出仅支持美股常规时段')
                trd_side = TrdSide.SELL
                # 硬退出用可成交限价（市价附近偏移），保证盘中即时成交又不过度滑点
                ret, data = ctx.place_order(
                    price=round(price, 2), qty=sell_qty, code=code, trd_side=trd_side,
                    order_type=OrderType.NORMAL, time_in_force=TimeInForce.DAY,
                    fill_outside_rth=False, remark=exit_id, **args)
            if ret != RET_OK or data is None or data.empty:
                raise RuntimeError('硬退出提交结果不确定，等待券商对账')
            self.apply_report(exit_id, data.iloc[0].to_dict())
        except Exception:
            with self.registry.transaction() as book:
                if book['orders'][exit_id]['status'] not in TERMINAL:
                    book['orders'][exit_id]['status'] = 'unknown'
                    enqueue(book, self.registry.namespace, 'order_unknown', exit_id,
                            {'status': 'unknown'},
                            proposal_id=exit_id, order_intent_id=order['order_intent_id'],
                            trade_id=tid)
            raise
        with self.registry.transaction() as book:
            return book['orders'][exit_id]['status']

    def submit(self, item, price, cap=None):
        from .approval.proposal_store import ProposalStore
        if not self.store or not item or item.get('status') != 'executing' or not ProposalStore.llm_ready(item):
            raise ValueError('缺少人工确认或LLM评估，禁止执行')
        fresh = self.store.get(item['id'])
        if not fresh or fresh['status'] != 'executing' or time.time() >= float(fresh['expires_at']):
            raise ValueError('审批已失效')
        if fresh.get('plan_id'):
            from mutifactor.llm.trade_review import approval_binding
            if (not ProposalStore.llm_ready(fresh) or fresh.get('approved_binding') != approval_binding(fresh)
                    or approval_binding(item) != approval_binding(fresh)):
                raise ValueError('计划/评估版本与批准不匹配')
            # Always execute the persisted, approved payload, never a caller copy.
            item = fresh
        if not finite_positive(price):
            raise ValueError('最新报价无效')
        if item.get('plan_id') and abs(price-float(item['price']))/float(item['price']) > float(item['max_price_drift_pct']):
            raise ValueError('报价超出本次批准的价格容忍范围')
        self.reconcile()
        quote_fetched = time.time()
        day_volume = None
        if not self.dry_run:
            price, day_volume, quote_fetched = self.fresh_quote(item['stock_code'])
            drift = abs(price-float(item['price']))/float(item['price'])
            limit = float(self.config.get('trading', {}).get('live_trading', {}).get('human_approval', {}).get('max_price_drift_pct', .03))
            if item.get('plan_id'):
                limit = min(limit,float(item['max_price_drift_pct']))
            if drift > limit:
                raise ValueError('最新报价偏离审批价，需重新确认')
        side = item.get('side', 'buy')
        meta = dict(item.get('trade_plan') or {})
        cfg = self.config.get('risk_budget', {})
        if self.dry_run:
            equity = float(cfg.get('dry_run_equity', 100000))
            cash = equity - sum(p['qty']*p['entry_price'] for p in self.registry.all().values())
            broker_positions = None
        else:
            from futu import RET_OK, Currency
            with self.pool.get_trade_ctx() as ctx:
                args = self.account(ctx)
                ret, positions = ctx.position_list_query(refresh_cache=True, **args)
                if ret != RET_OK:
                    raise ValueError('持仓对账失败')
                broker_positions = {r['code']: r for r in positions.to_dict('records') if float(r['qty']) != 0}
                # 有其他订单就不新增或重复退出：也涵盖历史券商保护单的竞态。
                ret, orders = ctx.order_list_query(refresh_cache=True, **args)
                if ret != RET_OK:
                    raise ValueError('券商挂单查询失败')
                done = {'FILLED_ALL', 'CANCELLED_ALL', 'CANCELLED_PART', 'FAILED', 'DELETED', 'SUBMIT_FAILED', 'DISABLED'}
                if any(str(r['order_status']) not in done and (side == 'buy' or r['code'] == item['stock_code'])
                       for r in orders.to_dict('records')):
                    raise ValueError('存在活动券商订单，等待成交/撤单对账')
                if side == 'buy':
                    ret, account = ctx.accinfo_query(currency=Currency.USD, refresh_cache=True, **args)
                    if ret != RET_OK or account.empty:
                        raise ValueError('美元净值/现金不可用')
                    equity = float(account.iloc[0]['total_assets'])
                    cash = float(account.iloc[0]['cash'])
        pid, code = item['id'], item['stock_code']
        with self.registry.transaction(approval=item if item.get('plan_id') else None) as book:
            if item.get('plan_id'):
                if item['account_scope'] != self.registry.namespace:
                    raise ValueError('审批账户不匹配')
                # Durable credentials were checked under this same database writer lock.
                from mutifactor.llm.trade_review import approval_binding
                if item['approved_binding'] != approval_binding(item) or not ProposalStore.llm_ready(item):
                    raise ValueError('批准或模型评估已经失效')
            if time.time()-quote_fetched > 30 or time.time() >= float(fresh['expires_at']):
                raise ValueError('复核耗时过长，报价或审批已过期')
            if pid in book['orders']:
                return book['orders'][pid]['status']
            if any(o['code'] == code and o['status'] in ACTIVE for o in book['orders'].values()):
                raise ValueError('该股票订单尚未终结')
            if side == 'buy':
                if self.dry_run:
                    cash = equity - sum(p['qty']*p['entry_price'] for p in book['positions'].values())
                if any(o['status'] in ('unknown', 'submitting', 'intent') for o in book['orders'].values()):
                    raise ValueError('有未知订单，禁止新增风险')
                if broker_positions is not None:
                    if set(broker_positions) != set(book['positions']):
                        raise ValueError('存在未知/未对账持仓，禁止新增风险')
                    if any(abs(float(broker_positions[c]['qty'])) != p['qty'] for c,p in book['positions'].items()):
                        raise ValueError('券商持仓数量不一致')
                if code in book['positions']:
                    raise ValueError('禁止重复买入及亏损摊平')
                occupied = set(book['positions']) | {o['code'] for o in book['orders'].values()
                                                     if o['side']=='buy' and o['status'] in ACTIVE}
                if len(occupied) >= int(cfg.get('max_positions', 3)):
                    raise ValueError('持仓及待成交买单已达到组合数量上限')
                if not meta or not finite_positive(meta.get('initial_stop')):
                    raise ValueError('缺少交易计划/初始止损')
                group = cfg.get('code_groups', {}).get(code)
                meta.update(risk_group=group, entry_mode=item.get('entry_mode'), signal_id=meta.get('signal_id') or f"{item.get('entry_mode')}:{code}:{meta.get('signal_time', pid)}")
                qty, risk = risk_quantity(price, float(meta['initial_stop']), equity, cash,
                    list(book['positions'].values()), list(book['orders'].values()), cfg, group,
                    min(float(cap or item.get('per_stock_capital') or 5000), float(item['quantity'])*price))
                if day_volume is not None:
                    liquidity_qty = math.floor(day_volume * float(cfg.get('max_day_volume_fraction', .001)))
                    if liquidity_qty < 1:
                        raise ValueError('成交量不足/未知，禁止新增风险')
                    risk *= min(qty, liquidity_qty) / qty
                    qty = min(qty, liquidity_qty)
                if any(o['code']==code and o['side']=='buy' and o['metadata'].get('signal_id')==meta['signal_id']
                       and o['status'] not in ('cancelled','rejected') for o in book['orders'].values()):
                    raise ValueError('该信号已提交过订单')
                if meta.get('target'):
                    rr=(float(meta['target'])-price)/(price-float(meta['initial_stop']))
                    if rr < float(meta.get('min_rr', 1.5)):
                        raise ValueError('最新价下目标盈亏比不足')
                if meta.get('breakout_level'):
                    atr=float(meta.get('signal_atr') or 0)
                    if atr <= 0 or (price-float(meta['breakout_level']))/atr > float(meta.get('max_chase_atr', .5)):
                        raise ValueError('突破追价超过ATR上限')
                # 阶段 H2/J3：仓位档位影子接入。默认 shadow → 不改变数量；
                # 仅当 llm_permissions.position_scale = constrained_action 且模型建议降档才真正缩量。
                # 加档(>1.0)永远不允许。
                try:
                    from scripts.live_trading.llm_permission import level_for, allowed_scale
                    review = (item.get('llm') or {})
                    proposed = review.get('position_scale')
                    level = level_for('position_scale', self.config)
                    final_scale, applied = allowed_scale('position_scale', level, proposed)
                    if applied and proposed is not None and 0 <= float(proposed) < 1.0:
                        import math as _m
                        scaled_qty = int(_m.floor(qty * float(proposed)))
                        if scaled_qty < 1:
                            raise ValueError(f'模型降档 {proposed}x 后数量不足一手')
                        risk = risk * scaled_qty / qty
                        qty = scaled_qty
                    if proposed is not None and not applied:
                        # 影子：仅记录（review 里已带建议，写入 note 供审计）
                        meta['position_scale'] = float(proposed)
                        meta['position_scale_applied'] = False
                        meta['position_scale_level'] = str(level)
                except ValueError:
                    raise
                except Exception:
                    # 影子接入失败不能阻塞交易（权限默认关闭）
                    pass
            else:
                pos = book['positions'].get(code)
                if self.dry_run:
                    qty = float((pos or {}).get('qty', 0))
                    direction = (pos or {}).get('direction', 'long')
                else:
                    bp = broker_positions.get(code, {})
                    direction = 'long' if bp.get('position_side') == 'LONG' else 'short'
                    qty = min(abs(float(bp.get('qty',0))), float(bp.get('can_sell_qty' if direction=='long' else 'can_buy_qty',0)))
                qty = min(qty, float(item['quantity']))
                if qty <= 0:
                    raise ValueError('无可平仓数量')
                meta['direction'] = direction
                risk = 0
            tid = stable_id('trade', self.registry.namespace, pid) if side == 'buy' else (book['positions'].get(code) or {}).get('trade_id')
            meta.update({k: item[k] for k in ('signal_id','plan_id','plan_version','review_id','input_snapshot_id') if k in item})
            order = dict(proposal=dict(item), id=pid, order_intent_id=stable_id('intent', self.registry.namespace, pid),
                         code=code, side=side, price=price, qty=qty, risk=risk,
                         metadata=meta, status='submitting', filled_qty=0, cost_per_share=float(cfg.get('cost_per_share', .05)),
                         created_at=time.time())
            if tid:
                order['trade_id'] = tid
            book['orders'][pid] = order
            enqueue(book, self.registry.namespace, 'order_intent_created', order['order_intent_id'],
                    {'quantity': qty, 'price': price, 'planned_r': risk, 'side': side},
                    proposal_id=pid, order_intent_id=order['order_intent_id'], trade_id=tid,
                    **{k: item[k] for k in ('signal_id','plan_id','plan_version','review_id') if k in item})
        if self.dry_run:
            self.apply_report(pid, dict(order_id='dry-'+pid, order_status='FILLED_ALL', dealt_qty=qty, dealt_avg_price=price,
                                        cumulative_fee=qty*order['cost_per_share']))
            return 'filled'
        from futu import RET_OK, OrderType, TrdSide, TimeInForce
        try:
            with self.pool.get_trade_ctx() as ctx:
                args = self.account(ctx)
                # 限价单限制审批后的价格变化；只在常规时段执行，盘前盘后需要另行验证。
                et = datetime.now(ZoneInfo('America/New_York'))
                if et.weekday() >= 5 or not (570 <= et.hour*60+et.minute < 960):
                    with self.registry.transaction() as book:
                        book['orders'][pid]['status'] = 'rejected'
                    raise ValueError('当前执行器仅支持美股常规时段')
                trd_side = TrdSide.BUY if side=='buy' or meta.get('direction')=='short' else TrdSide.SELL
                ret, data = ctx.place_order(price=round(price, 2), qty=qty, code=code, trd_side=trd_side,
                    order_type=OrderType.NORMAL, time_in_force=TimeInForce.DAY, fill_outside_rth=False,
                    remark=pid, **args)
            if ret != RET_OK or data is None or data.empty:
                # SDK 返回错误也可能发生在响应丢失之后；不猜测订单不存在。
                raise RuntimeError('订单提交结果不确定，等待券商对账')
            self.apply_report(pid, data.iloc[0].to_dict())
        except Exception:
            with self.registry.transaction() as book:
                if book['orders'][pid]['status'] not in TERMINAL:
                    book['orders'][pid]['status'] = 'unknown'
                    enqueue(book, self.registry.namespace, 'order_unknown', pid, {'status': 'unknown'},
                            proposal_id=pid, order_intent_id=order['order_intent_id'], trade_id=tid,
                            **{k: item[k] for k in ('signal_id','plan_id','plan_version','review_id') if k in item})
            raise
        with self.registry.transaction() as book:
            return book['orders'][pid]['status']


def service_for(owner):
    if not hasattr(owner, '_execution_service'):
        owner._execution_service = ExecutionService(owner.pool, owner.config, owner.approval_store, owner.dry_run,
            registry=owner.approval_store.registry if owner.approval_store else None)
    return owner._execution_service


def execute_approved_buy(owner, item):
    pid, code = item.get('id'), item.get('stock_code')
    if not pid or not code or not owner.approval_store:
        return
    if not owner.approval_store.mark(pid, 'executing', note='已完成人工确认及LLM评估，复核风险'):
        return
    service = service_for(owner)
    try:
        price = owner._get_current_price(code)
        if not finite_positive(price):
            raise ValueError('无法获取最新报价')
        drift = abs(price-float(item['price'])) / float(item['price'])
        if drift > owner.approval_max_drift:
            raise ValueError('报价偏离审批价过大，需重新确认')
        if owner._get_position_count() >= owner.max_positions:
            raise ValueError('持仓数量已满')
        service.submit(owner.approval_store.get(pid), price, owner._effective_position_size_usd())
    except Exception as exc:
        with service.registry.transaction() as book:
            order = book['orders'].get(pid)
        if order:
            service.publish(order)
        else:
            owner.approval_store.mark(pid, 'failed', note=str(exc))


def record_broker_sample(sample, path=None):
    """留存脱敏券商字段样例：只保留字段名与值类型，不落敏感值/凭证。

    在写/校准券商适配器前先留真实字段样例，供成交更正与费用补齐设计参考。
    """
    import json as _json
    from pathlib import Path as _Path
    out = {}
    for k, v in (sample or {}).items():
        if str(k).lower() in ('api_key', 'token', 'authorization', 'password', 'secret', 'cookie'):
            continue
        out[str(k)] = type(v).__name__
    path = _Path(path or _Path(__file__).resolve().parents[1] / 'data' / 'broker_samples.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(_json.dumps({'ts': time.time(), 'fields': out}, ensure_ascii=False) + '\n')
    return out
