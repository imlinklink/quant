"""B3 择时：周线趋势门 + 日线突破/回踩入场。

注册规则（第一版，逐字段固定）：
- 周线门：仅用**完整周**（W-FRI，周内交易日数与参考历一致且非最后一周）；
  最近一个完整周满足 `周收盘 > 20 周均线` **且** `20 周均线不下降`（≥ 上一完整周），
  则该门自该周最后一个交易日起保持开启，直到下一个完整周改写。
- 等待窗：自候选的执行日（月度信号后 T+1）起，最多 `max_wait_sessions` 个交易日；
  到期未入场则该候选作废，等下一个月度排名。
- 日线突破：收盘 > 此前 `breakout_lookback` 日最高价。
- 日线回踩：当日最低 ≤ MA20 或 MA50（容差 tol）且收盘 > 前一交易日最高价。
- 同日两者都成立时，固定优先取 `pullback`。信号次日开盘入场。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

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


def build_timed_entries(candidates: pd.DataFrame, bars: pd.DataFrame,
                        reference_sessions, *, max_wait_sessions: int = 20,
                        breakout_lookback: int = 20, pullback_ma: tuple = (20, 50),
                        tol: float = .01) -> pd.DataFrame:
    """对每个候选在其等待窗内寻找第一个合格入场日；返回入场日与次日执行日。"""
    required = {'security_id', 'decision_session', 'execution_session', 'rank'}
    if missing := required - set(candidates.columns):
        raise ValueError(f'B3_CANDIDATE_COLUMNS_MISSING:{",".join(sorted(missing))}')
    calendar = pd.DatetimeIndex(pd.to_datetime(list(reference_sessions))).normalize().unique()
    calendar = pd.DatetimeIndex(calendar).sort_values()
    rows = []
    for sid, group in bars.groupby('security_id'):
        regime = weekly_regime(group, calendar).set_index('session')
        signals = entry_signals(group, breakout_lookback=breakout_lookback,
                                pullback_ma=pullback_ma, tol=tol).set_index('session')
        sessions = pd.DatetimeIndex(group.sort_values('session').session)
        for row in candidates[candidates.security_id.eq(sid)].itertuples(index=False):
            start = pd.Timestamp(row.execution_session).normalize()
            window = sessions[(sessions >= start)][:max_wait_sessions]
            for day in window:
                if not bool(regime.weekly_uptrend.get(day, False)):
                    continue
                kind = signals.entry_signal.get(day, '')
                if not kind:
                    continue
                position = sessions.get_loc(day)
                if position + 1 >= len(sessions):
                    break  # 无次日开盘
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
    out = pd.DataFrame(rows)
    return out if not out.empty else pd.DataFrame(columns=['security_id', 'rank',
                                                           'decision_session', 'signal_session',
                                                           'execution_session', 'entry_type'])
