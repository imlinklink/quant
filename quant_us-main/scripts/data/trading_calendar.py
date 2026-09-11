#!/usr/bin/env python3
"""美股历史 session 日历。正式实验优先从 SPY 实际日线构建。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from pandas.tseries.holiday import (
    AbstractHolidayCalendar, GoodFriday, Holiday, USLaborDay, USMartinLutherKingJr,
    USMemorialDay, USThanksgivingDay, nearest_workday,
)
from pandas.tseries.offsets import CustomBusinessDay, DateOffset
from dateutil.relativedelta import MO

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.data.io_utils import read_frame, write_frame


class NyseRuleCalendar(AbstractHolidayCalendar):
    """常规 NYSE 假日；临时休市必须由 reference 日线覆盖。"""
    rules = [
        Holiday('NewYear', month=1, day=1, observance=nearest_workday),
        USMartinLutherKingJr,
        Holiday('PresidentsDay', month=2, day=1,
                offset=DateOffset(weekday=MO(3))),
        GoodFriday,
        USMemorialDay,
        Holiday('Juneteenth', month=6, day=19, start_date='2022-01-01',
                observance=nearest_workday),
        Holiday('IndependenceDay', month=7, day=4, observance=nearest_workday),
        USLaborDay,
        USThanksgivingDay,
        Holiday('Christmas', month=12, day=25, observance=nearest_workday),
    ]


def _rule_sessions(start, end):
    days = pd.date_range(start, end, freq=CustomBusinessDay(calendar=NyseRuleCalendar()))
    return pd.DatetimeIndex(days)


def sessions(start, end, reference_daily=None) -> pd.DataFrame:
    """返回 session_date/close_time_et/source；reference 必须是实际交易日数据。"""
    start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    if reference_daily:
        frame = read_frame(reference_daily)
        date_col = 'date' if 'date' in frame else 'time_key'
        dates = pd.to_datetime(frame[date_col], errors='coerce').dt.normalize()
        dates = dates[(dates >= start) & (dates <= end)].dropna().drop_duplicates().sort_values()
        if dates.empty:
            raise ValueError('reference_daily 在指定区间没有 session')
        return pd.DataFrame({'session_date': dates, 'close_time_et': '16:00',
                             'source': 'reference_daily'})
    dates = _rule_sessions(start, end)
    return pd.DataFrame({'session_date': dates, 'close_time_et': '16:00',
                         'source': 'rule_fallback'})


def main():
    parser = argparse.ArgumentParser(description='生成美股 session 日历')
    parser.add_argument('--start', required=True); parser.add_argument('--end', required=True)
    parser.add_argument('--reference-daily'); parser.add_argument('--output', required=True)
    args = parser.parse_args()
    out = sessions(args.start, args.end, args.reference_daily)
    write_frame(out, args.output)
    print(f'wrote {len(out)} sessions to {args.output}; source={out.source.iloc[0]}')


if __name__ == '__main__':
    main()
