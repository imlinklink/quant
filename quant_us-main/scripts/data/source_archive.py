#!/usr/bin/env python3
"""来源归档：把原始响应按 `<archive_root>/<source_id>/<run_id>/` 不可变保存。

对应技术设计 §2.3：`fetch(source, window, cursor) -> raw immutable files`。
真实来源由适配器拉取后交给本模块落盘；测试直接归档本地 fixture 目录。
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from scripts.data.io_utils import sha256_file

RAW_TABLES = ('security_master', 'symbol_history', 'corporate_actions')


def _file_record(path: Path) -> dict:
    return {'name': path.name, 'size': path.stat().st_size, 'sha256': sha256_file(path)}


def archive_source(raw_dir, source_id: str, run_id: str, archive_root) -> Path:
    """把 raw_dir 下的原始文件复制到不可覆盖的归档目录，并写 source_manifest.json。"""
    raw_dir = Path(raw_dir)
    if not raw_dir.is_dir():
        raise FileNotFoundError(f'原始来源目录不存在: {raw_dir}')
    target = Path(archive_root) / str(source_id) / str(run_id)
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f'来源归档已存在，禁止覆盖: {target}')
    (target / 'raw').mkdir(parents=True, exist_ok=True)
    files = []
    for src in sorted(raw_dir.glob('*.csv')):
        dst = target / 'raw' / src.name
        shutil.copy2(src, dst)
        files.append(_file_record(dst))
    manifest = {
        'source_id': str(source_id),
        'run_id': str(run_id),
        'ingested_at': datetime.now(timezone.utc).isoformat(),
        'raw_dir': str(raw_dir),
        'files': files,
    }
    (target / 'source_manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return target


def load_raw_table(archive_dir, table: str):
    """从归档目录读取某张原始表；不存在则返回 None。"""
    import pandas as pd
    path = Path(archive_dir) / 'raw' / f'{table}.csv'
    if not path.is_file():
        return None
    return pd.read_csv(path)
