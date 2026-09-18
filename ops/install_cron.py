#!/usr/bin/env python3
"""安装/预览/卸载 quant 运营定时任务（crontab）。

用法：
    python3 ops/install_cron.py --print    # 预览将写入的 crontab
    python3 ops/install_cron.py --install  # 安装（幂等，重复执行只保留一份）
    python3 ops/install_cron.py --remove   # 移除 quant-ops 块
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = '/usr/bin/python3'  # cron 环境 PATH 较短，用绝对路径

MARK_START = '# >>> quant-ops (auto-managed) >>>'
MARK_END = '# <<< quant-ops <<<'


def build_block() -> str:
    log_dir = ROOT / 'ops' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        MARK_START,
        '# 盘前：简报(美股/港股) + 选股建议 + 结果回填',
        f'20 8 * * 1-5 cd {ROOT} && {PY} ops/pipeline.py --mode morning >> {log_dir}/cron_morning.log 2>&1',
        '# 盘后：结果回填（港股收盘后）',
        f'10 17 * * 1-5 cd {ROOT} && {PY} ops/pipeline.py --mode evening >> {log_dir}/cron_evening.log 2>&1',
        '# 周六：周报',
        f'0 10 * * 6 cd {ROOT} && {PY} ops/pipeline.py --mode weekly >> {log_dir}/cron_weekly.log 2>&1',
        '# 美东收盘后(北京 08:35)：抄底扫描回填 + 归因（美股 P2 评估闭环）',
        f'35 8 * * 1-6 cd {ROOT}/quant_us-main && '
        f'{PY} scripts/live_trading/decision_ledger/backfill_scan_outcomes.py --days 3 '
        f'>> {log_dir}/cron_scan_backfill.log 2>&1 && '
        f'{PY} scripts/live_trading/decision_ledger/scan_attribution.py --horizon 24 '
        f'>> {log_dir}/cron_scan_backfill.log 2>&1',
        '# 港股收盘后(17:35)：扫描回填 + 归因（港股评估闭环）',
        f'35 17 * * 1-5 cd {ROOT}/quant_futu-main && '
        f'{PY} scripts/live_trading/decision_ledger/backfill_hk_checks.py --days 7 '
        f'>> {log_dir}/cron_hk_scan_backfill.log 2>&1 && '
        f'{PY} scripts/live_trading/decision_ledger/hk_scan_attribution.py '
        f'>> {log_dir}/cron_hk_scan_backfill.log 2>&1',
        '# R/L 双影子账户每日运行**不在 cron**：见 install_launchd.py 顶部的两条实测原因',
        '#   （cron 无 ~/Documents 的 TCC 权限；且 macOS cron 不补跑睡过的任务）。',
        '# 看护：每 5 分钟检查（只告警，默认不自动重启）',
        f'*/5 * * * * cd {ROOT} && {PY} ops/watchdog.py --once >> {log_dir}/cron_watchdog.log 2>&1',
        MARK_END,
    ]
    return '\n'.join(lines) + '\n'


def current_crontab() -> str:
    try:
        r = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=10)
        return r.stdout
    except Exception:
        return ''


def strip_old(content: str) -> str:
    lines = content.splitlines()
    out = []
    skip = False
    for ln in lines:
        if ln.strip() == MARK_START:
            skip = True
            continue
        if ln.strip() == MARK_END:
            skip = False
            continue
        if not skip:
            out.append(ln)
    text = '\n'.join(out).strip()
    return (text + '\n') if text else ''


def install():
    content = current_crontab()
    content = strip_old(content)
    content += build_block()
    p = subprocess.run(['crontab', '-'], input=content, text=True, capture_output=True)
    if p.returncode != 0:
        print('安装失败:', p.stderr)
        return 1
    print('✅ 已安装定时任务（幂等）')
    print()
    print(build_block())
    return 0


def remove():
    content = strip_old(current_crontab())
    p = subprocess.run(['crontab', '-'], input=content, text=True, capture_output=True)
    print('✅ 已移除 quant-ops 定时任务' if p.returncode == 0 else f'移除失败: {p.stderr}')
    return p.returncode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--print', action='store_true')
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--remove', action='store_true')
    args = parser.parse_args()

    if args.print:
        print('当前 crontab 里 quant-ops 块将替换为：')
        print('-' * 60)
        print(build_block())
        return 0
    if args.install:
        return install()
    if args.remove:
        return remove()
    parser.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
