#!/usr/bin/env python3
"""导入历史证券主数据 v2（+ ticker 历史、公司行动），输出可审计表与冲突表。

技术设计 §2.3 / §6。真实来源由适配器产出原始文件后归档；测试使用本地 fixture。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.security_master_v2 import build_outputs
from scripts.data.source_archive import archive_source


def parse_sources(specs, run_id, archive_root, dry_run):
    """把 'SOURCE_ID=PATH' 列表归档，返回按优先级排列的来源清单。"""
    sources = []
    for spec in specs:
        source_id, _, raw = spec.partition('=')
        if not raw:
            source_id, raw = Path(source_id).name, source_id
        raw = Path(raw)
        if dry_run:
            sources.append({'source_id': source_id, 'archive_dir': raw})
            continue
        archived = archive_source(raw, source_id, run_id, archive_root)
        sources.append({'source_id': source_id, 'archive_dir': archived})
    return sources


def main():
    p = argparse.ArgumentParser(description='导入历史证券主数据 v2')
    p.add_argument('--source-archive', nargs='+', required=True,
                   help='原始来源，格式 SOURCE_ID=PATH（可多个，顺序即优先级）')
    p.add_argument('--run-id', default='RUN-001', help='来源归档 run-id（不可覆盖）')
    p.add_argument('--archive-root', default='data/source_archive')
    p.add_argument('--output-dir', required=True)
    p.add_argument('--tables', default='master,symbols,actions',
                   help='要产出的表：master,symbols,actions 的子集')
    p.add_argument('--dry-run', action='store_true', help='只校验不写盘')
    args = p.parse_args()
    tables = tuple(t.strip() for t in args.tables.split(',') if t.strip())
    sources = parse_sources(args.source_archive, args.run_id, args.archive_root, args.dry_run)
    if args.dry_run:
        print(json.dumps({'dry_run': True, 'sources': [s['source_id'] for s in sources],
                          'tables': list(tables)}, ensure_ascii=False))
        return 0
    summary = build_outputs(sources, args.output_dir, tables=tables)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
