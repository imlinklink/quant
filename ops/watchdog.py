#!/usr/bin/env python3
"""运营看护：检查 OpenD / 交易服务 / 简报新鲜度，异常时通知并记录。

用法：
    python3 ops/watchdog.py --once       # 跑一次（适合手动检查/cron 频繁调用）
    python3 ops/watchdog.py --daemon     # 常驻循环（按 ops_config.yaml interval）
"""
import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / 'ops' / 'ops_config.yaml'
LOG_DIR = ROOT / 'ops' / 'logs'


def log(msg: str):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    line = f'{datetime.now().isoformat(timespec="seconds")} {msg}'
    print(line)
    with open(LOG_DIR / f'watchdog_{date.today().strftime("%Y%m%d")}.log', 'a', encoding='utf-8') as f:
        f.write(line + '\n')


ALERT_SECONDS = 60
STATE_PATH = LOG_DIR / 'watchdog_alert_state.json'


def _as_str(text) -> str:
    """AppleScript 字符串字面量的转义。

    原先直接把 msg 插进 `display notification "..."` —— **消息里只要有一个双引号或反斜杠
    就会破坏整条脚本**，而 `check=False, capture_output=True` 把错误吞得干干净净：
    报错为零、通知为零，日志上却照写「[通知] …」。转义不是防御性编程，
    它是这条通道唯一的正确性来源。
    """
    return str(text).replace('\\', '\\\\').replace('"', '\\"')


def notify(title: str, msg: str) -> dict:
    """弹一条**会自动消失的模态告警**，并返回「有没有被人看到」的证据。

    为什么不用 `display notification`：**实测（2026-09-19）它静默失效** ——
    osascript 返回 rc=0、stderr 为空、`usernoted`/`NotificationCenter` 都在跑，
    而用户**一条都没看到**。原因：它要求发送方（osascript 的宿主 Script Editor）
    持有通知权限，而那个权限显然没给。`display alert` 走另一条路、不需要它
    （实测用户看到了并点了按钮）。

    `giving up after` 让它自动消失，无人值守时不会堆叠成一屏模态框。
    返回值里 `seen`（出现过 `button returned:`）与「超时无人处理」是**可区分**的 ——
    这样「通知了」与「被看到了」在日志里不再是一回事。
    """
    log(f'[通知] {title}: {msg}')          # 保留原日志行；但**这一行不代表送达**
    script = (f'display alert "{_as_str(title)}" message "{_as_str(msg)}" '
              f'as warning giving up after {ALERT_SECONDS}')
    try:
        proc = subprocess.run(['/usr/bin/osascript', '-e', script],
                              check=False, capture_output=True, timeout=ALERT_SECONDS + 15)
        out = (proc.stdout or b'').decode('utf-8', 'replace').strip()
        err = (proc.stderr or b'').decode('utf-8', 'replace').strip()
        seen = 'button returned:' in out
        log(f'[通知结果] {"已看到" if seen else "未确认（超时或未处理）"} — {out or err or proc.returncode}')
        return {'channel': 'alert', 'seen': seen, 'rc': proc.returncode,
                'detail': out or err}
    except Exception as exc:
        log(f'[通知结果] ❌ 通道异常: {exc!r}')
        return {'channel': 'alert', 'seen': False, 'rc': None, 'detail': repr(exc)}


def alert_if_changed(problems: list) -> Optional[dict]:
    """**只在问题集合变化时**弹告警；返回 notify 的结果（没弹则 None）。

    不这么做的话，一个持续存在的问题会**每 5 分钟弹一次模态框** —— `--once` 是无状态的，
    每次运行都不知道上次报过什么。告警重复到第三次就没人看了，那与没有告警等价。
    状态落盘，所以跨进程也记得。
    """
    key = sorted(problems)
    try:
        last = json.loads(STATE_PATH.read_text(encoding='utf-8')).get('key') or []
    except Exception:
        last = []
    if key == last:
        log('（问题集合与上次相同，不重复弹告警）')
        return None
    STATE_PATH.write_text(
        json.dumps({'key': key, 'at': datetime.now().isoformat(timespec='seconds')},
                   ensure_ascii=False), encoding='utf-8')
    if not key:
        return None                       # 恢复：只落盘新状态，不弹（避免多一条噪音）
    return notify('quant 看护', '; '.join(key))


def drift_checks():
    """跑三处一致性核对，返回 [(名称, 返回码, 输出)]。

    **为什么放进看护**：本仓库的典型失效形态是「代码是对的、**盘上/记录里的是旧的**」——
    迁移到 `~/quant` 之后没人重跑 `install_cron.py --install`，整块 crontab 仍指向
    `~/Documents`、六条任务全在报 `Operation not permitted`，而**生成器一直是对的**。
    单元测试抓不住这类漂移（它测"生成什么"，坏的是"盘上装的是什么"），
    而**一个没人执行的核对与没有核对等价** —— 所以挂在这里，让它每 5 分钟被自动跑一次。

    三处：`crontab`、`LaunchAgent`、以及 `config.yaml` 与那份在 git 里的
    `docs/llm-decision-settings.yaml` 记录是否一致（`config.yaml` 含明文 key、不在 git 里，
    所以它的改动没有版本历史，记录就是那份历史）。
    """
    out = []
    for label, args in (('crontab', ['install_cron.py', '--check']),
                        ('LaunchAgent', ['install_launchd.py', '--check']),
                        # 配置记录：`config.yaml` 不在 git 里（含明文 key），所以"改了配置
                        # 才让某个行为成立"的决定记在 docs 那份**在** git 里的记录中。
                        # 记录落后于配置就比没有更糟（读的人以为那就是现状）。
                        # 会弹告警，但 `alert_if_changed` 去重 ⇒ 改配置时只弹一次。
                        ('配置记录', ['check_config_record.py'])):
        try:
            proc = subprocess.run(
                [sys.executable, str(ROOT / 'ops' / args[0]), *args[1:]],
                check=False, capture_output=True, text=True, timeout=30)
            out.append((label, proc.returncode,
                        ((proc.stdout or '') + (proc.stderr or '')).strip()))
        except Exception as exc:
            out.append((label, 1, repr(exc)))
    return out


