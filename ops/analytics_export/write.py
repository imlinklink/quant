"""按「世代」写快照，**整代写完才切 `latest`**。

调度与页面永远读不到写了一半的代：先写 `generation/<id>/`，全部成功后才把 `latest`
这个符号链接原子换过去（`os.replace` 一个临时链接）。

保留 `KEEP_GENERATIONS` 代，更老的删掉 —— **但删除带路径守卫**：`data/` 在两个运行
checkout 里是同一份（`quant-runtime-main/quant_us-main/data` 是指向开发 checkout 的
symlink），删错目录就是打穿运行环境。本仓库对「静默删错东西」有历史，所以这里
断言目标路径必须含 `web_snapshots/generation/` 才允许删。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

KEEP_GENERATIONS = 5
_ROOT_NAME = 'web_snapshots'
_GENERATION_DIR = 'generation'


def _dump(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)          # 原子：读方永远看不到半成品


def write_generation(snapshot_dir, generation_id: str, files: dict) -> Path:
    """`files` = `{相对路径: payload}`（payload 为 dict/list/str）。返回该代目录。"""
    root = Path(snapshot_dir)
    gen = root / _GENERATION_DIR / generation_id
    if gen.exists():
        raise FileExistsError(f'GENERATION_EXISTS:{gen}')
    for rel, payload in files.items():
        if isinstance(payload, str):
            p = gen / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(payload, encoding='utf-8')
        else:
            _dump(gen / rel, payload)
    # 整代写完才切 latest（相对链接，便于整目录搬迁）
    link = root / 'latest'
    tmp = root / 'latest.tmp'
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(Path(_GENERATION_DIR) / generation_id)
    os.replace(tmp, link)
    return gen


def prune(snapshot_dir, keep: int = KEEP_GENERATIONS) -> list:
    """删掉最老的几代。带路径守卫，见模块 docstring。

    守卫是**精确**的（不是子串匹配）：根目录必须就叫 `web_snapshots`，且每个待删项必须是
    `<root>/generation` 的**直接子目录**。子串版本会被 `.../not_web_snapshots/generation/`
    这类路径骗过 —— 删错目录在本仓库是「打穿运行环境」级别的事故。
    """
    root = Path(snapshot_dir)
    if root.name != _ROOT_NAME:
        raise ValueError(f'PRUNE_ROOT_NAME_REFUSED:{root}')
    gens_dir = (root / _GENERATION_DIR).resolve()
    gens = sorted(p for p in (root / _GENERATION_DIR).glob('*') if p.is_dir())
    removed = []
    for old in gens[:-keep] if keep > 0 else gens:
        if old.resolve().parent != gens_dir:
            raise ValueError(f'PRUNE_PATH_GUARD_REFUSED:{old}')
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


def read_latest(snapshot_dir) -> dict | None:
    """读 `latest/index.json`（页面侧也走这条路径，**不列举目录猜**）。"""
    idx = Path(snapshot_dir) / 'latest' / 'index.json'
    if not idx.exists():
        return None
    return json.loads(idx.read_text(encoding='utf-8'))
