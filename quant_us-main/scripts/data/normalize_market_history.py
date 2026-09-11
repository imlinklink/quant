#!/usr/bin/env python3
"""把 Futu 原始 K 线标准化为统一日线 schema。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.data.io_utils import read_frame, write_frame

def normalize(frame: pd.DataFrame, kind: str = 'day') -> pd.DataFrame:
    required = {'code', 'time_key', 'open', 'high', 'low', 'close', 'volume'}
    missing = required - set(frame)
    if missing: raise ValueError('原始行情缺字段: ' + ','.join(sorted(missing)))
    d = frame.copy().rename(columns={'code': 'stock'})
    for col in ('open', 'high', 'low', 'close', 'volume'):
        d[col] = pd.to_numeric(d[col], errors='coerce')
    if 'turnover' not in d:
        d['turnover'] = d['close'] * d['volume']
        d['turnover_method'] = 'close_x_volume'
    else:
        d['turnover'] = pd.to_numeric(d.turnover, errors='coerce')
        d['turnover_method'] = 'source'
    times = pd.to_datetime(d.time_key, errors='coerce')
    if kind == 'day':
        d['date'] = times.dt.normalize()
        keys = ['stock', 'date']
        columns = keys + ['open', 'high', 'low', 'close', 'volume', 'turnover',
                          'turnover_method']
    else:
        raise ValueError(f'不支持的周期: {kind}')
    for optional in ('downloaded_at', 'requested_start', 'requested_end'):
        if optional in d: columns.append(optional)
    return d[columns].drop_duplicates(keys, keep='last').sort_values(keys).reset_index(drop=True)


def collect_inputs(paths):
    files=[]
    for value in paths:
        path=Path(value)
        files.extend(sorted(path.rglob('*.csv*'))) if path.is_dir() else files.append(path)
    frames=[read_frame(p) for p in files]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def main():
    parser=argparse.ArgumentParser(description='标准化本地历史行情')
    parser.add_argument('--input', nargs='+', required=True)
    parser.add_argument('--output', required=True); args=parser.parse_args()
    out=normalize(collect_inputs(args.input), 'day')
    write_frame(out, args.output)
    print(f'wrote {len(out)} normalized rows to {args.output}')


if __name__ == '__main__': main()
