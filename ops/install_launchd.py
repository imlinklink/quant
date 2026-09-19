#!/usr/bin/env python3
"""安装/预览/卸载本项目的两个 macOS LaunchAgent 作业。

**为什么不用 cron**（实测两个硬伤）：

1. `/usr/sbin/cron` 没有 `~/Documents` 的 TCC 权限 —— 读不了项目文件。整个 ops cron 块
   一周来一直在报 `Operation not permitted`（`cron_watchdog.log` 里 1941 条）。
2. **macOS 的 cron 不补跑错过的任务**。机器在 08:50 睡着，那次就永远跳过了；而本任务的
   决策窗口只有约 17 小时，错过就错过一整天。

LaunchAgent 的 `StartCalendarInterval` **在唤醒后会补跑**，这正是这里需要的。

两个作业：

- `shadow-daily`（默认）：R/L 双影子账户每日运行，北京周二~周六一天三次。
- `trading-service`：`run_all.py --dry-run` 常驻服务，**`KeepAlive` 挂了自动拉起**。
  它是手工前台启动的，2026-09-19 静默死过一次（17:40 起日志一行不写、无退出信息），
  停了 3.5 小时无人知道 —— 而唯一会喊「服务不在线」的 `ops/watchdog.py` 挂在 crontab
  上、整块仍指向迁移前的旧路径。**没有守护的常驻服务等于随时会停，且停了你不知道。**

用法：
    python3 ops/install_launchd.py --print                # 预览 plist（默认 shadow-daily）
    python3 ops/install_launchd.py --install              # 安装并加载（幂等）
    python3 ops/install_launchd.py --remove               # 卸载并移除
    python3 ops/install_launchd.py --run-now              # 立刻触发一次（验证用）
    python3 ops/install_launchd.py --job trading-service --install

**装 `trading-service` 前必须先停掉手工启动的实例**：`RunAtLoad` 会立刻拉起一个，
两个进程抢 8890 端口（先到的赢，后到的起不来，而屏幕上看起来"装成功"了）。
"""
import argparse
import json
import plistlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
US = ROOT / 'quant_us-main'
# 日志放 ~/Library/Logs —— **不要放回 ~/Documents**：万一 TCC 仍然拦着，
# 至少日志本身还能写出来，否则我们会看到一个"什么都没发生"的空洞。
LOG_DIR = Path.home() / 'Library' / 'Logs' / 'quant'
LAUNCH_AGENTS = Path.home() / 'Library' / 'LaunchAgents'

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

JOBS = {
    'shadow-daily': {
        'label': 'com.quant.shadow-daily',
        'argv': ['/bin/bash', str(ROOT / 'ops' / 'shadow_daily.sh')],
        'workdir': str(ROOT),
        'stdout': 'shadow_daily.log',
        'stderr': 'shadow_daily.err.log',
        'process_type': 'Background',
        'run_at_load': False,
        'calendar': [{'Weekday': w, 'Hour': h, 'Minute': m}
                     for w in WEEKDAYS for (h, m) in ATTEMPTS],
    },
    'trading-service': {
        'label': 'com.quant.trading-service',
        # 与 runbook §6 的手工命令同一件事，只是交给 launchd 托管
        'argv': ['/usr/bin/python3', 'run_all.py', '--dry-run'],
        'workdir': str(US),
        'stdout': 'trading-service.log',
        'stderr': 'trading-service.err.log',
        # **不设 ProcessType**：`Background` 会降调度优先级并限制 I/O，适合批处理作业，
        # 不适合一个要一直响应请求、跑定时器的常驻服务。
        'run_at_load': True,
        # 挂了就拉起来 —— 这个作业存在的全部理由
        'keep_alive': True,
        # 启动即失败时不至于打爆日志（launchd 的重启下限是 10s）
        'throttle': 60,
    },
}


def plist_path(job: str) -> Path:
    return LAUNCH_AGENTS / f'{JOBS[job]["label"]}.plist'


def build_plist(job: str = 'shadow-daily') -> dict:
    spec = JOBS[job]
    plist = {
        'Label': spec['label'],
        'ProgramArguments': list(spec['argv']),
        'WorkingDirectory': spec['workdir'],
        'StandardOutPath': str(LOG_DIR / spec['stdout']),
        'StandardErrorPath': str(LOG_DIR / spec['stderr']),
        'RunAtLoad': spec['run_at_load'],
        'EnvironmentVariables': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'},
    }
    # 逐项可选，避免给不需要的作业塞空键（plist 里出现 `KeepAlive: False` 那种
    # "显式关掉"和"根本没这项"含义不同，读的人容易误解）
    if spec.get('process_type'):
        plist['ProcessType'] = spec['process_type']
    if spec.get('calendar'):
        plist['StartCalendarInterval'] = spec['calendar']
    if spec.get('keep_alive'):
        plist['KeepAlive'] = True
    if spec.get('throttle'):
        plist['ThrottleInterval'] = spec['throttle']
    return plist


def _gui_domain() -> str:
    return f'gui/{__import__("os").getuid()}'


def install(job: str = 'shadow-daily') -> dict:
    path = plist_path(job)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先卸载旧的，保证幂等（load 一个已存在的 label 会失败）
    subprocess.run(['launchctl', 'bootout', _gui_domain(), str(path)],
                   capture_output=True, text=True)
    with path.open('wb') as fh:
        plistlib.dump(build_plist(job), fh)
    proc = subprocess.run(['launchctl', 'bootstrap', _gui_domain(), str(path)],
                          capture_output=True, text=True)
    return {'job': job, 'plist': str(path), 'loaded': proc.returncode == 0,
            'stderr': proc.stderr.strip()}


def remove(job: str = 'shadow-daily') -> dict:
    path = plist_path(job)
    proc = subprocess.run(['launchctl', 'bootout', _gui_domain(), str(path)],
                          capture_output=True, text=True)
    existed = path.exists()
    if existed:
        path.unlink()
    return {'job': job, 'removed_plist': existed, 'unloaded': proc.returncode == 0}


def run_now(job: str = 'shadow-daily') -> dict:
    """立刻触发一次（不等待排程）。测试用。

    对 `RunAtLoad`+`KeepAlive` 的作业，`kickstart` 若不是在跑会直接拉起。
    """
    proc = subprocess.run(
        ['launchctl', 'kickstart', '-p', f'{_gui_domain()}/{JOBS[job]["label"]}'],
        capture_output=True, text=True)
    return {'job': job, 'triggered': proc.returncode == 0, 'pid': proc.stdout.strip(),
            'stderr': proc.stderr.strip()}


def main(argv=None):
    p = argparse.ArgumentParser(description='安装本项目的 LaunchAgent 作业')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--print', action='store_true')
    g.add_argument('--install', action='store_true')
    g.add_argument('--remove', action='store_true')
    g.add_argument('--run-now', action='store_true')
    # 默认 shadow-daily：加 --job 之前只有这一个作业，旧命令行必须继续照原样工作
    p.add_argument('--job', choices=sorted(JOBS), default='shadow-daily')
    args = p.parse_args(argv)
    if args.print:
        print(plistlib.dumps(build_plist(args.job)).decode())
        return 0
    result = (install(args.job) if args.install
              else remove(args.job) if args.remove else run_now(args.job))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if not (args.install and not result.get('loaded')) else 1


if __name__ == '__main__':
    raise SystemExit(main())
