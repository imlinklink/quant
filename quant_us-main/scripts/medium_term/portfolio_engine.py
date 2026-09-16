"""中期策略的逐日组合会计；纯离线，不连接账本或券商。"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .entry_risk import risk_sized_shares


@dataclass(frozen=True)
class RotationResult:
    equity: pd.DataFrame
    trades: pd.DataFrame
    rejected: pd.DataFrame


@dataclass(frozen=True)
class MultiAssetResult:
    equity: pd.DataFrame
    trades: pd.DataFrame
    rejected: pd.DataFrame


def _prices(frame: pd.DataFrame) -> pd.DataFrame:
    required = {'security_id', 'session', 'raw_open', 'raw_close'}
    if missing := required - set(frame.columns):
        raise ValueError(f'PORTFOLIO_PRICE_COLUMNS_MISSING:{",".join(sorted(missing))}')
    d = frame[list(required)].copy()
    d['security_id'] = d.security_id.astype(str)
    d['session'] = pd.to_datetime(d.session).dt.tz_localize(None).dt.normalize()
    if d.duplicated(['security_id', 'session']).any():
        raise ValueError('DUPLICATE_PORTFOLIO_PRICE')
    for column in ('raw_open', 'raw_close'):
        d[column] = pd.to_numeric(d[column], errors='coerce')
        if (~np.isfinite(d[column]) | (d[column] <= 0)).any():
            raise ValueError('INVALID_PORTFOLIO_PRICE')
    return d.sort_values(['session', 'security_id'])


def _action_map(actions: pd.DataFrame | None) -> dict:
    if actions is None or actions.empty:
        return {}
    required = {'security_id', 'ex_date', 'action_type'}
    if missing := required - set(actions.columns):
        raise ValueError(f'PORTFOLIO_ACTION_COLUMNS_MISSING:{",".join(sorted(missing))}')
    out = {}
    for item in actions.to_dict('records'):
        key = (str(item['security_id']), pd.Timestamp(item['ex_date']).normalize())
        out.setdefault(key, []).append(item)
    return out


def simulate_single_asset_rotation(prices: pd.DataFrame, targets: pd.DataFrame, *,
                                   initial_cash=100_000., round_trip_cost=.002,
                                   actions: pd.DataFrame | None = None,
                                   allow_fractional=True) -> RotationResult:
    """执行 B1 单资产轮动并生成逐日净值。

    `round_trip_cost` 沿用旧实验语义：完整买卖总成本；每个买入/卖出腿各扣一半。
    目标数据需由收盘后信号产生，且 execution_session 严格晚于 decision_session。
    """
    if initial_cash <= 0 or not 0 <= round_trip_cost < 1:
        raise ValueError('INVALID_PORTFOLIO_CONFIG')
    market = _prices(prices)
    required = {'decision_session', 'execution_session', 'target_security_id'}
    if missing := required - set(targets.columns):
        raise ValueError(f'TARGET_COLUMNS_MISSING:{",".join(sorted(missing))}')
    orders = targets.copy()
    for column in ('decision_session', 'execution_session'):
        orders[column] = pd.to_datetime(orders[column]).dt.tz_localize(None).dt.normalize()
    invalid = orders.execution_session.notna() & (orders.execution_session <= orders.decision_session)
    if invalid.any():
        raise ValueError('TARGET_EXECUTION_NOT_AFTER_DECISION')
    orders = orders[orders.execution_session.notna() & orders.target_security_id.notna()]
    if 'rebalance_required' in orders:
        orders = orders[orders.rebalance_required.astype(bool)]
    if orders.execution_session.duplicated().any():
        raise ValueError('DUPLICATE_EXECUTION_SESSION')
    if orders.empty:
        raise ValueError('NO_EXECUTABLE_TARGETS')
    by_execution = {row.execution_session: row for row in orders.itertuples(index=False)}
    lookup = market.set_index(['session', 'security_id'])
    sessions = pd.DatetimeIndex(market.session.unique()).sort_values()
    # 输入可包含动量预热期；组合绩效从首个可执行目标开始，不能把预热期现金
    # 伪装成策略运行期并稀释 CAGR/暴露率。
    sessions = sessions[sessions >= orders.execution_session.min()]
    action_map = _action_map(actions)
    cash, held, shares = float(initial_cash), None, 0.
    fee_rate = round_trip_cost / 2
    equity_rows, trade_rows, rejected = [], [], []
    cumulative_turnover = 0.

    for session in sessions:
        # 公司行动在除权日开盘前生效。
        if held:
            for event in action_map.get((held, session), ()):
                kind = str(event['action_type']).lower()
                if kind in ('split', 'reverse_split'):
                    ratio = float(event.get('ratio') or 0)
                    if not np.isfinite(ratio) or ratio <= 0:
                        raise ValueError(f'ACTION_RATIO_INVALID:{held}:{session.date()}')
                    shares *= ratio
                elif kind == 'cash_dividend':
                    amount = float(event.get('cash_amount') or 0)
                    if not np.isfinite(amount) or amount < 0:
                        raise ValueError(f'ACTION_CASH_INVALID:{held}:{session.date()}')
                    cash += shares * amount
                else:
                    raise ValueError(f'ACTION_TYPE_UNSUPPORTED:{kind}')

        order = by_execution.get(session)
        if order is not None:
            target = str(order.target_security_id)
            if target != held:
                try:
                    if held:
                        sell_open = float(lookup.loc[(session, held), 'raw_open'])
                    buy_open = float(lookup.loc[(session, target), 'raw_open'])
                except KeyError:
                    rejected.append({'decision_session': order.decision_session,
                                     'execution_session': session, 'target_security_id': target,
                                     'reason': 'EXECUTION_OPEN_MISSING'})
                else:
                    if held:
                        gross = shares * sell_open
                        fee = gross * fee_rate
                        cash += gross - fee
                        cumulative_turnover += gross
                        trade_rows.append({'session': session, 'security_id': held,
                                           'side': 'SELL', 'shares': shares, 'price': sell_open,
                                           'gross_notional': gross, 'fee': fee,
                                           'decision_session': order.decision_session})
                        held, shares = None, 0.
                    affordable = cash / (buy_open * (1 + fee_rate))
                    buy_shares = affordable if allow_fractional else np.floor(affordable)
                    if buy_shares <= 0:
                        # 卖出已成交但新资产买不起时保持现金。
                        rejected.append({'decision_session': order.decision_session,
                                         'execution_session': session, 'target_security_id': target,
                                         'reason': 'INSUFFICIENT_CASH'})
                    else:
                        gross = buy_shares * buy_open
                        fee = gross * fee_rate
                        cash -= gross + fee
                        cumulative_turnover += gross
                        held, shares = target, float(buy_shares)
                        trade_rows.append({'session': session, 'security_id': target,
                                           'side': 'BUY', 'shares': shares, 'price': buy_open,
                                           'gross_notional': gross, 'fee': fee,
                                           'decision_session': order.decision_session})

        market_value = 0.
        if held:
            try:
                market_value = shares * float(lookup.loc[(session, held), 'raw_close'])
            except KeyError as exc:
                raise ValueError(f'HELD_CLOSE_MISSING:{held}:{session.date()}') from exc
        equity = cash + market_value
        equity_rows.append({'session': session, 'cash': cash, 'security_id': held,
                            'shares': shares, 'market_value': market_value,
                            'equity': equity, 'gross_exposure': market_value / equity,
                            'cumulative_turnover': cumulative_turnover,
                            'initial_equity': float(initial_cash)})
    return RotationResult(pd.DataFrame(equity_rows), pd.DataFrame(trade_rows),
                          pd.DataFrame(rejected))


def _shadow_risk_sized_shares(entry_price, initial_stop, equity, cash, *,
                              risk_fraction, max_weight, fee_rate) -> float:
    """整数微定仓（与影子引擎 `risk_sized_shares_micro` 对齐，消除 float 精度差）。"""
    from decimal import Decimal, ROUND_HALF_UP
    MICRO = 1_000_000

    def _micro(x):
        return int((Decimal(str(x)) * MICRO).to_integral_value(rounding=ROUND_HALF_UP))

    price, stop = _micro(entry_price), _micro(initial_stop)
    nav, available = _micro(equity), _micro(cash)
    distance = price - stop
    risk_bp = int((Decimal(str(risk_fraction)) * 10000).to_integral_value(rounding=ROUND_HALF_UP))
    max_weight_bp = int((Decimal(str(max_weight)) * 10000).to_integral_value(rounding=ROUND_HALF_UP))
    fee_bp = int((Decimal(str(fee_rate)) * 10000).to_integral_value(rounding=ROUND_HALF_UP))
    if distance <= 0 or nav <= 0 or available < 0:
        return 0.0
    risk_shares = nav * risk_bp // 10000 // distance
    weight_shares = nav * max_weight_bp // 10000 // price
    cash_shares = available * 10000 // (price * (10000 + fee_bp))
    return float(min(risk_shares, weight_shares, cash_shares))


def _shadow_fee(gross, fee_rate) -> float:
    """整数微费用（与影子引擎 _fee 对齐）。"""
    from decimal import Decimal, ROUND_HALF_UP
    MICRO = 1_000_000
    fee_bp = int((Decimal(str(fee_rate)) * 10000).to_integral_value(rounding=ROUND_HALF_UP))
    gross_micro = int((Decimal(str(gross)) * MICRO).to_integral_value(rounding=ROUND_HALF_UP))
    return gross_micro * fee_bp // 10000 / MICRO


def simulate_multi_asset_portfolio(prices: pd.DataFrame, matrix: pd.DataFrame, *,
                                   initial_cash=100_000., risk_fraction=.01,
                                   max_weight=.20, round_trip_cost=None,
                                   actions: pd.DataFrame | None = None,
                                   allow_fractional=False, max_positions=5,
                                   evaluation_start=None,
                                   t1_settlement: bool = False,
                                   dividend_receivable: bool = False,
                                   shadow_precision: bool = False) -> MultiAssetResult:
    """按已接受的一个 B2/B3 × Exit × Cost 单元构造逐日五仓净值。

    `t1_settlement=True` / `dividend_receivable=True` 时切换到影子引擎同口径会计：
    卖出款 T+1 结算（进 unsettled_cash，下一会话可用）、除息记应收（按 pay_date）→ 支付日转现金。
    `shadow_precision=True` 时定仓与费用改用整数微美元运算（与影子引擎 `risk_sized_shares_micro`
    对齐），消除 float 精度差。默认关闭，保持历史（即时结算/float）语义不变。
    """
    required = {'security_id', 'entry_session', 'entry_price', 'initial_stop',
                'exit_session', 'exit_price', 'exit_phase', 'portfolio_accepted'}
    if missing := required - set(matrix.columns):
        raise ValueError(f'MULTI_ASSET_COLUMNS_MISSING:{",".join(sorted(missing))}')
    selected = matrix[matrix.portfolio_accepted.astype(bool)].copy()
    if selected.empty:
        raise ValueError('NO_ACCEPTED_PORTFOLIO_ENTRIES')
    if 'cost_scenario' in selected:
        costs = selected.cost_scenario.dropna().astype(float).unique()
        if len(costs) != 1:
            raise ValueError('MULTI_ASSET_REQUIRES_ONE_COST_SCENARIO')
        matrix_cost = float(costs[0])
        if round_trip_cost is not None and not np.isclose(round_trip_cost, matrix_cost):
            raise ValueError('MULTI_ASSET_COST_MISMATCH')
        round_trip_cost = matrix_cost
    round_trip_cost = .002 if round_trip_cost is None else float(round_trip_cost)
    if not 0 <= round_trip_cost < 1 or initial_cash <= 0:
        raise ValueError('INVALID_PORTFOLIO_CONFIG')
    if 'exit_method' in selected and selected.exit_method.nunique(dropna=False) != 1:
        raise ValueError('MULTI_ASSET_REQUIRES_ONE_EXIT_METHOD')
    if 'strategy' in selected and selected.strategy.nunique(dropna=False) != 1:
        raise ValueError('MULTI_ASSET_REQUIRES_ONE_STRATEGY')
    for column in ('entry_session', 'exit_session'):
        selected[column] = pd.to_datetime(selected[column]).dt.tz_localize(None).dt.normalize()
    selected = selected.sort_values(['entry_session', 'rank' if 'rank' in selected else 'security_id',
                                     'security_id']).reset_index(drop=True)
    selected['trade_id'] = range(len(selected))

    market = _prices(prices)
    # 用 (session, security_id) 字典替代 MultiIndex.loc，逐仓逐日查找从 ~10µs 降到 ~0.2µs。
    lookups = {
        'raw_open': dict(zip(zip(market.session, market.security_id),
                             market.raw_open.astype(float))),
        'raw_close': dict(zip(zip(market.session, market.security_id),
                              market.raw_close.astype(float))),
    }
    sessions = pd.DatetimeIndex(market.session.unique()).sort_values()
    start = (selected.entry_session.min() if evaluation_start is None
             else pd.Timestamp(evaluation_start).normalize())
    if start > selected.entry_session.min():
        raise ValueError('EVALUATION_START_AFTER_ENTRY')
    sessions = sessions[sessions >= start]
    entries = {day: group for day, group in selected.groupby('entry_session')}
    action_map = _action_map(actions)
    fee_rate = round_trip_cost / 2
    cash = float(initial_cash)
    unsettled_cash = 0.
    dividend_receivable_map: dict = {}
    positions, trade_rows, rejected, equity_rows = {}, [], [], []
    cumulative_turnover = 0.

    def price(day, security_id, column):
        value = lookups[column].get((day, security_id))
        if value is None:
            raise ValueError(f'HELD_PRICE_MISSING:{security_id}:{day.date()}:{column}')
        return float(value)

    def sell(position, day, exit_price, reason, phase):
        nonlocal cash, cumulative_turnover, unsettled_cash
        gross = position['shares'] * float(exit_price)
        fee = _shadow_fee(gross, fee_rate) if shadow_precision else gross * fee_rate
        if t1_settlement:
            unsettled_cash += gross - fee
        else:
            cash += gross - fee
        cumulative_turnover += gross
        trade_rows.append({'session': day, 'security_id': position['security_id'],
                           'side': 'SELL', 'shares': position['shares'],
                           'price': float(exit_price), 'gross_notional': gross,
                           'fee': fee, 'trade_id': position['trade_id'],
                           'reason': reason, 'phase': phase,
                           'entry_id': getattr(position['row'], 'entry_id', position['trade_id'])})
        del positions[position['trade_id']]

    for session in sessions:
        # 结算：T+1 卖出款 + 支付日分红 → 可用现金（影子会计）
        if t1_settlement:
            cash += unsettled_cash
            unsettled_cash = 0.
        if dividend_receivable:
            cash += dividend_receivable_map.pop(str(session.date()), 0.0)
        # 已持仓的公司行动在开盘前生效；当天新开仓不会重复获取权益。
        for position in list(positions.values()):
            for event in action_map.get((position['security_id'], session), ()):
                kind = str(event['action_type']).lower()
                if kind in ('split', 'reverse_split'):
                    ratio = float(event.get('ratio') or 0)
                    if not np.isfinite(ratio) or ratio <= 0:
                        raise ValueError(f'ACTION_RATIO_INVALID:{position["security_id"]}:{session.date()}')
                    position['shares'] *= ratio
                elif kind == 'cash_dividend':
                    amount = float(event.get('cash_amount') or 0)
                    if not np.isfinite(amount) or amount < 0:
                        raise ValueError(f'ACTION_CASH_INVALID:{position["security_id"]}:{session.date()}')
                    if dividend_receivable:
                        pay_date = str(event.get('pay_date') or '')
                        key = pay_date if pay_date else '__unsettled__'
                        dividend_receivable_map[key] = dividend_receivable_map.get(key, 0.0) + position['shares'] * amount
                    else:
                        cash += position['shares'] * amount
                else:
                    raise ValueError(f'ACTION_TYPE_UNSUPPORTED:{kind}')

        # 开盘退出先释放现金和仓位。
        for position in list(positions.values()):
            row = position['row']
            if row.exit_session == session and row.exit_phase == 'OPEN':
                sell(position, session, row.exit_price, row.exit_reason, 'OPEN')

        # 用当日开盘净值计算1%风险和20%市值上限。
        open_value = sum(position['shares'] * price(session, position['security_id'], 'raw_open')
                         for position in positions.values())
        open_equity = cash + open_value + sum(dividend_receivable_map.values())
        for row in entries.get(session, pd.DataFrame()).itertuples(index=False):
            security_id = str(row.security_id)
            if any(p['security_id'] == security_id for p in positions.values()):
                rejected.append({'session': session, 'security_id': security_id,
                                 'reason': 'DUPLICATE_ACTIVE_SECURITY',
                                 'entry_id': getattr(row, 'entry_id', row.trade_id)})
                continue
            if len(positions) >= max_positions:
                rejected.append({'session': session, 'security_id': security_id,
                                 'reason': 'MAX_POSITIONS',
                                 'entry_id': getattr(row, 'entry_id', row.trade_id)})
                continue
            actual_open = price(session, security_id, 'raw_open')
            if not np.isclose(actual_open, float(row.entry_price), rtol=1e-6):
                raise ValueError(f'MULTI_ASSET_ENTRY_OPEN_MISMATCH:{security_id}:{session.date()}')
            shares = (_shadow_risk_sized_shares(actual_open, float(row.initial_stop), open_equity,
                                               cash, risk_fraction=risk_fraction,
                                               max_weight=max_weight, fee_rate=fee_rate)
                      if shadow_precision else
                      risk_sized_shares(actual_open, float(row.initial_stop), open_equity, cash,
                                        risk_fraction=risk_fraction, max_weight=max_weight,
                                        fee_rate=fee_rate, allow_fractional=allow_fractional))
            if shares <= 0:
                rejected.append({'session': session, 'security_id': security_id,
                                 'reason': 'POSITION_SIZE_ZERO',
                                 'entry_id': getattr(row, 'entry_id', row.trade_id)})
                continue
            gross = shares * actual_open
            fee = _shadow_fee(gross, fee_rate) if shadow_precision else gross * fee_rate
            cash -= gross + fee
            cumulative_turnover += gross
            positions[row.trade_id] = {'trade_id': row.trade_id,
                                        'security_id': security_id,
                                        'shares': shares, 'row': row}
            trade_rows.append({'session': session, 'security_id': security_id,
                               'side': 'BUY', 'shares': shares, 'price': actual_open,
                               'gross_notional': gross, 'fee': fee,
                               'trade_id': row.trade_id, 'reason': 'ENTRY', 'phase': 'OPEN',
                               'entry_id': getattr(row, 'entry_id', row.trade_id)})

        # 盘中止损发生在开盘买入之后；收盘退出随后执行。
        for phase in ('INTRADAY', 'CLOSE'):
            for position in list(positions.values()):
                row = position['row']
                if row.exit_session == session and row.exit_phase == phase:
                    sell(position, session, row.exit_price, row.exit_reason, phase)

        market_value = sum(position['shares'] * price(session, position['security_id'], 'raw_close')
                           for position in positions.values())
        equity = cash + unsettled_cash + sum(dividend_receivable_map.values()) + market_value
        equity_rows.append({'session': session, 'cash': cash, 'unsettled_cash': unsettled_cash,
                            'dividend_receivable': sum(dividend_receivable_map.values()),
                            'market_value': market_value,
                            'equity': equity, 'position_count': len(positions),
                            'gross_exposure': market_value / equity,
                            'cumulative_turnover': cumulative_turnover,
                            'initial_equity': float(initial_cash)})
    return MultiAssetResult(pd.DataFrame(equity_rows), pd.DataFrame(trade_rows),
                            pd.DataFrame(rejected))
