#!/usr/bin/env python3
"""导入历史证据到不可变快照（技术设计 §3.2/§3.3）。只读来源、不联网。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.io_utils import read_frame
from scripts.evidence.evidence_store import audit_source, normalize_evidence, validate_evidence


def main():
    p = argparse.ArgumentParser(description='导入历史证据 -> evidence.jsonl 快照')
    p.add_argument('--source', required=True, help='含 evidence.csv 的来源目录')
    p.add_argument('--output-dir', required=True)
    p.add_argument('--ingested-at', default=None)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    path = Path(args.source) / 'evidence.csv'
    if not path.is_file():
        raise SystemExit(f'未找到证据文件: {path}')
    records = normalize_evidence(read_frame(path), ingested_at=args.ingested_at)
    errors = validate_evidence(records)
    if errors:
        raise SystemExit('证据校验失败: ' + ','.join(errors))
    summary = {'rows': int(len(records)), 'source_quality': audit_source(records)}
    if args.dry_run:
        print(json.dumps({'dry_run': True, **summary}, ensure_ascii=False))
        return 0
    out = Path(args.output_dir)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'输出目录非空，禁止覆盖: {out}')
    out.mkdir(parents=True, exist_ok=True)
    with (out / 'evidence.jsonl').open('w', encoding='utf-8') as fh:
        for record in records.to_dict('records'):
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n')
    (out / 'source_quality.json').write_text(
        json.dumps(summary['source_quality'], ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
