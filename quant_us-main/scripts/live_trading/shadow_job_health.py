#!/usr/bin/env python3
"""检查每交易日的三个影子作业（selection / setup / outcomes）是否**跑完**。

**为什么需要它**：`ShadowJobs.claim` 在 `attempt >= max_attempts` 后恒返回 None ⇒
**失败的 session 会永久停在那里**；而在此之前**没有任何地方会说出这件事**。
更隐蔽的一类是**完全没有记录** —— 服务当时没在跑就没有任何事件，
而"没有事件"不会出现在任何报表里（2026-09-16/17 就是这样）。

判定口径：某个交易日的某个作业**成功过**才算完成；`failed`（含重试耗尽）、
`running`（可能崩在中间）、以及**无记录**都算缺口。

时间窗用**纽约日期**（作业就是按 NY session 键的），且**不含今天** ——
今天的作业在美东 16:20 之后才跑，白天的"缺"是正常的，报了就是狼来了。

用法：
    python3 scripts/live_trading/shadow_job_health.py --days 7
    python3 scripts/live_trading/shadow_job_health.py --days 3 --json    # 看护调用
退出码：0 = 全部完成；1 = 有缺口。
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

import yaml  # noqa: E402

from scripts.data.trading_calendar import sessions as rule_sessions  # noqa: E402
from scripts.live_trading.decision_ledger.event_store import EventStore  # noqa: E402
from scripts.live_trading.position_registry import PositionRegistry  # noqa: E402
from scripts.live_trading.shadow_jobs import EXPECTED_JOBS, incomplete_jobs  # noqa: E402

NY = ZoneInfo('America/New_York')


def window_sessions(days: int, *, today=None):
    """近 `days` 个自然日里的交易日（纽约日期），**不含今天**。"""
    today = today or datetime.now(NY).date()
    start, end = today - timedelta(days=days), today - timedelta(days=1)
    cal = rule_sessions(datetime.combine(start, datetime.min.time()),
                        datetime.combine(end, datetime.min.time()))
    return [str(d)[:10] for d in cal['session_date']]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='影子日作业完整性检查')
    parser.add_argument('--config', default=str(BASE_DIR / 'config.yaml'))
    parser.add_argument('--days', type=int, default=7, help='回看多少个自然日（不含今天）')
    parser.add_argument('--scope', default=None, help='账本 namespace；缺省取配置的 account_scope')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    config = yaml.safe_load(Path(args.config).read_text(encoding='utf-8')) or {}
    scope = (args.scope
             or ((config.get('llm_decision') or {}).get('engine_v2') or {})
             .get('account_scope', 'DRY-RUN'))
    events = EventStore(PositionRegistry(namespace=scope))
    sessions = window_sessions(args.days)
    gaps = incomplete_jobs(events, sessions)
    done = len(sessions) * len(EXPECTED_JOBS) - len(gaps)

    if args.json:
        print(json.dumps({
            'scope': scope, 'days': args.days, 'sessions': sessions,
            'expected': len(sessions) * len(EXPECTED_JOBS), 'completed': done,
            'gaps': [{'session': s, 'job': j, 'status': st or 'NO_RECORD'}
                     for s, j, st in gaps]}, ensure_ascii=False))
        return 1 if gaps else 0

    if not sessions:
        print(f'近 {args.days} 天没有交易日（纽约日期），无可检查')
        return 0
    print(f'账本 {scope}｜近 {args.days} 天 {len(sessions)} 个交易日'
          f'｜{len(sessions) * len(EXPECTED_JOBS)} 个作业中完成 {done} 个')
    if not gaps:
        print('✅ 全部完成')
        return 0
    print(f'❌ {len(gaps)} 个缺口：')
    for session, job, status in gaps:
        print(f'   {session}  {job:<26} {status or "无记录（服务当时没在跑？）"}')
    print('\n补跑：python3 scripts/live_trading/retry_shadow_job.py --job <job> --session <session>')
    return 1


if __name__ == '__main__':
    sys.exit(main())
