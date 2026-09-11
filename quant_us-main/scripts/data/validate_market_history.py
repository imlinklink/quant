#!/usr/bin/env python3
"""验证日线覆盖率、字段约束和上市区间，输出不可覆盖的质量报告。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.data.io_utils import read_frame, write_frame


def _ohlc_invalid(d):
    numeric=d[['open','high','low','close']].apply(pd.to_numeric,errors='coerce')
    return (numeric.isna().any(axis=1) | (numeric <= 0).any(axis=1) |
            (numeric.low > numeric[['open','close','high']].min(axis=1)) |
            (numeric.high < numeric[['open','close','low']].max(axis=1)))


def validate_daily(daily, master, calendar, minimum_coverage=.98):
    """按股票汇总质量，并给出**按年份**的失败区间，供下游精确传播。

    Returns 每只股票一行，含 `failed_years`（分号分隔的年份）。
    该列让流水线只把失败的年份标为 quality_fail，
    而不是把该股票的全历史一起标记（设计 §8.4「该股票区间」）。
    """
    required={'stock','date','open','high','low','close','volume'}
    if not required.issubset(daily): raise ValueError('日线缺字段: '+','.join(sorted(required-set(daily))))
    d=daily.copy();d['date']=pd.to_datetime(d.date).dt.normalize();d['invalid_ohlc']=_ohlc_invalid(d)
    d['invalid_volume']=pd.to_numeric(d.volume,errors='coerce').lt(0)|pd.to_numeric(d.volume,errors='coerce').isna()
    duplicate=d.duplicated(['stock','date'],keep=False)
    m=master.copy();m['listing_date']=pd.to_datetime(m.listing_date);m['delisting_date']=pd.to_datetime(m.delisting_date,errors='coerce')
    c=pd.DatetimeIndex(pd.to_datetime(calendar.session_date).dt.normalize().unique())
    rows=[]
    for code,meta in m.set_index('code').iterrows():
        expected=c[(c>=meta.listing_date)&(pd.isna(meta.delisting_date)|(c<=meta.delisting_date))]
        g=d[d.stock==code];actual=g.date.drop_duplicates()
        coverage=len(actual[actual.isin(expected)])/len(expected) if len(expected) else np.nan
        outside=int((~g.date.isin(expected)).sum())
        reasons=[]
        if np.isfinite(coverage) and coverage<minimum_coverage: reasons.append('COVERAGE_BELOW_THRESHOLD')
        if g.invalid_ohlc.any(): reasons.append('INVALID_OHLC')
        if g.invalid_volume.any(): reasons.append('INVALID_VOLUME')
        if duplicate[g.index].any(): reasons.append('DUPLICATE_BAR')
        if outside: reasons.append('OUTSIDE_LISTING_WINDOW')
        # 逐年份定位失败区间：只有真正出问题的年份才需要下游标记。
        failed_years=set()
        if reasons:
            exp_years=sorted({t.year for t in expected})
            for year in exp_years:
                exp_y=expected[expected.year==year]
                gy=g[g.date.dt.year==year]
                if len(gy)==0:
                    if len(exp_y): failed_years.add(year)   # 整年缺失
                    continue
                act_y=gy.date.drop_duplicates()
                cov_y=len(act_y[act_y.isin(exp_y)])/len(exp_y) if len(exp_y) else 1.0
                if cov_y<minimum_coverage or gy.invalid_ohlc.any() or gy.invalid_volume.any() \
                        or duplicate[gy.index].any():
                    failed_years.add(year)
            # 区间外的数据也要标记其所在年份
            out_rows=g[~g.date.isin(expected)]
            failed_years |= {t.year for t in out_rows.date if pd.notna(t)}
        rows.append({'stock':code,'kind':'day','expected_bars':len(expected),'actual_bars':len(actual),
            'coverage':coverage,'duplicate_bars':int(duplicate[g.index].sum()),
            'invalid_ohlc':int(g.invalid_ohlc.sum()),'invalid_volume':int(g.invalid_volume.sum()),
            'outside_listing_window':outside,'quality':'quality_fail' if reasons else 'good',
            'reasons':';'.join(reasons),
            'failed_years':';'.join(str(y) for y in sorted(failed_years))})
    return pd.DataFrame(rows)


def main():
    p=argparse.ArgumentParser(description='验证历史行情数据质量')
    p.add_argument('--master',required=True);p.add_argument('--calendar',required=True)
    p.add_argument('--daily',required=True)
    p.add_argument('--output-dir',required=True);args=p.parse_args()
    out=Path(args.output_dir)
    if out.exists() and any(out.iterdir()): raise FileExistsError(f'质量目录非空，禁止覆盖: {out}')
    master=read_frame(args.master);calendar=read_frame(args.calendar)
    day=validate_daily(read_frame(args.daily),master,calendar)
    write_frame(day,out/'daily_quality.csv')
    summary=day.copy()
    write_frame(summary,out/'coverage_summary.csv')
    print(f'wrote {len(summary)} quality rows to {out}')
    return 1 if (summary.quality=='quality_fail').any() else 0


if __name__=='__main__':raise SystemExit(main())
