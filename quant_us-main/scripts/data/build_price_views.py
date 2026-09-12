#!/usr/bin/env python3
"""构建原始可交易价与 as-of 特征价两条视图，并输出终局结算检查（技术设计 §2.4）。"""
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
from scripts.data.price_views import build_price_views, terminal_outcome_flags


def main():
    p = argparse.ArgumentParser(description='构建原始价/as-of 特征价双视图')
    p.add_argument('--bars', required=True, help='原始日线（需含 security_id 或经 --id-column 指定）')
    p.add_argument('--actions', help='公司行动表 corporate_actions.csv')
    p.add_argument('--master', help='证券主数据（用于终局结算检查）')
    p.add_argument('--as-of', help='特征价 as-of 日期（YYYY-MM-DD）；缺省则用全部已登记行动')
    p.add_argument('--id-column', default='security_id', help='把该列重命名为 security_id')
    p.add_argument('--output-dir', required=True)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    bars = read_frame(args.bars)
    if args.id_column != 'security_id' and args.id_column in bars.columns:
        bars = bars.rename(columns={args.id_column: 'security_id'})
    if 'security_id' not in bars.columns:
        raise SystemExit('日线缺 security_id（可用 --id-column 指定来源列名）')
    if 'session' not in bars.columns and 'date' in bars.columns:
        bars = bars.rename(columns={'date': 'session'})
    actions = read_frame(args.actions) if args.actions and Path(args.actions).is_file() else None
    if args.dry_run:
        print(json.dumps({'dry_run': True, 'bars': len(bars)}, ensure_ascii=False))
        return 0

    out = Path(args.output_dir)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'输出目录非空，禁止覆盖: {out}')
    out.mkdir(parents=True, exist_ok=True)
    views = build_price_views(bars, actions, as_of=args.as_of)
    write_frame(views, out / 'price_views.csv.gz')
    summary = {'rows': int(len(views)), 'as_of': args.as_of,
               'price_basis': sorted(views['price_basis'].unique().tolist())}
    if args.master and Path(args.master).is_file():
        flags = terminal_outcome_flags(bars, read_frame(args.master), actions)
        write_frame(flags, out / 'terminal_outcome.csv')
        summary['terminal_outcome_unknown'] = int(flags['problems'].str.contains(
            'terminal_outcome_unknown', na=False).sum())
    (out / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
