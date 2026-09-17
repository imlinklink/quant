"""R/L 双影子账户存储：独立 SQLite + 复用 event_store 事件协议 + shadow 投影表。

R/L 共享同一 ledger 文件但 scope 隔离。金额/价格在 payload 中一律 int 微美元。
投影表全部可由事件重建；提交 BEGIN IMMEDIATE + expected_sequence，同 event_id 同 payload
幂等，同 id 异 payload 冲突。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from scripts.live_trading.decision_ledger.event_store import (
    canonical, digest, insert_event, make_event, migrate)

from .schema import SHADOW_TERMINALS

# 账本结构版本。任何会改变**已落库事件 payload 或 state_hash 输入**的改动都必须递增，
# 否则旧账本会在续写时报「同 event_id 异内容」或用新哈希误判状态冲突。
#   1 → 初始版本
#   2 → Application 增 cost_uncertain/attempt_id；AccountState 增 model_cost_unsettled
#       （进 state_hash）；新增事件类型 model_cost_settlement / shadow:opportunity_terminal；
#       save_state 白名单放行 missed
#   3 → entry-veto 证据包增 evidence{evidence_mode, source_packet_hash, exclusion 统计} 与
#       model_knowledge_cutoff。这两项进 packet_id ⇒ 进 attempt_id ⇒ 进 Application payload。
#   4 → Opportunity 增 parent_strategy_id/signal_generated_at/decision_deadline/
#       rule_reason_codes/market_snapshot_id（设计 §4），进 shadow:opportunity 事件 payload。
#   5 → 证据截止语义分离：包内 as_of 由「信号日收盘」改为「实际采集时刻」（设计 §3.2），
#       值变 ⇒ packet_id 变 ⇒ attempt_id 变 ⇒ Application 与 shadow:packet 事件 payload 变。
SHADOW_SCHEMA_VERSION = 5

_SHADOW_DDL = '''
CREATE TABLE IF NOT EXISTS shadow_schema(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS shadow_experiments (
    experiment_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_opportunities (
    experiment_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    security_id TEXT NOT NULL,
    session TEXT NOT NULL,
    rank INTEGER NOT NULL,
    terminal TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (experiment_id, opportunity_id));
CREATE TABLE IF NOT EXISTS shadow_applications (
    experiment_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    applied INTEGER NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (experiment_id, scope, opportunity_id));
CREATE TABLE IF NOT EXISTS shadow_account_state (
    experiment_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    state_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (experiment_id, scope, sequence));
CREATE TABLE IF NOT EXISTS shadow_daily_nav (
    experiment_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    session TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    equity INTEGER NOT NULL,
    cash_available INTEGER NOT NULL,
    gross_exposure INTEGER NOT NULL,
    fees INTEGER NOT NULL,
    valuation_status TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (experiment_id, scope, session, revision));
CREATE TABLE IF NOT EXISTS shadow_packets (
    experiment_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    packet_id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (experiment_id, opportunity_id));
CREATE TABLE IF NOT EXISTS shadow_job_runs (
    experiment_id TEXT NOT NULL,
    job_key TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    fencing_token TEXT,
    body TEXT NOT NULL,
    PRIMARY KEY (experiment_id, job_key, attempt));
'''


class LedgerSchemaMismatch(RuntimeError):
    """账本由不同版本的代码写入。影子账本是追加式证据，禁止跨版本续写。"""


class ShadowStore:
    def __init__(self, path: Path, experiment_id: str):
        self.path = Path(path)
        self.experiment_id = experiment_id
        # 实验级事件 scope（机会流）；账户级为 SHADOW:<id>:R / SHADOW:<id>:L
        self.experiment_scope = f'SHADOW:{experiment_id}'

    @contextmanager
    def transaction(self, immediate: bool = True):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=15)
        try:
            migrate(con, self.path)
            con.executescript(_SHADOW_DDL)
            if immediate:
                con.execute('BEGIN IMMEDIATE')
            stored = con.execute('SELECT MAX(version) FROM shadow_schema').fetchone()[0]
            if stored is None:
                con.execute('INSERT INTO shadow_schema(version) VALUES (?)',
                            (SHADOW_SCHEMA_VERSION,))
            elif stored != SHADOW_SCHEMA_VERSION:
                # 事件按稳定 id + payload 哈希追加，改结构必然改变哈希 —— 用新代码续写
                # 旧账本会表现为「同 event_id 异内容」报错，或用新 state_hash 判定旧状态
                # 冲突。这里显式拒绝，逼操作者做明确选择，而不是让实验中途悄悄换了尺子。
                raise LedgerSchemaMismatch(
                    f'LEDGER_SCHEMA_MISMATCH:{self.path}:'
                    f'ledger={stored}:code={SHADOW_SCHEMA_VERSION}:'
                    f'experiment={self.experiment_id}')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    # ---- experiments ----
    def save_experiment(self, manifest) -> None:
        body = json.dumps(manifest.__dict__ if hasattr(manifest, '__dict__') else manifest,
                          ensure_ascii=False, sort_keys=True)
        with self.transaction() as con:
            con.execute('INSERT OR REPLACE INTO shadow_experiments VALUES (?,?,?,?)',
                        (self.experiment_id, manifest.status, manifest.manifest_hash(), body))

    def get_experiment(self) -> dict | None:
        with self.transaction(immediate=False) as con:
            row = con.execute('SELECT body FROM shadow_experiments WHERE experiment_id=?',
                              (self.experiment_id,)).fetchone()
            return json.loads(row[0]) if row else None

    # ---- opportunities ----
    def put_opportunity(self, opp) -> None:
        """写入机会。**首次写入即冻结**：已存在就整体 no-op。

        机会一旦生成就不该被重跑改写（`signal_generated_at` 之类来自墙钟的字段每次
        重跑都不同，改写会变成「同 event_id 异 payload」直接报错；即便不报错，改写
        历史机会本身也是实验纪律所禁止的）。终态变迁走 `set_opportunity_terminal`。
        """
        oid = opp.opportunity_id()
        with self.transaction() as con:
            row = con.execute('SELECT 1 FROM shadow_opportunities WHERE experiment_id=? '
                              'AND opportunity_id=?', (self.experiment_id, oid)).fetchone()
            if row:
                return
            body = json.dumps(_asdict(opp), ensure_ascii=False, sort_keys=True)
            con.execute('INSERT OR REPLACE INTO shadow_opportunities VALUES (?,?,?,?,?,?,?)',
                        (self.experiment_id, oid, opp.security_id, opp.signal_session,
                         opp.rank, opp.terminal, body))
            _insert_event(con, self.experiment_scope, 'shadow:opportunity', oid, _asdict(opp))

    def set_opportunity_terminal(self, opportunity_id: str, terminal: str,
                                 session: str, note: str = '') -> None:
        """机会终态转移（共享漏斗级：READY → EXECUTED / MISSED_EXECUTION）。

        不能走 `put_opportunity`：它按 oid 键事件，改 terminal 会变成「同 event_id 异
        payload」而整事务回滚。这里按 (oid, terminal) 键，同一次转移重放幂等。

        注意投影表 `shadow_opportunities` 是实验级（无 scope 列），所以这里不区分 R/L；
        每账户的 VETO / 引擎 miss 记在 `shadow_applications` 与 step 事件里。
        """
        if terminal not in SHADOW_TERMINALS:
            raise ValueError(f'UNKNOWN_TERMINAL:{terminal}')
        with self.transaction() as con:
            con.execute('UPDATE shadow_opportunities SET terminal=? WHERE experiment_id=? '
                        'AND opportunity_id=?', (terminal, self.experiment_id, opportunity_id))
            _insert_event(con, self.experiment_scope, 'shadow:opportunity_terminal',
                          (opportunity_id, terminal),
                          {'experiment_id': self.experiment_id,
                           'opportunity_id': opportunity_id, 'terminal': terminal,
                           'session': session, 'note': note})

    def opportunity_terminals(self) -> dict:
        """{opportunity_id: terminal}（实验级共享漏斗终态）。"""
        with self.transaction(immediate=False) as con:
            rows = con.execute(
                'SELECT body FROM decision_events WHERE account_scope LIKE ?',
                (f'SHADOW:{self.experiment_id}%',)).fetchall()
            out = {}
            for (body,) in rows:
                ev = json.loads(body)
                if ev.get('event_type') == 'shadow:opportunity_terminal':
                    p = ev['payload']
                    out[p['opportunity_id']] = p['terminal']
            return out

    def opportunities(self) -> list[dict]:
        with self.transaction(immediate=False) as con:
            return [json.loads(r[0]) for r in con.execute(
                'SELECT body FROM shadow_opportunities WHERE experiment_id=? '
                'ORDER BY session, rank, opportunity_id', (self.experiment_id,))]

    # ---- applications ----
    def put_application(self, app) -> None:
        body = json.dumps(_asdict(app), ensure_ascii=False, sort_keys=True)
        with self.transaction() as con:
            con.execute('INSERT OR REPLACE INTO shadow_applications VALUES (?,?,?,?,?,?,?)',
                        (self.experiment_id, app.scope, app.opportunity_id, app.action,
                         app.decision_id, 1 if app.applied else 0, body))
            _insert_event(con, app.scope, 'shadow:application', app.opportunity_id, _asdict(app),
                          decision_id=app.decision_id)

    def application(self, scope: str, opportunity_id: str) -> dict | None:
        with self.transaction(immediate=False) as con:
            row = con.execute('SELECT body FROM shadow_applications WHERE experiment_id=? '
                              'AND scope=? AND opportunity_id=?',
                              (self.experiment_id, scope, opportunity_id)).fetchone()
            return json.loads(row[0]) if row else None

    # ---- frozen evidence packets ----
    def put_packet(self, opportunity_id: str, packet: dict) -> None:
        """首次写入即冻结：同一机会只有一个冻结证据包（设计 §3）。

        重跑必须原样复用已冻结的包 —— 里面的 `as_of` 是实际采集时刻，重算会得到不同
        的值，等于用今天的信息改写当时的决策依据。完整包（不止 packet_hash）落库，
        满足设计 §7「不能只保存 packet_hash 及最终 action」。
        """
        with self.transaction() as con:
            row = con.execute('SELECT 1 FROM shadow_packets WHERE experiment_id=? '
                              'AND opportunity_id=?',
                              (self.experiment_id, opportunity_id)).fetchone()
            if row:
                return
            con.execute('INSERT OR REPLACE INTO shadow_packets VALUES (?,?,?,?)',
                        (self.experiment_id, opportunity_id, packet['packet_id'],
                         json.dumps(packet, ensure_ascii=False, sort_keys=True)))
            _insert_event(con, self.experiment_scope, 'shadow:packet', packet['packet_id'],
                          packet)

    def packet_for_opportunity(self, opportunity_id: str) -> dict | None:
        with self.transaction(immediate=False) as con:
            row = con.execute('SELECT body FROM shadow_packets WHERE experiment_id=? '
                              'AND opportunity_id=?',
                              (self.experiment_id, opportunity_id)).fetchone()
            return json.loads(row[0]) if row else None

    # ---- model attempts（崩溃窗口防护）----
    def put_job_run(self, job_key: str, attempt: int, status: str,
                    body: dict | None = None, fencing_token: str | None = None) -> None:
        """记录一次模型尝试的状态（PENDING → COMPLETED / ABANDONED）。

        调用发生在模型 API 上、结果落在本表之前存在崩溃窗口。先写 PENDING，重启后
        看到 PENDING 就知道「钱可能已经花了且金额不可知」，据此不重试付费，改为把
        该次尝试挂账待补记。COMPLETED 的 body 携带完整决定，使「调用完成但决定未落库」
        的窗口也能恢复而不必重调。
        """
        with self.transaction() as con:
            con.execute('INSERT OR REPLACE INTO shadow_job_runs VALUES (?,?,?,?,?,?)',
                        (self.experiment_id, job_key, attempt, status, fencing_token,
                         json.dumps(body or {}, ensure_ascii=False, sort_keys=True)))

    def job_run(self, job_key: str, attempt: int = 1) -> dict | None:
        with self.transaction(immediate=False) as con:
            row = con.execute('SELECT status, body FROM shadow_job_runs WHERE experiment_id=? '
                              'AND job_key=? AND attempt=?',
                              (self.experiment_id, job_key, attempt)).fetchone()
            return {'status': row[0], **(json.loads(row[1]) or {})} if row else None

    # ---- account state / nav ----
    def save_state(self, scope: str, state, nav: dict, events: list | None = None) -> None:
        """提交一步状态。事务内校验 sequence（设计 §8 expected_sequence）：
        - seq == latest → 幂等重提交（同哈希返回，异哈希冲突）；
        - seq == latest+1 → 正常提交；
        - 否则（倒退 / 跳号）→ 拒绝。防止两进程同时推进同一 session 或乱序覆盖。
        """
        seq = state.sequence
        body = json.dumps(_asdict(state), ensure_ascii=False, sort_keys=True)
        with self.transaction() as con:
            row = con.execute('SELECT MAX(sequence) FROM shadow_account_state '
                              'WHERE experiment_id=? AND scope=?',
                              (self.experiment_id, scope)).fetchone()
            latest = row[0] or 0
            if seq < latest:
                raise ValueError(f'SEQUENCE_CONFLICT:{scope}:seq={seq}<latest={latest}')
            if seq == latest:
                existing = con.execute('SELECT state_hash FROM shadow_account_state '
                                       'WHERE experiment_id=? AND scope=? AND sequence=?',
                                       (self.experiment_id, scope, seq)).fetchone()
                if existing and existing[0] != state.state_hash():
                    raise ValueError(f'SAME_SEQUENCE_DIFFERENT_STATE:{scope}:seq={seq}')
                return  # 幂等重提交，不重复落事件/净值
            if seq != latest + 1:
                raise ValueError(f'SEQUENCE_GAP:{scope}:seq={seq}!=latest+1={latest + 1}')
            for i, e in enumerate(events or []):
                if e['type'] in ('fill', 'split', 'dividend_record', 'dividend_pay', 'settle',
                                 'nav', 'model_cost', 'model_cost_settlement', 'hold', 'missed'):
                    payload = {**e, '_sequence': seq, '_index': i}
                    _insert_event(con, scope, 'shadow:step', (seq, i), payload)
            con.execute('INSERT OR REPLACE INTO shadow_account_state VALUES (?,?,?,?,?)',
                        (self.experiment_id, scope, seq, state.state_hash(), body))
            con.execute('INSERT OR REPLACE INTO shadow_daily_nav VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (self.experiment_id, scope, nav['session'], nav.get('revision', 1),
                         nav['equity'], nav['cash_available'], nav['gross_exposure'],
                         nav['fees'], nav['valuation_status'], seq,
                         json.dumps(nav, ensure_ascii=False, sort_keys=True)))

    def latest_state(self, scope: str) -> tuple[int, dict] | None:
        with self.transaction(immediate=False) as con:
            row = con.execute('SELECT sequence, body FROM shadow_account_state '
                              'WHERE experiment_id=? AND scope=? ORDER BY sequence DESC LIMIT 1',
                              (self.experiment_id, scope)).fetchone()
            return (row[0], json.loads(row[1])) if row else None

    def daily_nav(self, scope: str) -> list[dict]:
        with self.transaction(immediate=False) as con:
            rows = con.execute('SELECT body FROM shadow_daily_nav '
                               'WHERE experiment_id=? AND scope=? ORDER BY session, revision',
                               (self.experiment_id, scope)).fetchall()
            return [json.loads(r[0]) for r in rows]

    def applications(self, scope: str | None = None) -> list[dict]:
        with self.transaction(immediate=False) as con:
            if scope is None:
                rows = con.execute('SELECT body FROM shadow_applications WHERE experiment_id=?',
                                   (self.experiment_id,)).fetchall()
            else:
                rows = con.execute('SELECT body FROM shadow_applications '
                                   'WHERE experiment_id=? AND scope=?',
                                   (self.experiment_id, scope)).fetchall()
            return [json.loads(r[0]) for r in rows]

    # ---- events (for replay) ----
    def events(self, scope: str | None = None) -> list[dict]:
        """返回 step 事件 payload（fill/split/dividend/settle），按 (sequence, index) 排序。"""
        with self.transaction(immediate=False) as con:
            if scope is None:
                rows = con.execute(
                    'SELECT body FROM decision_events WHERE account_scope LIKE ?',
                    (f'SHADOW:{self.experiment_id}%',)).fetchall()
            else:
                rows = con.execute(
                    'SELECT body FROM decision_events WHERE account_scope=?',
                    (scope,)).fetchall()
            payloads = []
            for (body,) in rows:
                ev = json.loads(body)
                if ev.get('event_type') == 'shadow:step':
                    p = ev['payload']
                    payloads.append(p)
            return sorted(payloads, key=lambda p: (p.get('_sequence', 0), p.get('_index', 0)))


def _asdict(obj) -> dict:
    """dataclass -> dict（深拷贝 positions 等可变字段）。"""
    if hasattr(obj, '__dataclass_fields__'):
        return {k: _asdict(v) for k, v in obj.__dict__.items()}
    if isinstance(obj, dict):
        return {k: _asdict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_asdict(v) for v in obj]
    return obj


def _insert_event(con, scope: str, event_type: str, key, payload, **links) -> bool:
    """写入事件。幂等：同 payload 已存在时 insert_event 返回 False（不报错）；
    同 event_id 异 payload 时 insert_event 内部 raise。返回是否新插入。"""
    event = make_event(scope, event_type, key, payload, **links)
    return insert_event(con, event)


def state_from_dict(d: dict):
    from .schema import AccountState, Position
    positions = {sid: Position(**p) for sid, p in d['positions'].items()}
    return AccountState(scope=d['scope'], sequence=d['sequence'],
                        cash_available=d['cash_available'], cash_reserved=d['cash_reserved'],
                        unsettled_cash=d['unsettled_cash'],
                        dividend_receivable=d['dividend_receivable'], positions=positions,
                        fees=d['fees'], model_cost=d['model_cost'],
                        model_cost_unsettled=tuple(d.get('model_cost_unsettled', ())),
                        initial_equity=d['initial_equity'], high_water=d['high_water'],
                        last_session=d['last_session'], valuation_status=d['valuation_status'],
                        risk_state=d.get('risk_state', 'NORMAL'),
                        recovery_streak=d.get('recovery_streak', 0))
