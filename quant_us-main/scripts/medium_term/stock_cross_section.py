"""B2 科技股月度截面动量候选。"""
from __future__ import annotations

import pandas as pd

from .momentum_features import (momentum_snapshot, point_in_time_momentum_snapshot,
                                rank_cross_section)
from .monthly_calendar import month_end_sessions, next_session, normalize_sessions


def generate_monthly_candidates(prices: pd.DataFrame, market: pd.DataFrame, *,
                                price_col='asof_close', market_price_col='asof_close',
                                market_ma_col='asof_ma200', top_n=5,
                                actions: pd.DataFrame | None = None) -> pd.DataFrame:
    """每月生成 B2 候选；QQQ 市场门关闭时保留排名但不选中。"""
    if top_n <= 0:
        raise ValueError('INVALID_TOP_N')
    needed = {'session', market_price_col, market_ma_col}
    if missing := needed - set(market.columns):
        raise ValueError(f'MARKET_GATE_COLUMNS_MISSING:{",".join(sorted(missing))}')
    m = market[list(needed)].copy()
    m['session'] = pd.to_datetime(m.session).dt.tz_localize(None).dt.normalize()
    if m.session.duplicated().any():
        raise ValueError('DUPLICATE_MARKET_SESSION')
    m = m.set_index('session').sort_index()
    calendar = normalize_sessions(prices.session)
    frames = []
    for decision in month_end_sessions(calendar):
        snap = rank_cross_section(
            point_in_time_momentum_snapshot(prices, actions, decision)
            if actions is not None else
            momentum_snapshot(prices, decision, price_col=price_col))
        execution = next_session(calendar, decision)
        market_row = m.loc[decision] if decision in m.index else None
        gate = bool(market_row is not None and
                    pd.notna(market_row[market_ma_col]) and
                    float(market_row[market_price_col]) > float(market_row[market_ma_col]))
        snap['execution_session'] = execution
        snap['market_gate_open'] = gate
        snap['selected'] = snap.eligible.astype(bool) & snap['rank'].le(top_n) & gate
        snap['selection_reason'] = ''
        snap.loc[~snap.eligible.astype(bool), 'selection_reason'] = snap.loc[
            ~snap.eligible.astype(bool), 'reject_reason']
        snap.loc[snap.eligible.astype(bool) & ~gate, 'selection_reason'] = 'MARKET_GATE_CLOSED'
        snap.loc[snap.eligible.astype(bool) & gate & ~snap['rank'].le(top_n),
                 'selection_reason'] = 'BELOW_TOP_N'
        if execution is None:
            snap.loc[snap.selected, 'selected'] = False
            snap.loc[snap.eligible.astype(bool), 'selection_reason'] = 'NEXT_SESSION_MISSING'
        frames.append(snap)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
