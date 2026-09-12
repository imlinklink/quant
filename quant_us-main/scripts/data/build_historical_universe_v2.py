#!/usr/bin/env python3
"""构建历史时点 universe v2（按 security_id，技术设计 §2.5）。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.io_utils import read_frame, write_frame
from scripts.historical_universe import build_point_in_time_universe_v2, universe_reason_summary


def main():
    p = argparse.ArgumentParser(description='构建历史时点 universe v2')
    p.add_argument('--master', required=True, help='security_master_v2.csv')
    p.add_argument('--symbols', required=True, help='symbol_history.csv')
    p.add_argument('--liquidity', required=True, help='T-1 流动性（security_id,date,previous_raw_close,adv20_usd,liquidity_as_of）')
    p.add_argument('--calendar', required=True, help='交易日（含 session_date 列）')
    p.add_argument('--output-dir', required=True)
    p.add_argument('--min-price', type=float, default=5.0)
    p.add_argument('--min-dollar-volume', type=float, default=5_000_000.0)
    p.add_argument('--master-version', default='')
    p.add_argument('--price-version', default='')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    master = read_frame(args.master)
    symbols = read_frame(args.symbols)
    liquidity = read_frame(args.liquidity)
    calendar = read_frame(args.calendar)
    sessions = pd.to_datetime(calendar['session_date']).tolist()
    if args.dry_run:
        print(json.dumps({'dry_run': True, 'sessions': len(sessions),
                          'securities': int(master['security_id'].nunique())}, ensure_ascii=False))
        return 0

    out = Path(args.output_dir)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'输出目录非空，禁止覆盖: {out}')
    out.mkdir(parents=True, exist_ok=True)
    universe = build_point_in_time_universe_v2(
        master, symbols, liquidity, sessions, min_price=args.min_price,
        min_dollar_volume=args.min_dollar_volume,
        master_version=args.master_version, price_version=args.price_version)
    write_frame(universe, out / 'universe.csv.gz')
    summary = universe_reason_summary(universe)
    write_frame(summary, out / 'universe_reasons.csv')
    stats = {'rows': int(len(universe)), 'eligible': int(universe['eligible'].sum()),
             'main_sample': int((universe['eligible'] & universe['asset_type'].eq('stock')).sum()),
             'sessions': len(sessions)}
    (out / 'summary.json').write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
