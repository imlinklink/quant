"""输入质量统计（任务 E.1）：统计真实提案最常缺的资料，按缺口补数据。

只统计缺口分布，不靠提示词要求模型更积极；补齐应来自独立来源与取得时间，
而不是改写提示词减少「资料不足」。
"""
import argparse
import json
from collections import Counter
from pathlib import Path


def build_input_quality(events):
    """从事件账本统计 LLM 评估的输入质量缺口。"""
    reviews = [e for e in events
               if e['event_type'] in ('llm_completed', 'llm_failed')]
    status_counter = Counter()
    missing_counter = Counter()
    latency = []
    for e in reviews:
        p = e.get('payload') or {}
        status_counter[p.get('status', 'unknown')] += 1
        for item in (p.get('missing_information') or []):
            missing_counter[str(item)] += 1
        if isinstance(p.get('latency_seconds'), (int, float)):
            latency.append(float(p['latency_seconds']))

    total = len(reviews)
    return {
        'total_reviews': total,
        'status_distribution': dict(status_counter),
        # 资料不足占比 = insufficient_information / 全部评估
        'insufficient_rate': round(status_counter.get('insufficient_information', 0) / total, 4) if total else None,
        'failed_rate': round(status_counter.get('failed', 0) / total, 4) if total else None,
        'stale_rate': round(status_counter.get('stale', 0) / total, 4) if total else None,
        'missing_information': dict(missing_counter.most_common()),
        'missing_rate': {k: round(v / total, 4) for k, v in missing_counter.items()} if total else {},
        'latency': {'samples': len(latency),
                    'mean': round(sum(latency) / len(latency), 2) if latency else None,
                    'max': round(max(latency), 2) if latency else None} if latency else {'samples': 0, 'mean': None, 'max': None},
    }


def main():
    parser = argparse.ArgumentParser(description='输入质量统计（LLM 评估资料缺口）')
    parser.add_argument('--events', required=True, help='events-v1.jsonl 导出')
    parser.add_argument('--output')
    args = parser.parse_args()
    events = [json.loads(line) for line in Path(args.events).read_text(encoding='utf-8').splitlines()
              if line.strip()]
    result = json.dumps(build_input_quality(events), ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(result + '\n', encoding='utf-8')
    else:
        print(result)


if __name__ == '__main__':
    main()
