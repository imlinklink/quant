"""B2/B3 的 M1–M5 中期退出路径；旧 E1–E11 保持不变。"""
from __future__ import annotations

import numpy as np
import pandas as pd


EXIT_IDS = ('M1', 'M2', 'M3', 'M4', 'M5')
FIXED_HOLDS = {'M1': 60, 'M2': 90, 'M3': 120}
MAX_HOLD = 120
MIN_OBSERVATION = 20
ATR_MULTIPLE = 3.5


def _normalise_bars(bars: pd.DataFrame, exit_id: str) -> pd.DataFrame:
    required = {'session', 'raw_open', 'raw_high', 'raw_low', 'raw_close'}
    if exit_id in ('M4', 'M5'):
        required |= {'week_complete', 'weekly_ma20'}
    if exit_id == 'M5':
        required.add('asof_atr')
    if missing := required - set(bars.columns):
        raise ValueError(f'MEDIUM_EXIT_COLUMNS_MISSING:{",".join(sorted(missing))}')
    d = bars.copy()
    d['session'] = pd.to_datetime(d.session).dt.tz_localize(None).dt.normalize()
    if d.session.duplicated().any():
        raise ValueError('DUPLICATE_EXIT_SESSION')
    d = d.sort_values('session').reset_index(drop=True)
    for column in ('raw_open', 'raw_high', 'raw_low', 'raw_close'):
        d[column] = pd.to_numeric(d[column], errors='coerce')
        if (~np.isfinite(d[column]) | (d[column] <= 0)).any():
            raise ValueError('INVALID_EXIT_PRICE')
    invalid_ohlc = ((d.raw_high < d[['raw_open', 'raw_close']].max(axis=1)) |
                    (d.raw_low > d[['raw_open', 'raw_close']].min(axis=1)) |
                    (d.raw_high < d.raw_low))
    if invalid_ohlc.any():
        raise ValueError('INVALID_EXIT_OHLC')
    return d


def _events(actions: pd.DataFrame | None, security_id: str) -> dict:
    if actions is None or actions.empty:
        return {}
    required = {'security_id', 'ex_date', 'action_type'}
    if missing := required - set(actions.columns):
        raise ValueError(f'MEDIUM_ACTION_COLUMNS_MISSING:{",".join(sorted(missing))}')
    use = actions[actions.security_id.astype(str) == str(security_id)]
    result = {}
    for item in use.to_dict('records'):
        day = pd.Timestamp(item['ex_date']).normalize()
        result.setdefault(day, []).append(item)
    return result


def _exit_result(exit_id, session, price, reason, phase, shares, cash,
                 entry_price, mfe, mae, stop, holding_sessions, data_quality='good'):
    gross = ((shares * price + cash) / entry_price - 1) if price is not None else None
    return {'exit_method': exit_id, 'exit_session': session, 'exit_price': price,
            'exit_reason': reason, 'exit_phase': phase,
            'holding_sessions': holding_sessions, 'shares_at_exit': shares,
            'cash_dividend_per_initial_share': cash, 'gross_pnl_pct': gross,
            'mfe_pct': mfe, 'mae_pct': mae, 'final_stop': stop,
            'data_quality': data_quality}


