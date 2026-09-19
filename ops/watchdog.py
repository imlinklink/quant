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


def notify(title: str, msg: str):
    log(f'[通知] {title}: {msg}')
    try:
        subprocess.run(
            ['osascript', '-e',
             f'display notification "{msg}" with title "{title}"'],
            check=False, capture_output=True, timeout=10,
        )
    except Exception:
        pass


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
            notify('quant 看护', f'{name} 服务不在线，请检查')
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

    status = {'ok': not problems, 'checked_at': datetime.now().isoformat(timespec='seconds'),
              'problems': problems}
    (LOG_DIR / 'status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
    if problems:
        log('⚠️ 发现问题: ' + '; '.join(problems))
        return 1
    log('✅ 看护检查全部通过')
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
