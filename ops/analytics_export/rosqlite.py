"""唯一的 sqlite 打开点，且**只有只读**。

`mode=ro` 是硬要求而不是风格：`PRAGMA`/`BEGIN IMMEDIATE`/`migrate()` 都会写库或留下
`-journal`/`-wal`，而 web 侧的只读证明
（`tests/unit/live/test_analytics_readonly.py`）会断言进程内**不存在**非 `mode=ro`
的连接。四个账本实测 `journal_mode=delete`、无 `-wal`/`-shm` 旁文件 ⇒ `mode=ro` 是
零副作用的（不需要 shm 写权限）。

路径不存在时**直接抛**，不返回空结果集 —— 「读不到」与「读到空」在页面上必须是两件事
（需求场景 10：来源失败不得退化成一个伪造的空账户）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def open_ro(path) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f'LEDGER_NOT_FOUND:{p}')
    con = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    return con
