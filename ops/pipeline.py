#!/usr/bin/env python3
"""每日运营流程管线（跨美股/港股两套系统）。

用法：
    python3 ops/pipeline.py --mode morning   # 盘前：简报(两端) + 选股建议 + 回填
    python3 ops/pipeline.py --mode evening   # 盘后：结果回填(两端)
    python3 ops/pipeline.py --mode weekly    # 周报（两端 + 合并）
    python3 ops/pipeline.py --mode all
    python3 ops/pipeline.py --mode morning --dry-run   # 只打印将执行的命令

建议 cron 时间：
    morning: 工作日 08:20（北京）
    evening: 工作日 17:10（港股收盘后；美股当日数据次日早上补全）
    weekly:  每周六 10:00
"""
import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # ops/ 的上一级 = quant 根目录
US = ROOT / 'quant_us-main'
HK = ROOT / 'quant_futu-main'
PY = sys.executable or 'python3'

CMD = {
    'market_brief': 'scripts/live_trading/run_market_brief.py',
    'suggestions': 'scripts/live_trading/llm_suggestions/run_suggestions.py',
    'backfill': 'scripts/live_trading/decision_ledger/backfill_outcomes.py',
    'weekly': 'scripts/live_trading/decision_ledger/weekly_report.py',
}


def plan() -> list:
    return [
        ('美股盘前简报', US, [CMD['market_brief']]),
        ('港股盘前简报', HK, [CMD['market_brief']]),
        ('美股+港股选股建议', US, [CMD['suggestions']]),
        ('美股结果回填', US, [CMD['backfill']]),
        ('港股结果回填', HK, [CMD['backfill']]),
    ]


def _run(name: str, cwd: Path, args: list):
    print(f"\n▶ [{datetime.now().strftime('%H:%M:%S')}] {name}")
    print(f"   cd {cwd} && python3 {' '.join(args)}")
    r = subprocess.run([PY, *args], cwd=str(cwd))
    if r.returncode != 0:
        raise SystemExit(f'❌ {name} 失败 (exit={r.returncode})')
    print(f'✅ {name} 完成')


def main():
    parser = argparse.ArgumentParser(description='每日运营流程管线')
    parser.add_argument('--mode', choices=['morning', 'evening', 'weekly', 'all'], default='all')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    tasks = []
    if args.mode in ('morning', 'all'):
        tasks = plan()
    if args.mode in ('evening', 'all'):
        tasks.append(('美股结果回填', US, [CMD['backfill']]))
        tasks.append(('港股结果回填', HK, [CMD['backfill']]))
    if args.mode in ('weekly', 'all'):
        tasks.append(('美股周报', US, [CMD['weekly'], '--days', '7']))
        tasks.append(('港股周报', HK, [CMD['weekly'], '--days', '7']))

    if args.dry_run:
        print('== 计划执行 ==')
        for name, cwd, args_ in tasks:
            print(f'  cd {cwd} && python3 {" ".join(args_)}')
        print(f'共 {len(tasks)} 步')
        return 0

    print('=' * 64)
    print(f'  每日运营流程 [{" / ".join(sorted(set(args.mode.split())))}]')
    print('=' * 64)
    failed = []
    for name, cwd, args_ in tasks:
        try:
            _run(name, cwd, args_)
        except SystemExit:
            failed.append(name)
    if failed:
        print(f'\n❌ 失败步骤: {", ".join(failed)}')
        return 1
    print('\n✅ 全部完成')
    return 0


if __name__ == '__main__':
    sys.exit(main())
