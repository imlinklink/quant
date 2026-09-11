"""CSV/Parquet 通用读写；没有 Parquet 引擎时给出明确错误。"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd


def read_frame(path) -> pd.DataFrame:
    path = Path(path)
    if path.suffix == '.parquet':
        return pd.read_parquet(path)
    return pd.read_csv(path, compression='infer')


def write_frame(frame: pd.DataFrame, path, *, overwrite=False) -> Path:
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f'禁止覆盖数据文件: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == '.parquet':
        frame.to_parquet(path, index=False)
    else:
        frame.to_csv(path, index=False, compression='infer')
    return path


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

