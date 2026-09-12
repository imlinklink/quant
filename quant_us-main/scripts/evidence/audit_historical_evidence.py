#!/usr/bin/env python3
"""来源可用性审计（§3.3）：未通过的来源不能进入严格历史证据包。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evidence.evidence_store import audit_source, validate_evidence

BLOCKING = ('timezone_explicit', 'retention_allowed')


def main():
    p = argparse.ArgumentParser(description='审计历史证据来源可用性')
    p.add_argument('--evidence', required=True, help='evidence.jsonl')
    p.add_argument('--sample', type=int, default=100)
    p.add_argument('--output-dir', required=True)
    args = p.parse_args()
    records = pd.read_json(args.evidence, lines=True)
    errors = validate_evidence(records)
    report = audit_source(records, sample=args.sample)
    report['validate_errors'] = errors
    report['passed'] = (not errors) and all(report.get(k) for k in BLOCKING)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'source_quality.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
