#!/usr/bin/env python3
"""CLI：把 run 的行情产物重键为 security_id（技术设计 §2.5 主键迁移）。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.id_bridge import bridge_run


def main():
    p = argparse.ArgumentParser(description='symbol -> security_id 行情桥接')
    p.add_argument('--run-dir', required=True, help='含 daily.csv.gz 与 daily_liquidity.csv.gz 的 run 目录')
    p.add_argument('--symbols', required=True, help='symbol_history.csv')
    p.add_argument('--output-dir', required=True)
    args = p.parse_args()
    report = bridge_run(args.run_dir, args.symbols, args.output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
