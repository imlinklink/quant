"""`RawSqlStore`：`ShadowStore` 的只读替身（同样的方法名与返回形状，走 `mode=ro` 原始 SQL）。

存在的唯一理由是**版本门**（见包 docstring）：库里有 schema 9 与 schema 10 两代账本，
ORM 打不开全部。方法语义**逐条对照** `scripts/portfolio_shadow/store.py` 实现 ——
行顺序、字段来源、`body` 反序列化方式都保持一致，否则 `report.py` 的算式会在两套读者上
得出不同结果。`tests/unit/ops/test_analytics_metrics_parity.py` 用同一份 fixture 账本
分别喂 `ShadowStore` 与 `RawSqlStore`，断言四个指标函数逐键相等。

**这里不做任何解释**：只把账本里已经写下的东西读出来。分类与词表在
`vocabulary.py`，指标算式在 `report.py`，本模块不新增第三处。
"""
from __future__ import annotations

import json
from contextlib import closing

from .rosqlite import open_ro


class RawSqlStore:
    def __init__(self, path, experiment_id: str):
        self.path = str(path)
        self.experiment_id = experiment_id

    def _all(self, sql: str, args=()):
        with closing(open_ro(self.path)) as con:
            return con.execute(sql, args).fetchall()

    def _one(self, sql: str, args=()):
        with closing(open_ro(self.path)) as con:
            return con.execute(sql, args).fetchone()

    # ---- 机会 -----------------------------------------------------------------
    def opportunity_terminals(self) -> dict:
        rows = self._all('SELECT body FROM decision_events WHERE account_scope LIKE ?',
                         (f'SHADOW:{self.experiment_id}%',))
        out = {}
        for r in rows:
            ev = json.loads(r[0])
            if ev.get('event_type') == 'shadow:opportunity_terminal':
                p = ev['payload']
                out[p['opportunity_id']] = p['terminal']
        return out

    def opportunities(self) -> list:
        return [json.loads(r[0]) for r in self._all(
            'SELECT body FROM shadow_opportunities WHERE experiment_id=? '
            'ORDER BY session, rank, opportunity_id', (self.experiment_id,))]

    def opportunity_rows(self) -> list:
        return [(r[0], json.loads(r[1])) for r in self._all(
            'SELECT opportunity_id, body FROM shadow_opportunities WHERE experiment_id=? '
            'ORDER BY session, rank, opportunity_id', (self.experiment_id,))]

    def opportunity(self, opportunity_id: str):
        row = self._one('SELECT body FROM shadow_opportunities WHERE experiment_id=? '
                        'AND opportunity_id=?', (self.experiment_id, opportunity_id))
        return json.loads(row[0]) if row else None

    # ---- 应用动作 -------------------------------------------------------------
    def application(self, scope: str, opportunity_id: str):
        row = self._one('SELECT body FROM shadow_applications WHERE experiment_id=? '
                        'AND scope=? AND opportunity_id=?',
                        (self.experiment_id, scope, opportunity_id))
        return json.loads(row[0]) if row else None

    def applications(self, scope: str | None = None) -> list:
        if scope is None:
            rows = self._all('SELECT body FROM shadow_applications WHERE experiment_id=?',
                             (self.experiment_id,))
        else:
            rows = self._all('SELECT body FROM shadow_applications WHERE experiment_id=? '
                             'AND scope=?', (self.experiment_id, scope))
        return [json.loads(r[0]) for r in rows]

    # ---- 冻结证据包 -----------------------------------------------------------
    def packet_for_opportunity(self, opportunity_id: str):
        row = self._one('SELECT body FROM shadow_packets WHERE experiment_id=? '
                        'AND opportunity_id=?', (self.experiment_id, opportunity_id))
        return json.loads(row[0]) if row else None

    def packets_matching(self, fragment: str) -> list:
        if not fragment:
            raise ValueError('FRAGMENT_REQUIRED')
        rows = self._all('SELECT opportunity_id, body FROM shadow_packets WHERE experiment_id=? '
                         'AND opportunity_id LIKE ? ORDER BY opportunity_id',
                         (self.experiment_id, f'%{fragment}%'))
        return [(r[0], json.loads(r[1])) for r in rows]

    # ---- 模型尝试 -------------------------------------------------------------
    def job_run(self, job_key: str, attempt: int = 1):
        row = self._one('SELECT status, fencing_token, body FROM shadow_job_runs '
                        'WHERE experiment_id=? AND job_key=? AND attempt=?',
                        (self.experiment_id, job_key, attempt))
        if not row:
            return None
        return {'status': row[0], 'fencing_token': row[1], **(json.loads(row[2]) or {})}

    # ---- 账户 -----------------------------------------------------------------
    def latest_state(self, scope: str):
        row = self._one('SELECT sequence, body FROM shadow_account_state '
                        'WHERE experiment_id=? AND scope=? ORDER BY sequence DESC LIMIT 1',
                        (self.experiment_id, scope))
        return (row[0], json.loads(row[1])) if row else None

    def daily_nav(self, scope: str) -> list:
        return [json.loads(r[0]) for r in self._all(
            'SELECT body FROM shadow_daily_nav WHERE experiment_id=? AND scope=? '
            'ORDER BY session, revision', (self.experiment_id, scope))]

    # ---- 引擎步进事件 ---------------------------------------------------------
    def events(self, scope: str | None = None) -> list:
        if scope is None:
            rows = self._all('SELECT body FROM decision_events WHERE account_scope LIKE ?',
                             (f'SHADOW:{self.experiment_id}%',))
        else:
            rows = self._all('SELECT body FROM decision_events WHERE account_scope=?', (scope,))
        payloads = []
        for r in rows:
            ev = json.loads(r[0])
            if ev.get('event_type') == 'shadow:step':
                payloads.append(ev['payload'])
        return sorted(payloads, key=lambda p: (p.get('_sequence', 0), p.get('_index', 0)))


def ledger_schema_version(path) -> int | None:
    """账本自己声明的 schema 版本（**读出来的**，不是从登记里抄的）。"""
    try:
        with closing(open_ro(path)) as con:
            row = con.execute('SELECT MAX(version) FROM shadow_schema').fetchone()
            return row[0] if row and row[0] is not None else None
    except Exception:
        return None


def experiment_row(path, experiment_id: str):
    """`shadow_experiments` 的一行（`{status, manifest_hash, body}`）。"""
    with closing(open_ro(path)) as con:
        row = con.execute('SELECT status, manifest_hash, body FROM shadow_experiments '
                          'WHERE experiment_id=?', (experiment_id,)).fetchone()
    if not row:
        return None
    return {'status': row[0], 'manifest_hash': row[1], 'manifest': json.loads(row[2])}


def table_counts(path) -> dict:
    """各表行数（诊断折叠区用；只读）。"""
    out = {}
    with closing(open_ro(path)) as con:
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        for n in names:
            out[n] = con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
    return out
