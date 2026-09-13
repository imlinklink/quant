"""用完整交易周构造中期退出特征，避免把部分周当作周收盘。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def add_weekly_exit_features(bars: pd.DataFrame, reference_sessions,
                             *, ma_weeks: int = 20) -> pd.DataFrame:
    """返回逐日数据；仅在完整周最后一个交易日标记并给出周线均线。

    reference_sessions 必须是独立的完整交易日历，并至少延伸到 bars 最后一周
    的下一交易周。这样样本尾部的部分周不会被误判成完整周。
    """
    required = {'session', 'raw_close'}
    if missing := required - set(bars.columns):
        raise ValueError(f'WEEKLY_FEATURE_COLUMNS_MISSING:{",".join(sorted(missing))}')
    if ma_weeks < 1:
        raise ValueError('WEEKLY_MA_WEEKS_INVALID')
    d = bars.copy()
    d['session'] = pd.to_datetime(d.session).dt.tz_localize(None).dt.normalize()
    if d.session.duplicated().any():
        raise ValueError('DUPLICATE_WEEKLY_FEATURE_SESSION')
    d = d.sort_values('session').reset_index(drop=True)
    close = pd.to_numeric(d.raw_close, errors='coerce')
    if (~np.isfinite(close) | (close <= 0)).any():
        raise ValueError('INVALID_WEEKLY_FEATURE_CLOSE')
    calendar = pd.DatetimeIndex(pd.to_datetime(list(reference_sessions))).tz_localize(None).normalize()
    calendar = calendar.sort_values().unique()
    if len(calendar) == 0:
        raise ValueError('REFERENCE_SESSIONS_EMPTY')
    if not set(d.session).issubset(set(calendar)):
        raise ValueError('BARS_OUTSIDE_REFERENCE_SESSIONS')

    d['_week'] = d.session.dt.to_period('W-FRI')
    calendar_frame = pd.DataFrame({'session': calendar})
    calendar_frame['_week'] = calendar_frame.session.dt.to_period('W-FRI')
    expected_last = calendar_frame.groupby('_week').session.max()
    latest_week = calendar_frame['_week'].max()
    weekly = d.groupby('_week', sort=True).agg(
        week_session=('session', 'max'), weekly_close=('raw_close', 'last'),
        observed_sessions=('session', 'size'))
    expected_count = calendar_frame.groupby('_week').session.size()
    weekly['complete'] = (weekly.week_session.eq(weekly.index.map(expected_last)) &
                          weekly.observed_sessions.eq(weekly.index.map(expected_count)) &
                          (weekly.index < latest_week))
    completed = weekly[weekly.complete].copy()
    completed['weekly_ma20'] = completed.weekly_close.rolling(
        ma_weeks, min_periods=ma_weeks).mean()
    feature_map = completed.set_index('week_session').weekly_ma20
    d['week_complete'] = d.session.isin(completed.week_session)
    d['weekly_ma20'] = d.session.map(feature_map)
    return d.drop(columns=['_week'])
