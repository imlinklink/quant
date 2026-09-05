#!/usr/bin/env python3
"""候选结果回填（反事实跟踪）。

每个交易日收盘后运行一次：找出账本里所有"信号提案"（含被你拒绝的、
LLM block 的、还没处理的），用富途日K回填它们之后
第 1/3/5/10 个交易日的收盘表现，写入 candidate_outcome 事件。

周报（weekly_report.py）会据此输出"拒绝 vs 接受 / LLM 分组"的事后对比。

用法：
    python scripts/live_trading/decision_ledger/backfill_outcomes.py            # 回填近30天信号
    python scripts/live_trading/decision_ledger/backfill_outcomes.py --days 90
    python scripts/live_trading/decision_ledger/backfill_outcomes.py --dry      # 只看计划不下数据

说明：可重复运行，已回填的 (proposal_id, offset) 会自动跳过。
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional

import yaml

BASE_DIR = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BASE_DIR))

from scripts.live_trading.decision_ledger import ledger

OFFSETS = (1, 3, 5, 10)


def _kline_fetch_factory(cfg: dict) -> Callable[[str, str, str], list]:
    """创建真实富途日K拉取函数，返回 [{date, close, volume}]。"""

    def fetch(code: str, start: str, end: str) -> list:
        from futu import OpenQuoteContext, KLType, RET_OK
        futu_cfg = (
            cfg.get('futu')
            or (cfg.get('hk') or {}).get('futu')
            or (cfg.get('trading') or {}).get('futu')
            or {}
        )
        with OpenQuoteContext(
            host=str(futu_cfg.get('host', '127.0.0.1')),
            port=int(futu_cfg.get('port', 11111)),
        ) as ctx:
            ret, data, _ = ctx.request_history_kline(
                code=code,
                start=start,
                end=end,
                ktype=KLType.K_DAY,
                extended_time=False,
                max_count=120,
            )
        if ret != RET_OK or data is None or len(data) == 0:
            return []
        rows = []
        for _, r in data.iterrows():
            try:
                rows.append({
                    'date': str(r['time_key'])[:10],
                    'close': float(r['close']),
                    'volume': float(r['volume']),
                })
            except Exception:
                continue
        return rows

    return fetch


def _proposal_tasks(events: list, days: int) -> tuple:
    """返回 (proposals 列表, 已回填集合)。proposal 事件含 proposal_id/code/ts。"""
    proposals = {}
    for e in events:
        if e.get('event_type') != 'proposal_created':
            continue
        pid = e.get('proposal_id')
        if not pid:
            continue
        proposals.setdefault(pid, e)

    cutoff = datetime.now() - timedelta(days=days)
    tasks = []
    for pid, p in proposals.items():
        try:
            ts = datetime.fromisoformat(str(p.get('ts', '')))
        except Exception:
            continue
        if ts < cutoff:
            continue
        code = p.get('stock_code')
        if not code:
            continue
        tasks.append({'proposal_id': pid, 'code': code, 'ts': ts, 'proposal': p})

    done = set()
    for e in events:
        if e.get('event_type') == 'candidate_outcome':
            try:
                done.add((e.get('proposal_id'), int(e.get('offset_days'))))
            except (TypeError, ValueError):
                pass
    return tasks, done


def backfill(events: list, fetch_kline: Callable, days: int = 30) -> int:
    """回填主流程；返回新增的 candidate_outcome 条数。"""
    tasks, done = _proposal_tasks(events, days)
    if not tasks:
        return 0

    # 按代码聚合并确定拉取区间
    by_code = defaultdict(list)
    for t in tasks:
        by_code[t['code']].append(t)

    recorded = 0
    for code, items in by_code.items():
        earliest = min(t['ts'] for t in items) - timedelta(days=5)
        latest = max(t['ts'] for t in items) + timedelta(days=25)
        rows = fetch_kline(
            code,
            earliest.strftime('%Y-%m-%d'),
            latest.strftime('%Y-%m-%d'),
        )
        if not rows:
            continue
        # 过滤零成交（停牌/假日），保证 offset 按实际交易日计
        rows = [r for r in rows if r.get('volume', 0) > 0]
        dates = [r['date'] for r in rows]
        closes = [r['close'] for r in rows]

        for t in items:
            pid = t['proposal_id']
            sig_date = t['ts'].strftime('%Y-%m-%d')
            # 锚点 = 信号日当天或之后第一个交易日
            anchor = -1
            for i, d in enumerate(dates):
                if d >= sig_date:
                    anchor = i
                    break
            if anchor < 0 or anchor >= len(closes):
                continue
            for off in OFFSETS:
                if (pid, off) in done:
                    continue
                idx = anchor + off
                if idx >= len(closes):
                    continue
                base = closes[anchor]
                if base <= 0:
                    continue
                ret = closes[idx] / base - 1
                ledger.record(
                    'candidate_outcome',
                    proposal_id=pid,
                    stock_code=code,
                    signal_date=sig_date,
                    signal_price=t['proposal'].get('price'),
                    offset_days=off,
                    outcome_date=dates[idx],
                    close_price=closes[idx],
                    ret_pct=ret,
                )
                recorded += 1
    return recorded


def main():
    parser = argparse.ArgumentParser(description='候选结果回填（反事实跟踪）')
    parser.add_argument('--days', type=int, default=30, help='回看多少天内的信号')
    parser.add_argument('--dry', action='store_true', help='只打印计划，不拉数据')
    args = parser.parse_args()

    events = ledger.load_events(days=args.days)
    tasks, done = _proposal_tasks(events, args.days)
    print(f'信号提案: {len(tasks)} 个 | 已回填组合: {len(done)} 个')
    if not tasks:
        print('没有需要回填的信号（先让系统跑出信号/确认单）')
        return 0

    if args.dry:
        for t in tasks:
            missing = [o for o in OFFSETS if (t['proposal_id'], o) not in done]
            print(f"  - {t['code']} ({t['ts'].strftime('%Y-%m-%d')}) 待回填: {missing}")
        return 0

    config_path = BASE_DIR / 'config.yaml'
    cfg = yaml.safe_load(config_path.read_text(encoding='utf-8')) or {}
    fetch = _kline_fetch_factory(cfg)
    n = backfill(events, fetch, days=args.days)
    print(f'回填完成: 新增 {n} 条 candidate_outcome')
    return 0


if __name__ == '__main__':
    sys.exit(main())
