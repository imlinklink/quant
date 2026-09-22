#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日报发布工具 —— 把各定时任务的产物按契约发布到 ~/quant-inputs/market-digest/。

为什么要有这一层（2026-09-18）
------------------------------
`ops/shadow_daily.sh` 由 LaunchAgent 在后台跑，**读不到 `~/Documents`**（TCC 权限），
所以日报必须有一份发布到 `~` 下。此前每条产出链各自决定落点：`~/Documents/美股盘后/`
与 `~/Documents/全球盘后/` 两份实际上从未被调度读到过（等于没发）。

本工具是**唯一**的发布入口：源文件留在原处（人类归档 + 各产出链自己的工作目录），
**发布副本**进 `market-digest`。这样产出链不必改输出路径，调度也不必知道产出链在哪 ——
两边只通过本工具约定的目录与文件名耦合。

命名契约（`market_digest.latest_digest` 依赖它，改这里必须同步改那边）
----------------------------------------------------------------------
    顶层    `YYYY-MM-DD_<kind>.html`   ← 文件名**必须以交易日日期开头**，否则被忽略
    aux/    `YYYY-MM-DD_<kind>.html`   ← 辅助产物；抽取器只 glob 顶层 `*.html`，读不到

同日多份的取用顺序由 `market_digest.DIGEST_PRIORITY` 决定（与日期一起参与排序）：
premarket(0) > uspostmarket(1) > globalpost(2) > postmarket(3) > 未列出(9)。

用法
----
    python3 ops/publish_digest.py --src <源文件> --kind premarket [--date YYYY-MM-DD]
    python3 ops/publish_digest.py --show-contract
    python3 ops/publish_digest.py --src x.html --kind postmarket --dry-run

日期缺省推断顺序：`--date` → 源文件名开头的 `YYYY-MM-DD` → 今天。
发布是**原子**的（先写 `.tmp` 再 rename），调度不会读到写了一半的文件。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

DIGEST_DIR = Path(os.environ.get('SHADOW_DIGEST_DIR')
                  or (Path.home() / 'quant-inputs' / 'market-digest'))

# kind -> (子目录, 文件名中段, 扩展名, 说明)
KINDS = {
    'premarket':    ('',    'premarket',          '.html', '每日盘前简报'),
    'uspostmarket': ('',    'uspostmarket',       '.html', '美股盘后深度'),
    'globalpost':   ('',    'globalpost',         '.html', '全球盘后日报'),
    'postmarket':   ('',    'postmarket',         '.html', '每日盘后复盘'),
    'gsmonitor':    ('',    'gsmonitor',          '.html', 'G/S v2 波段监控页'),
    'thsushot':     ('',    'thsushot',           '.html', '同花顺美股24小时热榜'),
    'xopinions':    ('aux', 'xopinions',          '.html', 'X 博主观点周报'),
    'probe':        ('aux', 'data_publish_probe', '.csv',  '数据源发布时点探测日志'),
}

DATE_PREFIX = re.compile(r'^(\d{4}-\d{2}-\d{2})')


def target_path(kind: str, day: str) -> Path:
    sub, stem, ext, _ = KINDS[kind]
    return DIGEST_DIR / sub / f'{day}_{stem}{ext}' if sub else DIGEST_DIR / f'{day}_{stem}{ext}'


def infer_date(src: Path | None, explicit: str | None) -> str:
    if explicit:
        if not DATE_PREFIX.match(explicit):
            sys.exit(f'[fatal] --date 必须是 YYYY-MM-DD，收到 {explicit!r}')
        return explicit
    if src is not None:
        m = DATE_PREFIX.match(src.name)
        if m:
            return m.group(1)
    return date.today().isoformat()


def publish(src: Path, kind: str, day: str) -> Path:
    if not src.is_file():
        sys.exit(f'[fatal] 源文件不存在：{src}')
    dest = target_path(kind, day)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() == dest.resolve():
        print(f'[skip] 源与目标同一文件，无需发布：{dest}')
        return dest
    if src.stat().st_size == 0:
        sys.exit(f'[fatal] 源文件为空，拒绝发布：{src}')

    tmp = dest.with_name(dest.name + '.tmp')
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)          # 原子：调度永远读不到半成品
    print(f'[published] {kind:12s} {src}  ->  {dest}  ({dest.stat().st_size} 字节)')
    return dest


def show_contract() -> None:
    print(f'发布根目录：{DIGEST_DIR}')
    print(f'{"kind":14s} {"落点":28s} {"取用序":6s} 说明')
    print('-' * 76)
    order = {'premarket': 0, 'uspostmarket': 1, 'globalpost': 2, 'postmarket': 3}
    for kind, (sub, stem, ext, desc) in KINDS.items():
        where = f'{sub}/' if sub else '(顶层)'
        pri = order.get(kind, 9)
        note = '' if not sub else '  ← 抽取器读不到（仅归档）'
        print(f'{kind:14s} {where + "YYYY-MM-DD_" + stem + ext:28s} {pri:<6d} {desc}{note}')
    print('-' * 76)
    print('顶层文件名必须以交易日日期开头，否则 latest_digest 会忽略它。')
    print('同日多份时按 (日期, 取用序, mtime) 取最大者 —— 取用序小的赢。')


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='把产物按契约发布到 market-digest')
    p.add_argument('--src', help='源文件路径')
    p.add_argument('--kind', choices=sorted(KINDS), help='产物类型')
    p.add_argument('--date', help='交易日 YYYY-MM-DD（缺省从源文件名推断，再缺省用今天）')
    p.add_argument('--dry-run', action='store_true', help='只打印落点，不写文件')
    p.add_argument('--show-contract', action='store_true', help='打印命名契约表')
    args = p.parse_args(argv)

    if args.show_contract:
        show_contract()
        return 0
    if not args.kind:
        p.error('需要 --kind（或 --show-contract）')

    src = Path(args.src).expanduser() if args.src else None
    day = infer_date(src, args.date)
    dest = target_path(args.kind, day)
    if args.dry_run:
        print(f'[dry-run] {args.src}  ->  {dest}')
        return 0
    if src is None:
        p.error('发布需要 --src')
    publish(src, args.kind, day)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
