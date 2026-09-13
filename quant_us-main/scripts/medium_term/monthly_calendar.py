"""月末决策日和下一交易日映射；不推测缺失交易日。"""
from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


def normalize_sessions(sessions: Iterable) -> pd.DatetimeIndex:
    """返回已排序、去重、无时区的交易日。"""
    values = pd.to_datetime(list(sessions), errors='raise')
    if getattr(values, 'tz', None) is not None:
        values = values.tz_convert('UTC').tz_localize(None)
    return pd.DatetimeIndex(values).normalize().drop_duplicates().sort_values()


def month_end_sessions(sessions: Iterable) -> pd.DatetimeIndex:
    """从实际交易日中选择每个自然月的最后一日。"""
    calendar = normalize_sessions(sessions)
    if calendar.empty:
        return calendar
    frame = pd.DataFrame({'session': calendar})
    return pd.DatetimeIndex(frame.groupby(frame.session.dt.to_period('M')).session.max())


def next_session(sessions: Iterable, decision_session) -> pd.Timestamp | None:
    """返回严格晚于决策日的首个实际交易日；数据结束时返回 None。"""
    calendar = normalize_sessions(sessions)
    decision = pd.Timestamp(decision_session)
    if decision.tzinfo is not None:
        decision = decision.tz_convert('UTC').tz_localize(None)
    decision = decision.normalize()
    position = calendar.searchsorted(decision, side='right')
    return calendar[position] if position < len(calendar) else None

