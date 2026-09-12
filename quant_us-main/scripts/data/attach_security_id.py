#!/usr/bin/env python3
"""给任意 symbol 键的帧补 `security_id`（技术设计 §2.5 主键迁移）。

用于把 setups / universe 等既有产物迁移到 security_id 主键；映射不到或歧义的记录
单独输出，不静默丢弃。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.id_bridge import attach_security_id
from scripts.data.io_utils import read_frame, write_frame


def attach(frame, symbols, *, symbol_col, date_col):
    mapped, unmapped, ambiguous = attach_security_id(frame, symbols, symbol_col=symbol_col,
                                                     date_col=date_col)
    return mapped, unmapped, ambiguous


def main():
    p = argparse.ArgumentParser(description='给帧补 security_id')
    p.add_argument('--frame', required=True)
    p.add_argument('--symbols', required=True, help='symbol_history.csv')
    p.add_argument('--symbol-col', required=True, help="如 setups 用 stock、universe 用 code")
    p.add_argument('--date-col', required=True, help='如 setup_time / universe_date / session')
    p.add_argument('--output', required=True)
    p.add_argument('--allow-unmapped', action='store_true',
                   help='存在未映射时仍写出（默认报错，避免静默漏映射）')
    args = p.parse_args()
    frame = read_frame(args.frame)
    symbols = read_frame(args.symbols)
    mapped, unmapped, ambiguous = attach(frame, symbols, symbol_col=args.symbol_col,
                                         date_col=args.date_col)
    if (len(unmapped) or len(ambiguous)) and not args.allow_unmapped:
        raise SystemExit(f'存在未映射 {len(unmapped)} / 歧义 {len(ambiguous)} 行；'
                         f'用 --allow-unmapped 显式放行')
    write_frame(mapped, args.output, overwrite=False)
    print(json.dumps({'rows': int(len(frame)), 'mapped': int(len(mapped)),
                      'unmapped': int(len(unmapped)), 'ambiguous': int(len(ambiguous))},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
