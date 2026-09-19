#!/usr/bin/env python3
"""核对 `docs/llm-decision-settings.yaml` 与 `config.yaml` 是否一致。

**为什么需要它**：`config.yaml` 含明文 API key ⇒ 不在 git 里（skip-worktree 隐藏），
**它的改动没有版本历史**。于是"改了配置才让某个行为成立"的决定记在
`docs/llm-decision-settings.yaml`（那份**在** git 里）。但记录一旦落后于配置，
它就比没有更糟：读的人会以为那就是现状。

这与 `install_cron.py --check` / `install_launchd.py --check` 是同一类核对 ——
**代码/记录是对的、装上去/实际跑的是旧的**，只有比对才看得见。

用法：
    python3 ops/check_config_record.py        # 一致返回 0，不一致返回 1 并逐项列出
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
US = ROOT / 'quant_us-main'
CONFIG = US / 'config.yaml'
RECORD = US / 'docs' / 'llm-decision-settings.yaml'

# 记录的节点范围。`config.yaml` 的其余节点（risk_budget、策略开关、人工审批…）不在本文内。
NODES = ('llm_decision', 'llm_permissions', 'shadow_integration')


def diff(live, record, path, out):
    """把 live 相对 record 的缺失/不同逐项收集进 out（**逐层递归**）。"""
    if isinstance(live, dict):
        rec = record if isinstance(record, dict) else {}
        for key, value in live.items():
            if key not in rec:
                out.append(f'{path}.{key} 在记录里缺失')
            else:
                diff(value, rec[key], f'{path}.{key}', out)
    elif isinstance(live, list):
        if list(live) != list(record or []):
            out.append(f'{path}: 配置={live} 记录={record}')
    elif live != record:
        out.append(f'{path}: 配置={live!r} 记录={record!r}')


def main() -> int:
    live = yaml.safe_load(CONFIG.read_text(encoding='utf-8')) or {}
    record = yaml.safe_load(RECORD.read_text(encoding='utf-8')) or {}
    problems = []
    for node in NODES:
        diff(live.get(node) or {}, record.get(node) or {}, node, problems)
    if problems:
        print(f'❌ {RECORD.name} 与 config.yaml 不一致（{len(problems)} 处）：')
        for item in problems:
            print('   -', item)
        print('\n   要么把配置改回记录，要么更新记录并说明为什么改。')
        return 1
    print(f'✅ {RECORD.name} 与 config.yaml 的 {" / ".join(NODES)} 逐项一致')
    return 0


if __name__ == '__main__':
    sys.exit(main())
