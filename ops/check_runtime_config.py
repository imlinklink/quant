#!/usr/bin/env python3
"""核对**运行 checkout 的 `config.yaml` 与开发 checkout 的一致**。

**为什么需要它（实测的事故）**：2026-09-22 的运行版本隔离把实验钉到了独立 checkout，
但 `config.yaml` **没跟着搬** —— worktree 是从 git 建的，拿到的是 **HEAD 里那份 93 行的
初始版**，而真正在用的运维配置（419 行，含 `llm` / `llm_decision` / `trend_breakout` /
`risk_budget` / `shadow_integration` 等 11 个块）只存在于开发 checkout（skip-worktree 隐藏）。

后果静默了约 24 小时：`trend_breakout.enabled` 缺失 ⇒ **突破线监控器根本没启动**
（服务日志里 `唐奇安突破监控器已启动` 出现 0 次、扫描心跳停在搬迁那一刻）；
`llm` 块缺失 ⇒ **没有 api_key、模型调用会失败**。

**为什么既有的核对都没抓到**：隔离的核验看的是冻结哈希、续跑一致、`install_launchd/cron
--check`、服务 health —— 没有一项检查**配置内容**。这与
`check_config_record.py` 是同一类：**代码/部署定义是对的、实际跑的那份配置是旧的**。

判据取**逐字节一致**（不是"包含必需块"）：运行 checkout 的 config 就该是开发 checkout 的
那一份。这样既抓"缺"也抓"旧"——后者同样致命（配置改了却只在一边生效）。

用法：
    python3 ops/check_runtime_config.py     # 一致返回 0；不一致返回 1 并列出差异
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEV_CONFIG = ROOT / 'quant_us-main' / 'config.yaml'
RUNTIME_CHECKOUTS = (
    Path('/Users/wh1817w/quant-runtime-main'),
    Path('/Users/wh1817w/quant-runtime-research'),
)


def _blocks(path: Path):
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def compare(dev_config: Path, checkouts) -> list:
    """返回差异清单（空 = 一致）。抽成函数是为了能对临时目录测它。"""
    if not dev_config.exists():
        return [f'开发 checkout 的配置不存在：{dev_config}']
    want = dev_config.read_bytes()
    want_blocks = _blocks(dev_config) or {}
    problems = []
    for checkout in checkouts:
        checkout = Path(checkout)
        cfg = checkout / 'quant_us-main' / 'config.yaml'
        name = checkout.name
        if not checkout.exists():
            problems.append(f'{name}: checkout 不存在（{checkout}）')
            continue
        if not cfg.exists():
            problems.append(f'{name}: 没有 config.yaml')
            continue
        got = cfg.read_bytes()
        if hashlib.sha256(got).hexdigest() == hashlib.sha256(want).hexdigest():
            continue
        got_blocks = _blocks(cfg) or {}
        missing = sorted(set(want_blocks) - set(got_blocks))
        extra = sorted(set(got_blocks) - set(want_blocks))
        detail = []
        if missing:
            detail.append(f'缺 {len(missing)} 个顶层块：{missing}')
        if extra:
            detail.append(f'多 {len(extra)} 个：{extra}')
        detail.append(f'行数 {len(got.splitlines())} vs 开发 {len(want.splitlines())}')
        problems.append(f'{name}: 与开发 checkout 的配置不一致 —— ' + '；'.join(detail))
    return problems


def main() -> int:
    problems = compare(DEV_CONFIG, RUNTIME_CHECKOUTS)
    if not problems:
        return 0
    print('运行 checkout 的 config.yaml 与开发 checkout 不一致（**这正是那次静默事故的形态**）：')
    for p in problems:
        print(f'  ❌ {p}')
    print('  处置：把开发 checkout 的 config.yaml 复制过去，并在那边设 skip-worktree，然后重启服务。')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
