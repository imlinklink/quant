#!/usr/bin/env python3
"""审计 v2 证券主数据：区间/来源冲突、退市结算缺口、ticker 映射缺失。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from scripts.data.io_utils import read_frame, write_frame
from scripts.data.security_master_v2 import (MASTER_COLUMNS, SYMBOL_COLUMNS,
                                             ACTION_COLUMNS, audit_master)


def _read_or_empty(path, columns):
    if path and Path(path).is_file():
        return read_frame(path)
    return pd.DataFrame(columns=list(columns))


def main():
    p = argparse.ArgumentParser(description='审计 v2 证券主数据')
    p.add_argument('--master', required=True)
    p.add_argument('--symbols')
    p.add_argument('--actions')
    p.add_argument('--output-dir', required=True)
    p.add_argument('--allow-missing', action='store_true',
                   help='仅诊断用；缺表时继续（正式审计禁止）')
    args = p.parse_args()
    master = _read_or_empty(args.master, MASTER_COLUMNS)
    if master.empty and not args.allow_missing:
        raise SystemExit(f'主数据为空或不存在: {args.master}')
    symbols = _read_or_empty(args.symbols, SYMBOL_COLUMNS)
    actions = _read_or_empty(args.actions, ACTION_COLUMNS)
    quality, summary = audit_master(master, symbols, actions)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_frame(quality, out / 'quality_by_security.csv', overwrite=True)
    (out / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    blocking = summary['master_errors'] + summary['symbol_errors']
    return 1 if blocking else 0


if __name__ == '__main__':
    raise SystemExit(main())