def shadow_job_check(days: int = 3):
    """近 N 天每个交易日的三个影子作业是否跑完。返回 `(返回码, 输出)`。

    **为什么放进看护**：`ShadowJobs.claim` 在重试耗尽后恒返回 None ⇒ **失败的 session
    会永久停在那里**；更隐蔽的一类是**完全没有记录**（服务当时没在跑），
    而"没有事件"不会出现在任何报表里。在此之前没有任何东西会说这件事。

    窗口取近 3 天且**不含今天**：今天的作业在美东 16:20 之后才跑，白天报缺就是狼来了。
    告警文案刻意**不含数字与日期** —— 状况不变时它必须稳定，否则 `alert_if_changed`
    的去重会失效，变成每天弹一次。
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / 'quant_us-main' / 'scripts' / 'live_trading'
                                  / 'shadow_job_health.py'), '--days', str(days)],
            check=False, capture_output=True, text=True, timeout=180,
            cwd=str(ROOT / 'quant_us-main'))
        return proc.returncode, ((proc.stdout or '') + (proc.stderr or '')).strip()
    except Exception as exc:
        return 1, repr(exc)


def port_open(port: int, host: str = '127.0.0.1') -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


def brief_fresh(path: Path) -> tuple:
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data.get('date') == date.today().strftime('%Y-%m-%d'), data
    except Exception:
        return False, {}


def check_once(cfg: dict) -> int:
    problems = []
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    opend_port = int(cfg.get('opend_port', 11111))
    if not port_open(opend_port):
        problems.append('富途 OpenD 未运行（端口 11111）')
    else:
        log('✅ OpenD 运行中')

    for svc in cfg.get('services', []):
        if not svc.get('enabled', False):
            continue
        name = svc.get('name')
        up_port = port_open(int(svc.get('port', 8899)))
        up_http = http_ok(svc.get('url', '')) if svc.get('url') else up_port
        if up_port and up_http:
            log(f'✅ [{name}] 服务正常 ({svc.get("url")})')
        else:
            problems.append(f'[{name}] 服务未运行或页面不可达')
            if cfg.get('restart_enabled') and svc.get('restart_cmd'):
                log(f'[{name}] 尝试自动重启: {svc["restart_cmd"]}')
                subprocess.Popen(svc['restart_cmd'], shell=True,
                                 cwd=str(ROOT / svc.get('project', '')))

    for m in cfg.get('markets', []):
        # 关掉的市场不检查（默认 true）。港股线 2026-09-19 起未恢复，不关掉的话
        # 它的简报会永远是"不是今天的"，于是每 5 分钟报一次港股 —— 把真问题淹掉。
        if not m.get('enabled', True):
            log(f"⏭️  [{m.get('name')}] 已停用，跳过简报检查")
            continue
        fresh, data = brief_fresh(ROOT / m.get('brief_path', ''))
        if fresh:
            log(f"✅ [{m.get('name')}] 简报是今天的: {data.get('risk_level')}")
        else:
            problems.append(f"[{m.get('name')}] 简报不是今天的，需要运行 run_market_brief.py")

    # 三处一致性核对：**必须被自动发现**，理由见 `drift_checks`。
    # 措辞保持中性 —— 三项性质不同（装到系统的 crontab / plist，与一份记录文件），
    # 用"与生成结果一致""需重装"这类只对其中一项成立的说法会误导。
    for label, rc, detail in drift_checks():
        if rc == 0:
            log(f'✅ {label} 与预期一致')
        else:
            problems.append(f'{label} 与预期不一致（需同步）')
            log(f'❌ {label} 与预期不一致:\n{detail}')

    # 每日作业完整性（理由见 `shadow_job_check`）：**缺口必须被看见**
    rc, detail = shadow_job_check()
    if rc == 0:
        log('✅ 影子日作业：近 3 天全部完成')
    else:
        problems.append('影子日作业有未完成的 session（近 3 天）')
        log(f'❌ 影子日作业缺口:\n{detail}')

    status = {'ok': not problems, 'checked_at': datetime.now().isoformat(timespec='seconds'),
              'problems': problems}
    (LOG_DIR / 'status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
    if problems:
        log('⚠️ 发现问题: ' + '; '.join(problems))
        # 告警收口在一处（原先散在服务循环里，每次失败都弹）：**只在问题集合变化时弹**。
        alert_if_changed(problems)
        return 1
    log('✅ 看护检查全部通过')
    alert_if_changed(problems)          # 记下"已恢复"，这样下次真的再坏时会重新弹
    return 0


def main():
    parser = argparse.ArgumentParser(description='quant 运营看护')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--daemon', action='store_true')
    args = parser.parse_args()

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding='utf-8')) or {}
    if args.once or not args.daemon:
        return check_once(cfg)

    interval = int(cfg.get('check_interval_sec', 300))
    log(f'看护常驻启动，每 {interval}s 检查一次')
    while True:
        try:
            check_once(cfg)
        except Exception as e:
            log(f'检查异常: {e}')
        time.sleep(interval)


if __name__ == '__main__':
    sys.exit(main())
