#!/usr/bin/env python3
"""为每个 setup 构建冻结证据包与拒绝日志（§3.4）。不联网、不调用模型。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evidence.evidence_store import build_packet


def main():
    p = argparse.ArgumentParser(description='构建冻结历史证据包 packets/ + exclusions/')
    p.add_argument('--setups', required=True, help='含 setup_id/security_id/decision_cutoff')
    p.add_argument('--evidence', required=True, help='evidence.jsonl')
    p.add_argument('--source-version', default='')
    p.add_argument('--price-version', default='')
    p.add_argument('--allow-unproven-observed-at', action='store_true',
                   help='诊断层：允许缺 observed_at 的材料进入 packet（严格层禁止，默认关闭）')
    p.add_argument('--diagnostic', action='store_true',
                   help='诊断层：同时放宽 observed_at 与来源核验，仅用于覆盖率诊断，不得用于正式回放')
    p.add_argument('--output-dir', required=True)
    args = p.parse_args()
    setups = pd.read_csv(args.setups)
    for column in ('setup_id', 'security_id', 'decision_cutoff'):
        if column not in setups.columns:
            raise SystemExit(f'setups 缺字段: {column}')
    records = pd.read_json(args.evidence, lines=True)
    policy = {'require_observed_at': not (args.allow_unproven_observed_at or args.diagnostic),
              'require_verified': not args.diagnostic}

    out = Path(args.output_dir)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'输出目录非空，禁止覆盖: {out}')
    (out / 'packets').mkdir(parents=True, exist_ok=True)
    index, all_exclusions = {}, []
    for row in setups.to_dict('records'):
        packet, exclusions = build_packet(records, row['security_id'], row['decision_cutoff'],
                                          source_version=args.source_version,
                                          price_version=args.price_version, policy=policy)
        text = json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
        (out / 'packets' / f"{row['setup_id']}.json").write_text(text, encoding='utf-8')
        (out / 'packets' / f"{row['setup_id']}.sha256").write_text(
            hashlib.sha256(text.encode('utf-8')).hexdigest() + '\n', encoding='utf-8')
        index[str(row['setup_id'])] = packet['packet_hash']
        for item in exclusions:
            all_exclusions.append({'setup_id': row['setup_id'], **item})
    (out / 'index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n',
                                    encoding='utf-8')
    (out / 'exclusions.json').write_text(
        json.dumps(all_exclusions, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'packets': len(index), 'exclusions': len(all_exclusions)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
