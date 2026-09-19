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
        # 2026-09-19 重装。此前整块指向迁移前的 ~/Documents/quant，六条全在报
        # `Operation not permitted`（TCC）。**实测探针确认**：仓库搬到 ~/quant 之后
        # cron 已能正常执行这里的命令（探针每分钟写一行，两次都成功）。
        # 但「macOS cron 不补跑睡过的任务」这条没变 —— 时间敏感的任务仍应走 launchd。
        '# 盘前：美股简报（服务的监控器读它：仓位缩放 + avoid 闸门）',
        # 只跑简报，**不跑 pipeline --mode morning**：那条把「选股建议」也捆在里面，
        # 而选股建议会调 LLM（付费）。恢复哪些是逐个定的，定时任务不该顺手把没定的
        # 一起打开 —— 要开请显式加一条。
        f'20 8 * * 1-5 cd {ROOT}/quant_us-main && '
        f'{PY} scripts/live_trading/run_market_brief.py '
        f'>> {log_dir}/cron_morning.log 2>&1',
        '# 盘后：结果回填（只美股；港股线 2026-09-19 起未恢复）',
        f'10 17 * * 1-5 cd {ROOT} && '
        f'{PY} ops/pipeline.py --mode evening --markets us '
        f'>> {log_dir}/cron_evening.log 2>&1',
        '# 周六：周报（只美股）',
        f'0 10 * * 6 cd {ROOT} && '
        f'{PY} ops/pipeline.py --mode weekly --markets us '
        f'>> {log_dir}/cron_weekly.log 2>&1',
        '# 美东收盘后(北京 08:35)：抄底扫描回填 + 归因（美股 P2 评估闭环）',
        f'35 8 * * 1-6 cd {ROOT}/quant_us-main && '
        f'{PY} scripts/live_trading/decision_ledger/backfill_scan_outcomes.py --days 3 '
        f'>> {log_dir}/cron_scan_backfill.log 2>&1 && '
        f'{PY} scripts/live_trading/decision_ledger/scan_attribution.py --horizon 24 '
        f'>> {log_dir}/cron_scan_backfill.log 2>&1',
        '# 港股收盘后(17:35)的扫描回填 + 归因**已移除**（2026-09-19：港股线未恢复）。',
        '#   要恢复就加回来，并同时把 ops_config.yaml 的 markets.hk.enabled 打开，',
        '#   否则看护会每 5 分钟报一次"港股简报不是今天的"。',
        '# R/L 双影子账户每日运行与常驻服务**都不在 cron**：',
        '#   见 install_launchd.py —— shadow-daily（每日三次）与 trading-service（KeepAlive）。',
        '# 看护（watchdog）**已移到 launchd**（StartInterval 300s）：它原在本块里每 5 分钟跑，',
        '#   但块整体是死的，等于没有看护 —— 2026-09-19 服务静默停了 3.5 小时无人知道。',
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
