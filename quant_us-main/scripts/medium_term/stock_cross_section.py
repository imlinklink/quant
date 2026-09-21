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


def rank_monthly_snapshot(snapshot: pd.DataFrame, decision, *, members=None) -> pd.DataFrame:
    """（可选）时点宇宙掩码 → 排名。**两条路径共用这一处。**

    批处理（`generate_monthly_candidates`）与影子增量生成器
    （`IncrementalCandidateGenerator._generate_monthly`）各自构造截面 —— 那一步两者
    **本来就不同**（缺 actions 时一个用 `momentum_snapshot`、另一个用 PIT 复权视图），
    不该硬并。要共用的是**掩码与排名**：掩码一开始只加到了批量那条上，于是"前向跑 B 臂"
    与"回测跑 B 臂"会在宇宙口径上悄悄分叉 —— 而两个 B 臂只允许差"是不是前向"。
    """
    if members is not None:
        snapshot = _apply_members(snapshot, members, decision)
    return rank_cross_section(snapshot)


def monthly_snapshots(prices: pd.DataFrame, *, actions: pd.DataFrame | None = None,
                      price_col='asof_close') -> dict:
    """每个月末一份**原始截面**（掩码与排名**之前**）。

    **抽出来是为了让多臂共享这一步。** 它按 (证券, as_of) 重建整段 as-of 复权视图 —— 与
    "面板里还有谁"无关，而实测它是整条回测的 **83%**（一个 32 只的臂 6.5 分钟里 5.3 分钟
    在这里）。多个臂各自重算，等于把最贵的一步做 N 遍。

    **为什么共享是等价的**（不是"差不多"）：掩码在**排名之前**把非成员标为不合格，而
    `rank_cross_section` 只对合格者排序、排序键是 `(momentum_score, mom_6m, security_id)`
    —— **完整序**，与行集合无关 ⇒ 成员的名次与"只喂成员"逐位相同。下游只消费
    `selected` 行，故装配结果等价。
    """
    from .momentum_features import momentum_snapshot, point_in_time_momentum_snapshot
    calendar = normalize_sessions(prices.session)
    return {decision: (point_in_time_momentum_snapshot(prices, actions, decision)
                       if actions is not None else
                       momentum_snapshot(prices, decision, price_col=price_col))
            for decision in month_end_sessions(calendar)}


def assemble_candidates(snapshots: dict, prices: pd.DataFrame, market: pd.DataFrame, *,
                        market_price_col='asof_close', market_ma_col='asof_ma200',
                        top_n=5, members: pd.DataFrame | None = None) -> pd.DataFrame:
    """装配候选表：掩码 → 排名 → 市场门 → `selected` / `selection_reason`。

    `snapshots` 可以是**整块面板**算出来的一份（多臂共享，见 `monthly_snapshots`），
    带 `members` 时非成员会被标为不合格而不参与排名 —— 于是每臂看到的就是"自己那个池子"。
    """
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
    for decision, raw in snapshots.items():
        snap = rank_monthly_snapshot(raw, decision, members=members)
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


def generate_monthly_candidates(prices: pd.DataFrame, market: pd.DataFrame, *,
                                price_col='asof_close', market_price_col='asof_close',
                                market_ma_col='asof_ma200', top_n=5,
                                actions: pd.DataFrame | None = None,
                                members: pd.DataFrame | None = None) -> pd.DataFrame:
    """每月生成 B2 候选；QQQ 市场门关闭时保留排名但不选中。

    `members`（可选）= 时点宇宙掩码：需含 `security_id` / `session` / 布尔列 `eligible`。
    给了它就**只在当日合格宇宙内排名**（见 `_apply_members`）。**不给时行为与改动前
    逐字节相同**（有测试钉死）—— 这条是"宇宙是唯一变量"的前提。

    多臂场景请用 `monthly_snapshots` + `assemble_candidates` 共享最贵的截面重建
    （见 `monthly_snapshots` 的说明）。
    """
    if top_n <= 0:
        raise ValueError('INVALID_TOP_N')
    return assemble_candidates(
        monthly_snapshots(prices, actions=actions, price_col=price_col), prices, market,
        market_price_col=market_price_col, market_ma_col=market_ma_col,
        top_n=top_n, members=members)
