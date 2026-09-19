#!/usr/bin/env python3
"""人工补跑某个**已耗尽或缺失**的影子日作业（照 `protocol_review --retry` 的先例）。

**为什么需要它**：`ShadowJobs.claim` 在 `attempt >= max_attempts` 后恒返回 None ⇒
失败的 session **永久不再补**。而"三次重试全部撞上同一个瞬时故障"完全可能 ——
2026-09-19 发现 09-14 / 09-18 的多个作业就是这样永久停住的，
且在此之前**没有任何地方会说出这件事**（见 `shadow_job_health.py`）。

`force` 只放开「重试已耗尽」与退避两条闸，**不放开「已经成功过」** ——
重跑一个成功过的作业会重复写事件，那不是补跑、是制造重复。

用法：
    python3 scripts/live_trading/retry_shadow_job.py --job selection_outcomes --session 2026-09-18
    python3 scripts/live_trading/retry_shadow_job.py --job daily_setup_shadow --session 2026-09-18
    python3 scripts/live_trading/retry_shadow_job.py --list-gaps --days 10   # 先看有哪些

退出码：0 = 领取并成功；1 = 未能领取（已成功过/正在跑）或跑失败。
"""
import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))


def current_session(now=None):
    """三个 runner **现在**会算的那个 session：纽约时间下**收盘已过**的最新交易日。

    为什么需要它：`run_daily_selection.py` **没有 session 参数**、`run_daily_setups.py`
    的 `--as-of` **作业也不传**、`run_outcomes.py` 是全局结算 —— 三个 runner 都是
    **"现在"锚定**的，而作业的 claim 是**按 session 键**的。
    ⇒ 对**过去的 session** 补跑，只会算出"今天"的东西，然后把 claim 记成"那天成功"：
    **账本会说谎**。所以这里只允许补**当前 session**，不允许补历史。
    """
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    import pandas as pd
    from scripts.data.trading_calendar import sessions

    local = (now or datetime.now(ZoneInfo('UTC'))).astimezone(ZoneInfo('America/New_York'))
    dates = sessions(local.date() - timedelta(days=14), local.date()).session_date
    completed = [d for d in dates
                 if d.tz_localize('America/New_York') + pd.Timedelta(hours=16) <= local]
    return str(completed[-1].date()) if completed else None


def main(argv=None) -> int:
    from scripts.live_trading.outcome_scheduler import OutcomeSchedulerThread
    from scripts.live_trading.shadow_jobs import EXPECTED_JOBS

    parser = argparse.ArgumentParser(description='人工补跑影子日作业')
    parser.add_argument('--config', default=str(BASE_DIR / 'config.yaml'))
    parser.add_argument('--job', choices=EXPECTED_JOBS)
    parser.add_argument('--session', help='纽约日期，如 2026-09-18')
    parser.add_argument('--list-gaps', action='store_true', help='列出缺口后退出')
    parser.add_argument('--days', type=int, default=10, help='配合 --list-gaps')
    parser.add_argument('--max-attempts', type=int, default=3)
    args = parser.parse_args(argv)

    if args.list_gaps:
        from scripts.live_trading.shadow_job_health import main as health_main
        return health_main(['--config', args.config, '--days', str(args.days)])

    if not args.job or not args.session:
        parser.error('需要 --job 与 --session（或 --list-gaps）')

    # **只允许补当前 session**：见 `current_session` 的说明 —— 补历史会写出一份
    # "用今天的活儿冒充那天"的账目，那比留着缺口更糟。
    current = current_session()
    if args.session != current:
        print(f'❌ 拒绝：--session {args.session} 不是当前 session（{current}）。\n'
              f'   三个 runner（selection / setups / outcomes）都是"现在"锚定的，\n'
              f'   对历史 session 补跑只会算出今天的东西、却把那天记成"成功" —— 账本会说谎。\n'
              f'   历史缺口只能用当时的输入重算，本工具做不到。')
        return 1

    # 复用服务那条路径的 runner：**不另写一套子进程调用** ——
    # 两套调用一旦不同，"补跑"与"正常跑"的结果就不是同一件事了。
    sched = OutcomeSchedulerThread(
        __import__('yaml').safe_load(Path(args.config).read_text(encoding='utf-8')) or {},
        args.config)
    runners = {
        'selection_and_reconcile': lambda: sched._selection(args.session),
        'daily_setup_shadow': sched._run_setups,
        'selection_outcomes': sched._run,
    }
    ran = sched.jobs.execute(args.job, args.session, runners[args.job],
                             max_attempts=args.max_attempts, force=True)
    if not ran:
        print(f'未领取：{args.job} @ {args.session} 已成功过、或仍在跑 '
              f'（`force` 刻意不放开这两种情形）')
        return 1
    ok = sched.jobs.succeeded(args.job, args.session)
    print(('✅ 补跑成功' if ok else '❌ 补跑仍失败') + f'：{args.job} @ {args.session}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
