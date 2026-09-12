#!/usr/bin/env python3
"""历史 LLM 标签作业的**离线**骨架（§3.5）：只接受已冻结 packet，不联网、不调用模型。

- 不给 `--labels`：只产出待标注清单 `job.json`（每个 setup 的 packet_id/packet_hash），
  不生成任何标签——标签必须由外部、可审计的作业产出。
- 给 `--labels`：校验标签一对一属于 packet、引用 ID 在 packet 内，并报告覆盖率（含缺失分母）。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evidence.evidence_store import validate_labels


def _load_packets(packets_dir):
    packets = {}
    for path in sorted(Path(packets_dir).glob('*.json')):
        packet = json.loads(path.read_text(encoding='utf-8'))
        packets[path.stem] = packet
    return packets


def main():
    p = argparse.ArgumentParser(description='历史选股标签的回放校验（离线，无模型调用）')
    p.add_argument('--packets', required=True, help='build_historical_packet 的输出目录')
    p.add_argument('--labels', help='外部产出的标签 JSONL；缺省只产出待标注清单')
    p.add_argument('--mode', choices=['strict', 'diagnostic'], default='strict')
    p.add_argument('--output-dir', required=True)
    args = p.parse_args()
    packets = _load_packets(Path(args.packets) / 'packets')
    if not packets:
        raise SystemExit('未找到冻结 packet')
    out = Path(args.output_dir)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'输出目录非空，禁止覆盖: {out}')
    out.mkdir(parents=True, exist_ok=True)

    job = [{'setup_id': sid, 'packet_hash': pkt['packet_hash'],
            'decision_cutoff': pkt['decision_cutoff']} for sid, pkt in sorted(packets.items())]
    (out / 'job.json').write_text(json.dumps(job, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    if not args.labels:
        print(json.dumps({'mode': args.mode, 'to_label': len(job),
                          'note': '未提供标签；本作业不调用模型，不生成标签'}, ensure_ascii=False))
        return 0

    labels = [json.loads(line) for line in Path(args.labels).read_text(encoding='utf-8').splitlines()
              if line.strip()]
    errors = validate_labels(labels, packets)
    decisions = Counter(str(l.get('llm_decision')) for l in labels)
    coverage = {
        'packets': len(packets),
        'labels': len(labels),
        'labeled_setup_ids': len({str(l.get('setup_id')) for l in labels}),
        'coverage': round(len({str(l.get('setup_id')) for l in labels}) / len(packets), 4),
        'decision_counts': dict(decisions),
        'validation_errors': len(errors),
    }
    (out / 'coverage.json').write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + '\n',
                                       encoding='utf-8')
    (out / 'validation_errors.json').write_text(
        json.dumps(errors, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(coverage, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == '__main__':
    raise SystemExit(main())