def simulate_exit(entry: dict, bars: pd.DataFrame, exit_id: str, *,
                  actions: pd.DataFrame | None = None) -> dict:
    """模拟单笔中期退出；bars 首行必须是入场日，成交价必须等于首日开盘。"""
    if exit_id not in EXIT_IDS:
        raise ValueError(f'UNKNOWN_MEDIUM_EXIT:{exit_id}')
    security_id = str(entry.get('security_id') or '')
    if not security_id:
        raise ValueError('MEDIUM_EXIT_SECURITY_ID_REQUIRED')
    d = _normalise_bars(bars, exit_id)
    if d.empty:
        return _exit_result(exit_id, None, None, 'DATA_MISSING', None, 1., 0.,
                            float(entry['entry_price']), 0., 0., np.nan, 0,
                            'missing_future_bars')
    entry_session = pd.Timestamp(entry['entry_session']).normalize()
    d = d[d.session >= entry_session].head(MAX_HOLD).reset_index(drop=True)
    if d.empty or d.session.iloc[0] != entry_session:
        raise ValueError('ENTRY_SESSION_PRICE_MISSING')
    entry_price = float(entry['entry_price'])
    initial_stop = float(entry['initial_stop'])
    if not np.isclose(entry_price, float(d.raw_open.iloc[0]), rtol=1e-6):
        raise ValueError('MEDIUM_ENTRY_OPEN_MISMATCH')
    if not 0 < initial_stop < entry_price:
        raise ValueError('INVALID_INITIAL_STOP')

    stop = initial_stop
    high = entry_price
    shares, cash = 1., 0.
    mfe = mae = 0.
    pending_trend_exit = False
    events = _events(actions, security_id)

    for i, bar in d.iterrows():
        holding = i + 1
        if i > 0:  # 入场日行动已反映在开盘价，不能重复应用。
            for event in events.get(bar.session, ()):
                kind = str(event['action_type']).lower()
                if kind in ('split', 'reverse_split'):
                    ratio = float(event.get('ratio') or 0)
                    if not np.isfinite(ratio) or ratio <= 0:
                        raise ValueError(f'ACTION_RATIO_INVALID:{security_id}:{bar.session.date()}')
                    shares *= ratio
                    stop /= ratio
                    high /= ratio
                elif kind == 'cash_dividend':
                    amount = float(event.get('cash_amount') or 0)
                    if not np.isfinite(amount) or amount < 0:
                        raise ValueError(f'ACTION_CASH_INVALID:{security_id}:{bar.session.date()}')
                    cash += shares * amount
                    stop = max(0., stop - amount)
                    high = max(0., high - amount)
                else:
                    raise ValueError(f'ACTION_TYPE_UNSUPPORTED:{security_id}:{kind}')

        op, bh, bl, close = map(float, (bar.raw_open, bar.raw_high, bar.raw_low, bar.raw_close))
        mfe = max(mfe, (shares * bh + cash) / entry_price - 1)
        mae = min(mae, (shares * bl + cash) / entry_price - 1)

        # 周线信号只能在上一完整周收盘后形成，因此在次日开盘执行。
        if pending_trend_exit:
            reason = 'GAP_STOP' if op <= stop else 'WEEKLY_TREND_EXIT'
            return _exit_result(exit_id, bar.session, op, reason, 'OPEN', shares, cash,
                                entry_price, mfe, mae, stop, holding)

        # 初始硬止损从入场日立即生效；后续先检查旧保护线。
        if i > 0 and op <= stop:
            return _exit_result(exit_id, bar.session, op, 'GAP_STOP', 'OPEN', shares,
                                cash, entry_price, mfe, mae, stop, holding)
        if bl <= stop:
            return _exit_result(exit_id, bar.session, stop, 'STOP', 'INTRADAY', shares,
                                cash, entry_price, mfe, mae, stop, holding)

        horizon = FIXED_HOLDS.get(exit_id, MAX_HOLD)
        if holding >= horizon:
            return _exit_result(exit_id, bar.session, close, 'TIME_EXIT', 'CLOSE', shares,
                                cash, entry_price, mfe, mae, stop, holding)

        high = max(high, bh)
        if exit_id == 'M5' and holding >= MIN_OBSERVATION:
            atr = float(bar.asof_atr)
            if not np.isfinite(atr) or atr <= 0:
                raise ValueError(f'ATR_INVALID:{security_id}:{bar.session.date()}')
            stop = max(stop, high - ATR_MULTIPLE * atr)

        if exit_id in ('M4', 'M5') and holding >= MIN_OBSERVATION and bool(bar.week_complete):
            weekly_ma = float(bar.weekly_ma20)
            if not np.isfinite(weekly_ma) or weekly_ma <= 0:
                raise ValueError(f'WEEKLY_MA_INVALID:{security_id}:{bar.session.date()}')
            pending_trend_exit = close < weekly_ma

    last = d.iloc[-1]
    result = _exit_result(exit_id, last.session, None, 'DATA_END', None, shares, cash,
                          entry_price, mfe, mae, stop, len(d), 'right_censored')
    result['mark_price'] = float(last.raw_close)
    result['unrealized_pnl_pct'] = (shares * float(last.raw_close) + cash) / entry_price - 1
    return result


