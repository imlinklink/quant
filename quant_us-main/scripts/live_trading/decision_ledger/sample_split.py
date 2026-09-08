"""样本外划分（任务 E.5）：按日期划分训练/验证/锁定测试期。

同日或重叠持仓不能当独立样本随机打散：按 signal 的持仓期分组，
同一 signal 的所有事件（含 fill/exit）跟随 signal 的分期，不跨分。
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def _parse_boundary(value):
    """解析边界日期。允许纯日期（'YYYY-MM-DD'，按 UTC 当天 00:00）或带时区时间。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        raise ValueError(f'无法解析日期: {value}')
    if dt.tzinfo is None or dt.utcoffset() is None:
        return dt.replace(tzinfo=timezone.utc)  # 纯日期 → UTC 当天
    return dt.astimezone(timezone.utc)


def _parse_timestamp(value):
    """解析事件时间戳，必须带时区；无时区时间直接拒绝，不默认为 UTC。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f'时间必须带时区: {value}')
        return value.astimezone(timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        raise ValueError(f'无法解析日期: {value}')
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f'时间必须带时区: {value}')
    return dt.astimezone(timezone.utc)


def split_signals(events, train_end, val_end, test_end=None):
    """按「信号时间 + 退出时间」划分 signal_id 到 train/val/test。

    标签窗口 = [信号时间, 退出时间]。跨边界的持仓（信号在 train 但退出在 val/test）
    单独放进 excluded，不能把测试期收益混进训练标签；持仓未了结（无退出）也进 excluded。
    拒绝无时区时间和乱序边界。

    返回 {train:[], val:[], test:[], excluded:[], unassigned:[]}。
    本函数只做「按信号时间分桶」，不宣称完成「重叠持仓不跨分」——跨界的明确排除。
    """
    train_end = _parse_boundary(train_end)
    val_end = _parse_boundary(val_end)
    test_end = _parse_boundary(test_end) if test_end else None
    if not (train_end < val_end):
        raise ValueError('train_end 必须早于 val_end')
    if test_end is not None and not (val_end < test_end):
        raise ValueError('val_end 必须早于 test_end')

    signal_date = {}
    signal_exit = {}
    for e in events:
        sid = e.get('signal_id')
        if not sid:
            continue
        if e['event_type'] in ('rule_candidate', 'rule_rejected'):
            bar = (e.get('payload') or {}).get('signal_bar_end')
            if bar:
                signal_date.setdefault(sid, str(bar))
        # 退出时间：卖出成交或交易关闭事件的 observed_at（取最晚）
        if e['event_type'] in ('fill_received', 'trade_closed'):
            p = e.get('payload') or {}
            is_exit = (e['event_type'] == 'trade_closed'
                       or (e['event_type'] == 'fill_received' and p.get('side') == 'sell'))
            if is_exit and e.get('observed_at'):
                cur = signal_exit.get(sid, '')
                if str(e['observed_at']) > cur:
                    signal_exit[sid] = str(e['observed_at'])

    buckets = {'train': [], 'val': [], 'test': [], 'excluded': [], 'unassigned': []}
    for sid, bar in sorted(signal_date.items()):
        try:
            dt = _parse_timestamp(bar)
        except ValueError:
            buckets['unassigned'].append(sid)
            continue
        exit_at = signal_exit.get(sid)
        try:
            exit_dt = _parse_timestamp(exit_at) if exit_at else None
        except ValueError:
            buckets['unassigned'].append(sid)
            continue

        if exit_dt is not None:
            # 有明确退出：标签窗口完整，检查是否跨边界
            if dt < train_end:
                buckets['excluded' if exit_dt >= train_end else 'train'].append(sid)
            elif dt < val_end:
                buckets['excluded' if exit_dt >= val_end else 'val'].append(sid)
            elif test_end is None or dt < test_end:
                buckets['test'].append(sid)
            else:
                buckets['unassigned'].append(sid)
        else:
            # 无退出（持仓中）：收益标签尚未实现，按信号时间分桶
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
