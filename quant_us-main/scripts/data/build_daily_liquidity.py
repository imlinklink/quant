#!/usr/bin/env python3
"""从日线生成只使用前一交易日可知信息的每日流动性特征。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.data.io_utils import read_frame, write_frame


def build_liquidity(frame: pd.DataFrame, window=20, min_observations=15):
    code_col = 'stock' if 'stock' in frame else 'code'
    close_col = 'raw_close' if 'raw_close' in frame else 'close'
    required={code_col,'date',close_col,'volume'}
    if not required.issubset(frame): raise ValueError('日线缺字段: '+','.join(sorted(required-set(frame))))
    d=frame.copy().rename(columns={code_col:'code'});d['date']=pd.to_datetime(d.date).dt.normalize()
    d=d.sort_values(['code','date']).drop_duplicates(['code','date'])
    if 'turnover' in d:
        d['dollar_volume']=pd.to_numeric(d.turnover,errors='coerce')
    else:
        d['dollar_volume']=pd.to_numeric(d[close_col],errors='coerce')*pd.to_numeric(d.volume,errors='coerce')
    group=d.groupby('code',group_keys=False)
    d['previous_close']=group[close_col].shift(1)
    d['adv20']=group['dollar_volume'].transform(lambda s:s.rolling(window,min_periods=min_observations).mean().shift(1))
    d['median_dollar_volume_20d']=group['dollar_volume'].transform(lambda s:s.rolling(window,min_periods=min_observations).median().shift(1))
    d['valid_observations_20d']=group['dollar_volume'].transform(lambda s:s.notna().rolling(window).sum().shift(1))
    d['liquidity_as_of']=group['date'].shift(1)
    d['quality']=np.where(d[['previous_close','adv20','liquidity_as_of']].isna().any(axis=1),
                          'insufficient_history','good')
    return d[['date','code','previous_close','dollar_volume','adv20',
              'median_dollar_volume_20d','valid_observations_20d','liquidity_as_of','quality']]


def main():
    p=argparse.ArgumentParser(description='生成 T-1 可知的每日流动性')
    p.add_argument('--daily',required=True);p.add_argument('--output',required=True)
    p.add_argument('--window',type=int,default=20);p.add_argument('--min-observations',type=int,default=15)
    args=p.parse_args();out=build_liquidity(read_frame(args.daily),args.window,args.min_observations)
    write_frame(out,args.output);print(f'wrote {len(out)} liquidity rows to {args.output}')


if __name__=='__main__':main()
