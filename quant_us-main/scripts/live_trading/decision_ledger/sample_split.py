"""样本外划分（任务 E.5）：按日期划分训练/验证/锁定测试期。

同日或重叠持仓不能当独立样本随机打散：按 signal 的持仓期分组，
同一 signal 的所有事件（含 fill/exit）跟随 signal 的分期，不跨分。
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def _parse_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise ValueError(f'无法解析日期: {value}')


def split_signals(events, train_end, val_end, test_end=None):
    """按 signal_bar_end 划分 signal_id 到 train/val/test。

    划分依据用规则事件的 signal_bar_end（信号收盘时间，稳定不随后续事件漂移）。
    返回 {train:[], val:[], test:[], unassigned:[]}。
    """
    train_end = _parse_date(train_end)
    val_end = _parse_date(val_end)
    test_end = _parse_date(test_end) if test_end else None

    signal_date = {}
    for e in events:
        sid = e.get('signal_id')
        if not sid:
            continue
        if e['event_type'] in ('rule_candidate', 'rule_rejected'):
            bar = (e.get('payload') or {}).get('signal_bar_end')
            if bar:
                signal_date.setdefault(sid, bar)

    buckets = {'train': [], 'val': [], 'test': [], 'unassigned': []}
    for sid, bar in sorted(signal_date.items()):
        try:
            dt = datetime.fromisoformat(str(bar).replace('Z', '+00:00')).astimezone(timezone.utc)
        except (ValueError, TypeError):
            buckets['unassigned'].append(sid)
            continue
        if dt < train_end:
            buckets['train'].append(sid)
        elif dt < val_end:
            buckets['val'].append(sid)
        elif test_end is None or dt < test_end:
            buckets['test'].append(sid)
        else:
            buckets['unassigned'].append(sid)
    return buckets


def main():
    parser = argparse.ArgumentParser(description='样本外划分（训练/验证/锁定测试）')
    parser.add_argument('--events', required=True, help='events-v1.jsonl 导出')
    parser.add_argument('--train-end', required=True, help='训练期截止（含），ISO 日期')
    parser.add_argument('--val-end', required=True, help='验证期截止（含），ISO 日期')
    parser.add_argument('--test-end', default=None, help='锁定测试期截止（可选）')
    args = parser.parse_args()

    events = [json.loads(line) for line in Path(args.events).read_text(encoding='utf-8').splitlines()
              if line.strip()]
    result = split_signals(events, args.train_end, args.val_end, args.test_end)
    print(json.dumps({k: {'count': len(v), 'signals': v} for k, v in result.items()},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
