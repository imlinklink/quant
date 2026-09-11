#!/usr/bin/env python3
"""合并一个或多个 security master 文件并输出统一、可审计的证券主表。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.data.io_utils import read_frame, write_frame

REQUIRED = {'code', 'listing_date', 'delisting_date', 'asset_type'}
ALLOWED_TYPES = {'stock', 'etf', 'leveraged_etf'}


def normalize_code(value):
    code = str(value).strip().upper()
    return code if '.' in code else f'US.{code}'


def build_master(frames, source_names=None):
    rows = []
    source_names = source_names or [f'source_{i}' for i in range(len(frames))]
    for frame, source in zip(frames, source_names):
        missing = REQUIRED - set(frame.columns)
        if missing:
            raise ValueError(f'{source} 缺字段: {sorted(missing)}')
        item = frame.copy()
        item['code'] = item['code'].map(normalize_code)
        item['listing_date'] = pd.to_datetime(item['listing_date'], errors='coerce').dt.date
        item['delisting_date'] = pd.to_datetime(item['delisting_date'], errors='coerce').dt.date
        item['asset_type'] = item['asset_type'].astype(str).str.lower().str.strip()
        item['_source'] = source
        rows.append(item)
    data = pd.concat(rows, ignore_index=True)
    bad_types = sorted(set(data.asset_type) - ALLOWED_TYPES)
    if bad_types:
        raise ValueError(f'非法 asset_type: {bad_types}')
    if data.listing_date.isna().any():
        raise ValueError('listing_date 存在空值或非法日期')
    conflicts = data.groupby('code').agg(listings=('listing_date', 'nunique'),
                                         types=('asset_type', 'nunique'))
    conflicts = conflicts[(conflicts.listings > 1) | (conflicts.types > 1)]
    if not conflicts.empty:
        raise ValueError('security master 来源冲突: ' + ','.join(conflicts.index[:20]))
    # 后输入可补充前输入字段，但核心字段已经过冲突检查。
    data = data.sort_values(['code', '_source']).groupby('code', as_index=False).last()
    if ((pd.to_datetime(data.delisting_date) < pd.to_datetime(data.listing_date)) &
            data.delisting_date.notna()).any():
        raise ValueError('存在退市日期早于上市日期')
    data['source'] = data.pop('_source')
    return data.sort_values('code').reset_index(drop=True)


def main():
    parser = argparse.ArgumentParser(description='构建统一 security_master')
    parser.add_argument('--input', nargs='+', required=True); parser.add_argument('--output', required=True)
    args = parser.parse_args()
    out = build_master([read_frame(p) for p in args.input], [Path(p).name for p in args.input])
    write_frame(out, args.output)
    print(f'wrote {len(out)} securities to {args.output}')


if __name__ == '__main__':
    main()