def apply_five_position_limit(trades: pd.DataFrame, max_positions=5) -> pd.DataFrame:
    """按真实退出阶段释放仓位；收盘退出不能供同日开盘的新交易使用。"""
    required = {'entry_session', 'exit_session', 'exit_phase', 'security_id'}
    if missing := required - set(trades.columns):
        raise ValueError(f'POSITION_LIMIT_COLUMNS_MISSING:{",".join(sorted(missing))}')
    d = trades.copy()
    for column in ('entry_session', 'exit_session'):
        d[column] = pd.to_datetime(d[column]).dt.tz_localize(None).dt.normalize()
    if 'rank' not in d:
        d['rank'] = 999999
    d = d.sort_values(['entry_session', 'rank', 'security_id']).reset_index(drop=True)
    active, accepted, reasons = [], [], []
    for row in d.itertuples(index=False):
        now = row.entry_session
        active = [position for position in active
                  if position['exit_session'] > now or
                  (position['exit_session'] == now and position['exit_phase'] != 'OPEN')]
        active_ids = {position['security_id'] for position in active}
        if row.security_id in active_ids:
            accepted.append(False); reasons.append('DUPLICATE_ACTIVE_SECURITY')
        elif len(active) >= max_positions:
            accepted.append(False); reasons.append('MAX_POSITIONS')
        elif pd.isna(row.exit_session):
            accepted.append(False); reasons.append('EXIT_UNAVAILABLE')
        else:
            accepted.append(True); reasons.append('')
            active.append({'security_id': row.security_id, 'exit_session': row.exit_session,
                           'exit_phase': row.exit_phase})
    d['portfolio_accepted'] = accepted
    d['portfolio_reject_reason'] = reasons
    return d


def run_exit_matrix(entries: pd.DataFrame, daily_bars: pd.DataFrame, *,
                    actions: pd.DataFrame | None = None,
                    costs=(.001, .002, .005, .01), position_usd=20_000.,
                    max_positions=5) -> pd.DataFrame:
    """把冻结的 B2/B3 入场运行成 M1–M5 × 成本矩阵。"""
    required = {'security_id', 'execution_session', 'initial_stop'}
    if missing := required - set(entries.columns):
        raise ValueError(f'MEDIUM_ENTRY_COLUMNS_MISSING:{",".join(sorted(missing))}')
    if not {'security_id', 'session'}.issubset(daily_bars.columns):
        raise ValueError('MEDIUM_DAILY_ID_COLUMNS_MISSING')
    selected = entries.copy()
    if 'selected' in selected:
        selected = selected[selected.selected.astype(bool)]
    selected['execution_session'] = pd.to_datetime(
        selected.execution_session).dt.tz_localize(None).dt.normalize()
    daily = daily_bars.copy()
    daily['security_id'] = daily.security_id.astype(str)
    daily['session'] = pd.to_datetime(daily.session).dt.tz_localize(None).dt.normalize()
    groups = {security_id: group.sort_values('session').reset_index(drop=True)
              for security_id, group in daily.groupby('security_id')}
    rows = []
    for record in selected.to_dict('records'):
        security_id = str(record['security_id'])
        stock = groups.get(security_id)
        if stock is None:
            raise ValueError(f'MEDIUM_DAILY_SECURITY_MISSING:{security_id}')
        entry_day = pd.Timestamp(record['execution_session']).normalize()
        future = stock[stock.session >= entry_day].head(MAX_HOLD).reset_index(drop=True)
        if future.empty or future.session.iloc[0] != entry_day:
            raise ValueError(f'MEDIUM_ENTRY_SESSION_MISSING:{security_id}:{entry_day.date()}')
        entry_price = float(future.raw_open.iloc[0])
        base = dict(record)
        base.update({'entry_session': entry_day, 'entry_price': entry_price,
                     'position_usd': float(position_usd)})
        for exit_id in EXIT_IDS:
            result = simulate_exit(base, future, exit_id, actions=actions)
            for cost in costs:
                gross = result.get('gross_pnl_pct')
                rows.append({**base, **result, 'cost_scenario': float(cost),
                             'net_pnl_pct': gross - cost if gross is not None else None,
                             'net_pnl_usd': ((gross - cost) * position_usd
                                             if gross is not None else None)})
    matrix = pd.DataFrame(rows)
    if matrix.empty:
        return matrix
    out = []
    group_columns = ['exit_method', 'cost_scenario']
    if 'strategy' in matrix:
        group_columns.insert(0, 'strategy')
    for _, group in matrix.groupby(group_columns, dropna=False):
        out.append(apply_five_position_limit(group, max_positions=max_positions))
    return pd.concat(out, ignore_index=True)
