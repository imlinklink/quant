#!/usr/bin/env python3
"""安装/预览/卸载 R/L 双影子账户每日运行（macOS LaunchAgent）。

**为什么不用 cron**（实测两个硬伤）：

1. `/usr/sbin/cron` 没有 `~/Documents` 的 TCC 权限 —— 读不了项目文件。整个 ops cron 块
   一周来一直在报 `Operation not permitted`（`cron_watchdog.log` 里 1941 条）。
2. **macOS 的 cron 不补跑错过的任务**。机器在 08:50 睡着，那次就永远跳过了；而本任务的
   决策窗口只有约 17 小时，错过就错过一整天。

LaunchAgent 的 `StartCalendarInterval` **在唤醒后会补跑**，这正是这里需要的。

用法：
    python3 ops/install_launchd.py --print     # 预览 plist
    python3 ops/install_launchd.py --install   # 安装并加载（幂等）
    python3 ops/install_launchd.py --remove    # 卸载并移除
    python3 ops/install_launchd.py --run-now   # 立刻触发一次（验证用）
"""
import argparse
import json
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'com.quant.shadow-daily'
PLIST = Path.home() / 'Library' / 'LaunchAgents' / f'{LABEL}.plist'
# 日志放 ~/Library/Logs —— **不要放回 ~/Documents**：万一 TCC 仍然拦着，
# 至少日志本身还能写出来，否则我们会看到一个"什么都没发生"的空洞。
LOG_DIR = Path.home() / 'Library' / 'Logs' / 'quant'
SCRIPT = ROOT / 'ops' / 'shadow_daily.sh'
# 北京周二~周六，**一天三次**。launchd 的 Weekday：0/7=周日，1=周一 … 6=周六。
#
# 为什么要多次：**Futu 的个股日线在收盘后数小时才出，且滞后时长不稳定**。实测滞后
# 已超过 8 小时（北京 11:50 = 美东 09-17 23:50，收盘后 8 小时，TECH 个股仍停在 09-16，
# 而同批 ETF 已到 09-17）—— 单次定时就是在赌那一刻数据到了没有，赌错就永远慢一天、
# 评审窗口直接错过（实测 `DECISION_WINDOW_MISSED`）。
#
# 三次尝试是安全的：整个任务**幂等**（机会/证据包/决策都是首次写入即冻结，`run-daily`
# 的结算按"晚于上次已结算"取值），数据没出的那几次会安静地什么都不做。
# 最后一次 19:40 距 T+1 的决策截止（美东 09:20 = 北京 21:20）还有 1.7 小时。
#
# 注意：LaunchAgent 的补跑是「唤醒后执行」，**不保证在截止前醒来**。所以脚本会记录每个
# 应跑 session 的终态（`ops/shadow_status.py`），漏跑看得见。
WEEKDAYS = (2, 3, 4, 5, 6)
ATTEMPTS = ((16, 40), (18, 10), (19, 40))


def build_plist() -> dict:
    return {
        'Label': LABEL,
        'ProgramArguments': ['/bin/bash', str(SCRIPT)],
        'WorkingDirectory': str(ROOT),
        'StartCalendarInterval': [{'Weekday': w, 'Hour': h, 'Minute': m}
                                  for w in WEEKDAYS for (h, m) in ATTEMPTS],
        'StandardOutPath': str(LOG_DIR / 'shadow_daily.log'),
        'StandardErrorPath': str(LOG_DIR / 'shadow_daily.err.log'),
        'ProcessType': 'Background',
        # 睡过的那次在唤醒后补跑（这是换掉 cron 的主要原因）
        'RunAtLoad': False,
        'EnvironmentVariables': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'},
    }


def _gui_domain() -> str:
    return f'gui/{__import__("os").getuid()}'


def install() -> dict:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    # 先卸载旧的，保证幂等（load 一个已存在的 label 会失败）
    subprocess.run(['launchctl', 'bootout', _gui_domain(), str(PLIST)],
                   capture_output=True, text=True)
    with PLIST.open('wb') as fh:
        plistlib.dump(build_plist(), fh)
    proc = subprocess.run(['launchctl', 'bootstrap', _gui_domain(), str(PLIST)],
                          capture_output=True, text=True)
    return {'plist': str(PLIST), 'loaded': proc.returncode == 0,
            'stderr': proc.stderr.strip()}


def remove() -> dict:
    proc = subprocess.run(['launchctl', 'bootout', _gui_domain(), str(PLIST)],
                          capture_output=True, text=True)
    existed = PLIST.exists()
    if existed:
        PLIST.unlink()
    return {'removed_plist': existed, 'unloaded': proc.returncode == 0}


def run_now() -> dict:
    """立刻触发一次（不等待排程）。测试用。"""
    proc = subprocess.run(['launchctl', 'kickstart', '-p', f'{_gui_domain()}/{LABEL}'],
                          capture_output=True, text=True)
    return {'triggered': proc.returncode == 0, 'pid': proc.stdout.strip(),
            'stderr': proc.stderr.strip()}


def main(argv=None):
    p = argparse.ArgumentParser(description='安装影子账户每日运行的 LaunchAgent')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--print', action='store_true')
    g.add_argument('--install', action='store_true')
    g.add_argument('--remove', action='store_true')
    g.add_argument('--run-now', action='store_true')
    args = p.parse_args(argv)
    if args.print:
        print(plistlib.dumps(build_plist()).decode())
        return 0
    result = (install() if args.install else remove() if args.remove else run_now())
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not (args.install and not result.get('loaded')) else 1


if __name__ == '__main__':
    raise SystemExit(main())
