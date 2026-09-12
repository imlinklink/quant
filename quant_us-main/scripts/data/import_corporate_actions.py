#!/usr/bin/env python3
"""导入公司行动（corporate_actions）：拆股/反向拆股/股息/并购/分拆/退市结算。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.import_security_master_v2 import parse_sources
from scripts.data.security_master_v2 import build_outputs


def main():
    p = argparse.ArgumentParser(description='导入公司行动 corporate_actions')
    p.add_argument('--source-archive', nargs='+', required=True,
                   help='原始来源，格式 SOURCE_ID=PATH（可多个，顺序即优先级）')
    p.add_argument('--run-id', default='RUN-001')
    p.add_argument('--archive-root', default='data/source_archive')
    p.add_argument('--output-dir', required=True)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    sources = parse_sources(args.source_archive, args.run_id, args.archive_root, args.dry_run)
    if args.dry_run:
        print(json.dumps({'dry_run': True, 'table': 'actions'}, ensure_ascii=False))
        return 0
    summary = build_outputs(sources, args.output_dir, tables=('actions',))
    print(json.dumps({'action_rows': summary.get('action_rows')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
