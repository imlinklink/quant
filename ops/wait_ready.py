#!/usr/bin/env python3
"""等「网络 + OpenD」就绪（各日作业在动网络之前调用它）。

**为什么需要**：这台机器会频繁进出睡眠。每次醒来都把网络与 Futu 连接切断 ——
服务日志实测同一刻成批出现 `Disconnected: … reason=KeepAliveFail`（7 条连接一起断）。
而作业的排程时刻是**墙上固定**的，撞上"刚醒、还没连上"的窗口就整条失败。
三条实测（都是这个形态）：

| 时刻 | 作业 | 表现 |
|---|---|---|
| 2026-09-24 09:03 | market-brief | LLM 调用 DNS 失败 `Errno 8` |
| 2026-09-23 20:07:51 | shadow-daily | `FAIL refresh`（同刻 OpenD 7 条连接 KeepAliveFail） |
| 2026-09-23 21:08:54 | forward-arms | `FAIL 行动导入` |

**排除法证据**：手动重跑同一个公司行动导入器（同 32 只、同路径）得到
`{"codes": 32, "rows": 1871, "fetch_failures": {}}` —— **0 失败**。
所以不是代码/数据问题，是**时刻**问题。

**为什么不改排程时刻**：机器的睡眠/唤醒时间随用户作息变（实测有 09:20 才联网的日子，
也有 20:07、21:08 正在睡的时段）。等就绪比猜时刻可靠。

就绪判据（两条都要）：
1. **DNS** 能解析 LLM 的主机（从 `config.yaml` 的 `llm.base_url` 取，取不到退回已知主机）；
2. **OpenD**（默认 127.0.0.1:11111）能建 TCP 连接。

用法::

    python3 ops/wait_ready.py [--seconds 900] [--quiet]

就绪 → 退出码 0 并打印 `READY …`；超时 → 退出码 1 并打印 `TIMEOUT …`。
**超时如实失败**：宁可这一轮不跑，也不在半截网络上跑出一份残缺结果。
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FALLBACK_HOST = 'api.deepseek.com'
OPEND = ('127.0.0.1', 11111)
POLL_SECONDS = 10


def llm_host(root: Path = ROOT) -> str:
    """从 config.yaml 的 `llm.base_url` 取主机名；取不到就退回来一个已知主机。

    **不硬编码**：换供应商时这里跟着走，不需要改代码。
    """
    try:
        import yaml
        cfg = yaml.safe_load((root / 'quant_us-main' / 'config.yaml').read_text(encoding='utf-8'))
        base = str(((cfg or {}).get('llm') or {}).get('base_url') or '')
        if '//' in base:
            host = base.split('//', 1)[1].split('/', 1)[0].split(':', 1)[0]
            if host:
                return host
    except Exception:
        pass
    return FALLBACK_HOST


def check(host: str) -> tuple:
    dns_ok = opend_ok = False
    try:
        socket.gethostbyname(host)
        dns_ok = True
    except Exception:
        pass
    try:
        with socket.create_connection(OPEND, timeout=3):
            opend_ok = True
    except Exception:
        pass
    return dns_ok, opend_ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--seconds', type=int, default=900, help='最多等多少秒（默认 900）')
    ap.add_argument('--quiet', action='store_true', help='只在结束时打印一行')
    args = ap.parse_args(argv)

    host = llm_host()
    deadline = time.time() + args.seconds
    tries = 0
    while True:
        tries += 1
        dns_ok, opend_ok = check(host)
        if dns_ok and opend_ok:
            print(f'READY {tries} 次尝试 DNS={host} OpenD={OPEND[0]}:{OPEND[1]}')
            return 0
        if time.time() >= deadline:
            print(f'TIMEOUT {tries} 次尝试 DNS={dns_ok} OpenD={opend_ok}（等了 {args.seconds}s）')
            return 1
        if not args.quiet:
            print(f'  等待就绪（第 {tries} 次：DNS={dns_ok} OpenD={opend_ok}）', flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    raise SystemExit(main())
