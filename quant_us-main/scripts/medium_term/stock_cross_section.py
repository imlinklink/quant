"""B2 科技股月度截面动量候选。"""
from __future__ import annotations

import pandas as pd

from .momentum_features import (momentum_snapshot, point_in_time_momentum_snapshot,
                                rank_cross_section)
from .monthly_calendar import month_end_sessions, next_session, normalize_sessions


def _apply_members(snapshot: pd.DataFrame, members: pd.DataFrame,
                   decision) -> pd.DataFrame:
    """把**当日非成员**标为不合格（`eligible=False` + 原因），**不删行**。

    为什么标而不删：非成员要留在截面里当审计分母（"这道门排除了多少"必须算得出来），
    而 `rank_cross_section` 只对 `eligible` 的证券排名 ⇒ 标 False 就足以让排名
    **只在当日合格宇宙内**进行。

    为什么必须在排名**之前**：先按全体排名、再把不合格者剔掉，名次就是与**不可交易的**
    标的比出来的 —— 那不是时点宇宙，是「先知道答案再挑池子」。
    """
    frame = members.copy()
    frame['session'] = pd.to_datetime(frame.session).dt.tz_localize(None).dt.normalize()
    at = frame[frame.session.eq(pd.Timestamp(decision))]
    ok = set(at.loc[at.eligible.astype(bool), 'security_id'].astype(str))
    out = snapshot.copy()
    out['security_id'] = out.security_id.astype(str)
    outside = ~out.security_id.isin(ok)
    if outside.any():
        out.loc[outside, 'eligible'] = False
        out.loc[outside, 'reject_reason'] = 'OUTSIDE_PIT_UNIVERSE'
    return out


def generate_monthly_candidates(prices: pd.DataFrame, market: pd.DataFrame, *,
                                price_col='asof_close', market_price_col='asof_close',
                                market_ma_col='asof_ma200', top_n=5,
                                actions: pd.DataFrame | None = None,
                                members: pd.DataFrame | None = None) -> pd.DataFrame:
    """每月生成 B2 候选；QQQ 市场门关闭时保留排名但不选中。

    `members`（可选）= 时点宇宙掩码：需含 `security_id` / `session` / 布尔列 `eligible`。
    给了它就**只在当日合格宇宙内排名**（见 `_apply_members`）。**不给时行为与改动前
    逐字节相同**（有测试钉死）—— 这条是"宇宙是唯一变量"的前提。
    """
    if top_n <= 0:
        raise ValueError('INVALID_TOP_N')
    needed = {'session', market_price_col, market_ma_col}
    if missing := needed - set(market.columns):
        raise ValueError(f'MARKET_GATE_COLUMNS_MISSING:{",".join(sorted(missing))}')
    if members is not None and not {'security_id', 'session', 'eligible'} <= set(members.columns):
        raise ValueError('MEMBERS_COLUMNS_MISSING')
    m = market[list(needed)].copy()
    m['session'] = pd.to_datetime(m.session).dt.tz_localize(None).dt.normalize()
    if m.session.duplicated().any():
        raise ValueError('DUPLICATE_MARKET_SESSION')
    m = m.set_index('session').sort_index()
    calendar = normalize_sessions(prices.session)
    frames = []
    for decision in month_end_sessions(calendar):
        snap = (point_in_time_momentum_snapshot(prices, actions, decision)
                if actions is not None else
                momentum_snapshot(prices, decision, price_col=price_col))
        if members is not None:
            snap = _apply_members(snap, members, decision)
        snap = rank_cross_section(snap)
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
