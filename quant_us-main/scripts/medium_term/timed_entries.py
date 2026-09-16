"""B3 择时：周线趋势门 + 日线突破/回踩入场。

注册规则（第一版，逐字段固定）：
- 周线门：仅用**完整周**（W-FRI，周内交易日数与参考历一致且非最后一周）；
  最近一个完整周满足 `周收盘 > 20 周均线` **且** `20 周均线不下降`（≥ 上一完整周），
  则该门自该周最后一个交易日起保持开启，直到下一个完整周改写。
- 等待窗：自候选的执行日（月度信号后 T+1）起，最多 `max_wait_sessions` 个交易日
  （按**参考交易日历**计数，缺行情不会无意延长等待期限）；到期未入场则该候选作废，
  等下一个月度排名。
- 日线突破：收盘 > 此前 `breakout_lookback` 日最高价。
- 日线回踩：当日最低 ≤ MA20 或 MA50（容差 tol）且收盘 > 前一交易日最高价。
- 同日两者都成立时，固定优先取 `pullback`。信号次日开盘入场。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.data.price_views import build_price_view

from .weekly_features import add_weekly_exit_features


def weekly_regime(bars: pd.DataFrame, reference_sessions, *, ma_weeks: int = 20) -> pd.DataFrame:
    """返回逐日 `weekly_uptrend`：由最近完整周的门状态前向保持。"""
    d = add_weekly_exit_features(bars, reference_sessions, ma_weeks=ma_weeks)
    d = d.sort_values('session').reset_index(drop=True)
    complete = d['week_complete'].astype(bool)
    ma = pd.to_numeric(d['weekly_ma20'], errors='coerce')
    idx = d.index[complete]
    ma_c = ma.loc[idx]
    up_c = ma_c.ge(ma_c.shift(1)).fillna(False)  # 与上一完整周比较，不是上一行
    above_c = d.loc[idx, 'raw_close'].astype(float).gt(ma_c)
    flag = pd.Series(np.nan, index=d.index)
    flag.loc[idx] = (above_c & up_c).astype(float)
    d['weekly_uptrend'] = flag.ffill().fillna(0.).astype(bool)
    return d[['session', 'weekly_uptrend']]


def entry_signals(bars: pd.DataFrame, *, breakout_lookback: int = 20,
                  pullback_ma: tuple = (20, 50), tol: float = .01) -> pd.DataFrame:
    """逐日突破/回踩信号；同日两者成立时按 pullback 优先。"""
    d = bars.sort_values('session').reset_index(drop=True).copy()
    high = pd.to_numeric(d.raw_high, errors='coerce')
    close = pd.to_numeric(d.raw_close, errors='coerce')
    low = pd.to_numeric(d.raw_low, errors='coerce')
    prior_high = high.shift(1)
    d['breakout'] = close > high.rolling(breakout_lookback, min_periods=breakout_lookback).max().shift(1)
    touched = pd.Series(False, index=d.index)
    for window in pullback_ma:
        ma = close.rolling(window, min_periods=window).mean()
        touched = touched | low.le(ma * (1 + tol))
    d['pullback'] = touched & close.gt(prior_high)
    d['entry_signal'] = np.where(d.pullback, 'pullback', np.where(d.breakout, 'breakout', ''))
    return d[['session', 'entry_signal']]


def _adjusted_bars(bars: pd.DataFrame, actions, as_of) -> pd.DataFrame:
    """原始 OHLC → build_price_view 按 as_of 复权 → 改回 raw_* 命名（跨拆股连续）。"""
    d = bars.rename(columns={'raw_open': 'open', 'raw_high': 'high',
                             'raw_low': 'low', 'raw_close': 'close'})
    if 'volume' not in d.columns:
        d['volume'] = 1.0
    if actions is None or actions.empty:
        return d.rename(columns={'open': 'raw_open', 'high': 'raw_high',
                                 'low': 'raw_low', 'close': 'raw_close'})
    sid = str(bars.security_id.iloc[0])
    first_session = pd.to_datetime(bars.session).dt.normalize().min()
    act = actions[actions.security_id.astype(str).eq(sid)].copy()
    act = act[pd.to_datetime(act.ex_date).dt.normalize().gt(first_session)]
    view = build_price_view(d, act, price_basis='asof_adjusted', as_of=as_of)
    return view.rename(columns={'open': 'raw_open', 'high': 'raw_high',
                                'low': 'raw_low', 'close': 'raw_close'})


def build_timed_entries(candidates: pd.DataFrame, bars: pd.DataFrame,
                        reference_sessions, *, actions=None,
                        max_wait_sessions: int = 20, breakout_lookback: int = 20,
                        pullback_ma: tuple = (20, 50), tol: float = .01) -> pd.DataFrame:
    """对每个候选在其等待窗内寻找第一个合格入场日；返回入场日与次日执行日。

    择时特征（周线门/突破/回踩）按候选等待窗末日的拆股复权价计算，避免原始序列
    跨拆股产生虚假信号；`actions` 为 None 时退回原始价（保持既有测试语义）。
    """
    required = {'security_id', 'decision_session', 'execution_session', 'rank'}
    if missing := required - set(candidates.columns):
        raise ValueError(f'B3_CANDIDATE_COLUMNS_MISSING:{",".join(sorted(missing))}')
    calendar = pd.DatetimeIndex(pd.to_datetime(list(reference_sessions))).normalize().unique()
    calendar = pd.DatetimeIndex(calendar).sort_values()
    rows = []
    bar_sids = set(bars.security_id.astype(str))
    for row in candidates[~candidates.security_id.astype(str).isin(bar_sids)].itertuples(index=False):
        # 候选缺整段行情 → DATA_BLOCKED 终态（不静默丢弃）
        rows.append({'security_id': row.security_id, 'rank': int(row.rank),
                     'decision_session': pd.Timestamp(row.decision_session).normalize(),
                     'signal_session': pd.NaT, 'execution_session': pd.NaT,
                     'entry_type': 'DATA_BLOCKED'})
    for sid, group in bars.groupby('security_id'):
        group = group.sort_values('session').reset_index(drop=True)
        sessions = pd.DatetimeIndex(group.session)
        for row in candidates[candidates.security_id.eq(sid)].itertuples(index=False):
            start = pd.Timestamp(row.execution_session).normalize()
            # 等待窗按参考交易日历计数：证券自身缺行情（停牌/缺 bar）不延长等待期限。
            ref_window = calendar[calendar >= start][:max_wait_sessions]
            window = sessions[sessions.isin(ref_window)]
            if len(window) == 0:
                rows.append({'security_id': sid, 'rank': int(row.rank),
                             'decision_session': pd.Timestamp(row.decision_session).normalize(),
                             'signal_session': pd.NaT, 'execution_session': pd.NaT,
                             'entry_type': 'EXPIRED'})
                continue
            adj = _adjusted_bars(group, actions, window[-1]) if actions is not None else group
            regime = weekly_regime(adj, calendar).set_index('session')
            signals = entry_signals(adj, breakout_lookback=breakout_lookback,
                                    pullback_ma=pullback_ma, tol=tol).set_index('session')
            for day in window:
                if not bool(regime.weekly_uptrend.get(day, False)):
                    continue
                kind = signals.entry_signal.get(day, '')
                if not kind:
                    continue
                position = sessions.get_loc(day)
                if position + 1 >= len(sessions):
                    # 无次日开盘 → 明确终态（不静默丢弃）
                    rows.append({'security_id': sid, 'rank': int(row.rank),
                                 'decision_session': pd.Timestamp(row.decision_session).normalize(),
                                 'signal_session': day, 'execution_session': pd.NaT,
                                 'entry_type': 'NO_NEXT_OPEN'})
                    break
                rows.append({'security_id': sid, 'rank': int(row.rank),
                             'decision_session': pd.Timestamp(row.decision_session).normalize(),
                             'signal_session': day, 'execution_session': sessions[position + 1],
                             'entry_type': kind})
                break
            else:
                rows.append({'security_id': sid, 'rank': int(row.rank),
                             'decision_session': pd.Timestamp(row.decision_session).normalize(),
                             'signal_session': pd.NaT, 'execution_session': pd.NaT,
                             'entry_type': 'EXPIRED'})
    if len(rows) != len(candidates):
        raise ValueError('TIMED_ENTRIES_DENOMINATOR_MISMATCH')
    out = pd.DataFrame(rows)
    return out if not out.empty else pd.DataFrame(columns=['security_id', 'rank',
                                                           'decision_session', 'signal_session',
                                                           'execution_session', 'entry_type'])
