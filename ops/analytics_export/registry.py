"""来源登记：唯一一份「哪个实验在哪份账本上、由哪份代码写」的权威。

需求 §4 明令「实验使用显式来源登记……**不按『最新文件』猜当前实验**」。所以本模块：

- 只按登记里的**显式路径**开文件，**没有 glob、没有 latest 指针**（有源码 tripwire 测试）；
- 把从 `arms.json` 派生的三臂展开成具体 scope（**读** `arms.json` 而不是抄一份实验号，
  避免「同一件事两份定义」——那是本仓库出过多次真 bug 的形态）；
- 提供**反向完备性**检查：盘上每个实验目录都必须在 `scopes` 或 `unregistered`(带 why) 里。
  新实验落地而没登记 ⇒ 测试红。这条是刻意的：本仓库最怕的失败形态是「漏了但没人知道」。
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_REGISTRY = Path(__file__).resolve().parents[1] / 'analytics_scopes.json'


def load_registry(path=None) -> dict:
    p = Path(path) if path else DEFAULT_REGISTRY
    reg = json.loads(p.read_text(encoding='utf-8'))
    if reg.get('registry_version') != 1:
        raise ValueError(f'REGISTRY_VERSION_UNSUPPORTED:{reg.get("registry_version")}')
    seen = set()
    for s in reg.get('scopes') or []:
        if s['scope_id'] in seen:
            raise ValueError(f'DUPLICATE_SCOPE_ID:{s["scope_id"]}')
        seen.add(s['scope_id'])
    return reg


def _arm_scopes(base: Path, entry: dict) -> list:
    """把 `derive_from_arms` 展开成每臂一个 scope。

    `arms.json` 是 `forward_arms start` 写下的冻结记录（含每臂 experiment_id 与
    start_session），所以这里的边界是**机械事实**，不是声明。
    """
    arms_path = base / entry['derive_from_arms']
    arms = json.loads(arms_path.read_text(encoding='utf-8'))
    out = []
    for arm, meta in sorted(arms['arms'].items()):
        exp = meta['experiment_id']
        out.append({
            'scope_id': f'paper:{exp}',
            'kind': 'paper',
            'label': f'三臂前向 {arm}（universe={meta.get("universe_size")}'
                     f'{"，时点门" if meta.get("pit_gate") else ""}）',
            'experiment_id': exp,
            'run_dir': f'{entry["derive_from_arms"].rsplit("/", 1)[0]}/{arm}',
            'manifest': None,
            'ledger': f'{entry["derive_from_arms"].rsplit("/", 1)[0]}/{arm}/ledger.sqlite3',
            'accounts': [{'account_id': f'SHADOW:{exp}:R', 'role': 'R', 'label': '规则'}],
            'replay_start_session': None,
            'replay_end_session': None,
            'forward_start_session': arms.get('start_session'),
            'boundary_source': 'from_arms_json',
            'strategy_version_extra': {
                'registration_sha256': arms.get('registration_sha256'),
                'baseline_manifest_sha256': arms.get('baseline_manifest_sha256'),
                'arm': arm,
            },
            'writer_checkout': entry.get('writer_checkout'),
            'writer_pin': entry.get('writer_pin'),
            'export_version': entry.get('export_version', 1),
        })
    return out


def expand(base: Path, registry: dict) -> list:
    """把登记展开成具体的 scope 列表（`paper_arms` 展开成每臂一条）。"""
    out = []
    for entry in registry['scopes']:
        if 'derive_from_arms' in entry:
            out.extend(_arm_scopes(base, entry))
        else:
            out.append(dict(entry))
    return out


def registered_dirs(registry: dict) -> set:
    """登记里出现过的运行目录（相对 base）。"""
    dirs = set()
    for s in registry['scopes']:
        if 'run_dir' in s:
            dirs.add(s['run_dir'])
        if 'derive_from_arms' in s:
            dirs.add(s['derive_from_arms'].rsplit('/', 1)[0])
    for u in registry.get('unregistered') or []:
        dirs.add(u['run_dir'])
    return dirs


def scan_run_dirs(base: Path) -> set:
    """盘上**实际存在**的实验运行目录（相对 base）。"""
    found = set()
    for manifest in sorted(base.glob('portfolio_shadow/*/manifest.json')):
        found.add(str(manifest.parent.relative_to(base)))
    for pattern in ('strategy_diagnostics/SD-*', 'strategy_research/SR-*'):
        for d in sorted(base.glob(pattern)):
            if d.is_dir():
                found.add(str(d.relative_to(base)))
    return found


def check_completeness(base: Path, registry: dict) -> list:
    """未登记的运行目录 —— 空列表才算通过。"""
    return sorted(scan_run_dirs(base) - registered_dirs(registry))
