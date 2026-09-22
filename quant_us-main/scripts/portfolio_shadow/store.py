"""R/L 双影子账户存储：独立 SQLite + 复用 event_store 事件协议 + shadow 投影表。

R/L 共享同一 ledger 文件但 scope 隔离。金额/价格在 payload 中一律 int 微美元。
投影表全部可由事件重建；提交 BEGIN IMMEDIATE + expected_sequence，同 event_id 同 payload
幂等，同 id 异 payload 冲突。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from scripts.live_trading.decision_ledger.event_store import (
    canonical, digest, insert_event, make_event, migrate)

from .schema import (ATTEMPT_STATUSES, ATTEMPT_TERMINAL, MANIFEST_STATUSES,
                     SHADOW_TERMINALS)


def _parse_iso(value):
    """ISO 时间串 → datetime；解析失败返回 None。

    不要用字符串直接比大小：带微秒与不带微秒的 ISO 串在分隔符处（`.` vs `+`）排序会错。
    """
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def _shift_iso(value, seconds: int) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        raise ValueError(f'TIMESTAMP_INVALID:{value}')
    return (parsed + timedelta(seconds=seconds)).isoformat()

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
#   6 → 证据源接入：事件字段做缺失值清洗（NaN → None，原先会变成字面量 'nan'）、
#       data_quality.fetch_status 取值扩展（OK/EMPTY/FAILED/NOT_CONFIGURED）、
#       evidence.meta 增窗口/容量截断统计。
#   7 → 编排落地（设计 §7）：Application 的 applied 拆成 decision_frozen/execution_applied，
#       新增 shadow:execution_applied 事件；shadow_job_runs 状态机改为
#       PREPARED/CALL_STARTED/COMPLETED/FAILED/TIMED_OUT/UNKNOWN。
#   8 → 尝试状态词汇对齐：shadow_job_runs.status 由模型侧词汇改为状态机取值
#       （'OK' → 'COMPLETED'），body 增 model_status 保留原始词汇；put_job_run 增两道守卫。
#   9 → 冻结身份补 start_session（manifest_hash 的输入变了，旧账本记录的哈希一律不匹配）；
#       save_experiment 拒绝同 id 不同配置；运行时新增 verify_manifest_frozen 校验。
#  10 → 机械利润保护（预登记 EXIT-PROTECT-20260921）：Position 增
#       initial_risk_micro / highest_completed_close_micro / protection_activated /
#       pending_stop_micro / pending_stop_effective_session / protection_version，
#       且**全部进 state_hash**；新增 `protection_state`（每持仓每 session 无条件写，
#       否则重放重建不出 H）与 `stop_update_applied`（T+1 实际抬线的时刻）两类 step 事件。
#       ⇒ 本版本的代码**读不了** 9 及更早的账本，反之亦然（这是刻意：用新代码解读
#       旧状态得出的持仓保护线会是错的）。已冻结的 012 只读不写，不受影响。
SHADOW_SCHEMA_VERSION = 10

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
    execution_applied INTEGER NOT NULL,
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
        """登记实验。**同 id 同配置幂等；同 id 不同配置拒绝**。

        原实现用 `INSERT OR REPLACE`，于是「重新 freeze」能悄悄把原哈希覆盖掉 ——
        参数变更必须新建实验（新 experiment_id），不能顶着同一个身份改尺子。
        """
        incoming = manifest.manifest_hash()
        with self.transaction() as con:
            row = con.execute('SELECT manifest_hash FROM shadow_experiments '
                              'WHERE experiment_id=?', (self.experiment_id,)).fetchone()
            if row is not None and row[0] != incoming:
                raise ValueError(
                    f'EXPERIMENT_ALREADY_FROZEN:{self.experiment_id}:'
                    f'stored={row[0][:16]}:incoming={incoming[:16]}'
                    '（参数变更必须新建 experiment_id，不能改已有实验）')
            body = json.dumps(_asdict(manifest), ensure_ascii=False, sort_keys=True)
            con.execute('INSERT OR REPLACE INTO shadow_experiments VALUES (?,?,?,?)',
                        (self.experiment_id, manifest.status, incoming, body))

    def get_experiment(self) -> dict | None:
        with self.transaction(immediate=False) as con:
            row = con.execute('SELECT body FROM shadow_experiments WHERE experiment_id=?',
                              (self.experiment_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def frozen_manifest_hash(self) -> str | None:
        """账本里记录的冻结哈希；未冻结返回 None。"""
        with self.transaction(immediate=False) as con:
            row = con.execute('SELECT manifest_hash FROM shadow_experiments '
                              'WHERE experiment_id=?', (self.experiment_id,)).fetchone()
            return row[0] if row else None

    def set_experiment_status(self, status: str, note: str = '') -> None:
        """运行状态变更（暂停/恢复/关闭）。**单独记录、不参与冻结身份** ——

        `manifest_hash` 只含不可变字段，否则暂停一次就会让实验看起来「被改过」。
        """
        if status not in MANIFEST_STATUSES:
            raise ValueError(f'UNKNOWN_EXPERIMENT_STATUS:{status}')
        with self.transaction() as con:
            row = con.execute('SELECT body FROM shadow_experiments WHERE experiment_id=?',
                              (self.experiment_id,)).fetchone()
            if row is None:
                raise ValueError(f'EXPERIMENT_NOT_FROZEN:{self.experiment_id}')
            body = {**json.loads(row[0]), 'status': status}
            con.execute('UPDATE shadow_experiments SET status=?, body=? WHERE experiment_id=?',
                        (status, json.dumps(body, ensure_ascii=False, sort_keys=True),
                         self.experiment_id))
            _insert_event(con, self.experiment_scope, 'shadow:experiment_status',
                          (status, note),
                          {'experiment_id': self.experiment_id, 'status': status, 'note': note})

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

    def opportunity_rows(self) -> list[tuple[str, dict]]:
        """(opportunity_id, body)。机会体里不含**计算出来**的 id，故单独给出。"""
        with self.transaction(immediate=False) as con:
            return [(r[0], json.loads(r[1])) for r in con.execute(
                'SELECT opportunity_id, body FROM shadow_opportunities WHERE experiment_id=? '
                'ORDER BY session, rank, opportunity_id', (self.experiment_id,))]

    def opportunity(self, opportunity_id: str) -> dict | None:
        with self.transaction(immediate=False) as con:
            row = con.execute('SELECT body FROM shadow_opportunities WHERE experiment_id=? '
                              'AND opportunity_id=?',
                              (self.experiment_id, opportunity_id)).fetchone()
            return json.loads(row[0]) if row else None

    # ---- applications ----
    def put_application(self, app) -> None:
        body = json.dumps(_asdict(app), ensure_ascii=False, sort_keys=True)
        with self.transaction() as con:
            con.execute('INSERT OR REPLACE INTO shadow_applications VALUES (?,?,?,?,?,?,?)',
                        (self.experiment_id, app.scope, app.opportunity_id, app.action,
                         app.decision_id, 1 if app.execution_applied else 0, body))
            _insert_event(con, app.scope, 'shadow:application', app.opportunity_id, _asdict(app),
                          decision_id=app.decision_id)

    def application(self, scope: str, opportunity_id: str) -> dict | None:
        with self.transaction(immediate=False) as con:
            row = con.execute('SELECT body FROM shadow_applications WHERE experiment_id=? '
                              'AND scope=? AND opportunity_id=?',
                              (self.experiment_id, scope, opportunity_id)).fetchone()
            return json.loads(row[0]) if row else None

    def executed_opportunities(self, scope: str, session: str) -> set:
        """该 session 已**落库**的入场成交对应的 opportunity_id。

        崩溃恢复时用它补齐归因，而不是拿内存里的 intents 猜 —— 内存状态在崩溃那一刻就没了。
        """
        return {e.get('opportunity_id') for e in self.events(scope)
                if e.get('type') == 'fill' and e.get('session') == session
                and e.get('side') == 'BUY' and e.get('reason') == 'ENTRY'
                and e.get('opportunity_id')}

    def mark_execution_applied(self, scope: str, opportunity_id: str, session: str) -> None:
        """标记该账户动作已在执行日结算（设计 §7：动作冻结 ≠ 成交）。

        单独键事件（(opportunity_id, session)）—— 不能走 `put_application`：那是按
        opportunity_id 键的，改 execution_applied 会变成「同 event_id 异 payload」。
        """
        with self.transaction() as con:
            row = con.execute('SELECT body FROM shadow_applications WHERE experiment_id=? '
                              'AND scope=? AND opportunity_id=?',
                              (self.experiment_id, scope, opportunity_id)).fetchone()
            if row is None:
                raise ValueError(f'APPLICATION_MISSING:{scope}:{opportunity_id}')
            body = {**json.loads(row[0]), 'execution_applied': True}
            # 列与 body 必须一起更新：`application()` 读的是 body，只改列会让两者不一致
            con.execute('UPDATE shadow_applications SET execution_applied=1, body=? '
                        'WHERE experiment_id=? AND scope=? AND opportunity_id=?',
                        (json.dumps(body, ensure_ascii=False, sort_keys=True),
                         self.experiment_id, scope, opportunity_id))
            _insert_event(con, scope, 'shadow:execution_applied', (opportunity_id, session),
                          {'experiment_id': self.experiment_id, 'scope': scope,
                           'opportunity_id': opportunity_id, 'session': session})

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

    def raw_events(self, event_type: str) -> list:
        """按类型读**任意实验级事件的 payload**。

        `events()` 只返回 `shadow:step`（引擎步进事件）；反事实、机会终态等由
        `EventStore.record` 写的实验级事件不在其中，需要这个方法才能读到。
        """
        with self.transaction(immediate=False) as con:
            rows = con.execute(
                'SELECT body FROM decision_events WHERE account_scope LIKE ? '
                'AND event_type=? ORDER BY observed_at',
                (f'SHADOW:{self.experiment_id}%', event_type)).fetchall()
        return [json.loads(row[0])['payload'] for row in rows]

    def packets_matching(self, fragment: str) -> list[tuple[str, dict]]:
        """按主体键**片段**取冻结包，返回 [(主体键, 包)]，按键排序。

        持仓评审的主体键是 `{opportunity_id}@pos:{执行日}`：评审命令按执行日取回本日
        待评审的仓位，不需要重放行情去反推「昨天收盘时持有什么」—— 冻结包本身就是那份
        记录，重推反而可能因为复权/数据修订而算出不同的一组主体。

        用包含匹配而非严格后缀：按执行日取时片段是 `@pos:{ISO 会话日}`（本身就是键的后缀），
        统计时片段是 `@pos:`。键的格式固定为 `{id}@pos:{ISO 日期}`，故 `@pos:{某日期}`
        不可能出现在另一个日期的键里，包含匹配不会误取。
        """
        if not fragment:
            raise ValueError('FRAGMENT_REQUIRED')
        with self.transaction(immediate=False) as con:
            rows = con.execute(
                'SELECT opportunity_id, body FROM shadow_packets WHERE experiment_id=? '
                'AND opportunity_id LIKE ? ORDER BY opportunity_id',
                (self.experiment_id, f'%{fragment}%')).fetchall()
            return [(r[0], json.loads(r[1])) for r in rows]

    # ---- model attempts（设计 §7：原子领取 + 尝试状态机 + 租约）----
    def prepare_job_run(self, job_key: str, attempt: int = 1,
                        body: dict | None = None) -> None:
        """登记一次尝试，但**不覆盖已有记录**。

        用 `INSERT OR IGNORE` 而非 REPLACE：覆盖会把 CALL_STARTED 连同它的租约一起抹掉，
        于是重启后的接管者会以为自己是首次调用 —— 崩溃证据被"登记"这一步自己销毁了。
        """
        with self.transaction() as con:
            con.execute('INSERT OR IGNORE INTO shadow_job_runs VALUES (?,?,?,?,?,?)',
                        (self.experiment_id, job_key, attempt, 'PREPARED', None,
                         json.dumps(body or {}, ensure_ascii=False, sort_keys=True)))

    def put_job_run(self, job_key: str, attempt: int, status: str,
                    body: dict | None = None, fencing_token: str | None = None) -> None:
        """写入尝试状态。两道守卫，都是为了让「已付费的有效结果」不会被冲掉：

        - 状态必须在状态机取值内 —— 传入别的词汇（如模型侧的 'OK'）会让记录看起来
          从未终结，重跑时被判为崩溃遗留而覆盖；
        - 已终态的记录不得被改写成非终态。
        """
        if status not in ATTEMPT_STATUSES:
            raise ValueError(f'UNKNOWN_ATTEMPT_STATUS:{status}（允许：{list(ATTEMPT_STATUSES)}）')
        with self.transaction() as con:
            row = con.execute('SELECT status FROM shadow_job_runs WHERE experiment_id=? '
                              'AND job_key=? AND attempt=?',
                              (self.experiment_id, job_key, attempt)).fetchone()
            if (row and row[0] in ATTEMPT_TERMINAL and status not in ATTEMPT_TERMINAL):
                raise ValueError(f'ATTEMPT_ALREADY_TERMINAL:{job_key}:{row[0]}->{status}')
            con.execute('INSERT OR REPLACE INTO shadow_job_runs VALUES (?,?,?,?,?,?)',
                        (self.experiment_id, job_key, attempt, status, fencing_token,
                         json.dumps(body or {}, ensure_ascii=False, sort_keys=True)))

    def job_run(self, job_key: str, attempt: int = 1) -> dict | None:
        with self.transaction(immediate=False) as con:
            row = con.execute('SELECT status, fencing_token, body FROM shadow_job_runs '
                              'WHERE experiment_id=? AND job_key=? AND attempt=?',
                              (self.experiment_id, job_key, attempt)).fetchone()
            if not row:
                return None
            return {'status': row[0], 'fencing_token': row[1],
                    **(json.loads(row[2]) or {})}

    def claim_attempt(self, job_key: str, *, now: str, lease_seconds: int,
                      body: dict | None = None, attempt: int = 1) -> str:
        """原子领取一次模型尝试（设计 §7）。整个检查+写入在同一事务内完成。

        返回：
        - `'claimed'`        ：首次领取（本机会从未调用过）→ 调用方可以发起网络请求；
        - `'already_started'`：**租约尚未到期**，另一个 worker 正在调用 → 不得重复调用；
        - `'abandoned'`      ：租约已到期且无结果（进程在发送后崩溃）→ **不盲目重发**，
          按设计 §7 判为 UNKNOWN 并以 ABSTAIN 冻结；
        - `'finalized'`      ：已进入终态 → 直接读取结果，绝不再次调用。

        `fencing_token` 存**租约到期时刻**（`now + lease_seconds`）。判据是
        `到期时刻 > now`，**不是**与 `now + 新租期` 比较 —— 后者等价于拿「本次开始
        时刻」和「now」比，会在租约远未到期时把正常调用误判成崩溃遗留。
        三种「已存在」情形必须分开返回：把过期租约也当成 `claimed` 会把崩溃后的重启
        变成一次静默重发。
        """
        lease_until = _shift_iso(now, lease_seconds)
        now_dt = _parse_iso(now)
        with self.transaction() as con:            # BEGIN IMMEDIATE：写者串行化
            row = con.execute('SELECT status, fencing_token FROM shadow_job_runs '
                              'WHERE experiment_id=? AND job_key=? AND attempt=?',
                              (self.experiment_id, job_key, attempt)).fetchone()
            if row is None:
                con.execute('INSERT OR REPLACE INTO shadow_job_runs VALUES (?,?,?,?,?,?)',
                            (self.experiment_id, job_key, attempt, 'CALL_STARTED',
                             lease_until,
                             json.dumps(body or {}, ensure_ascii=False, sort_keys=True)))
                return 'claimed'
            status, lease = row
            if status in ATTEMPT_TERMINAL or status == 'PREPARED':
                if status == 'PREPARED':           # 已登记但从未领取
                    con.execute('UPDATE shadow_job_runs SET status=?, fencing_token=?, body=? '
                                'WHERE experiment_id=? AND job_key=? AND attempt=?',
                                ('CALL_STARTED', lease_until,
                                 json.dumps(body or {}, ensure_ascii=False, sort_keys=True),
                                 self.experiment_id, job_key, attempt))
                    return 'claimed'
                return 'finalized'
            lease_dt = _parse_iso(lease)
            if lease_dt is not None and now_dt is not None and lease_dt > now_dt:
                return 'already_started'           # 租约仍有效：别的 worker 在调用
            return 'abandoned'                     # 租约过期且无结果：崩溃遗留

    # ---- account state / nav ----
    def save_state(self, scope: str, state, nav: dict, events: list | None = None, *,
                   applied_marks=None, session: str | None = None) -> None:
        """提交一步状态。事务内校验 sequence（设计 §8 expected_sequence）：
        - seq == latest → 幂等重提交（同哈希返回，异哈希冲突）；
        - seq == latest+1 → 正常提交；
        - 否则（倒退 / 跳号）→ 拒绝。防止两进程同时推进同一 session 或乱序覆盖。

        `applied_marks` 与状态**同事务**提交（设计 §7：动作冻结 ≠ 成交，但冻结与成交的
        对应关系不能断）。分两次提交会留下永久的决策—成交归因缺口：账户已成交而标记未写
        时崩溃，重跑时 `step` 返回 no-op 直接跳过，那个缺口再也补不上。幂等重提交分支也
        会补做标记 —— 上次正好崩在这一步的话，重跑就是修复。
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
                # 幂等重提交：不重复落事件/净值，但**要补做标记**（上次可能崩在两步之间）
                _apply_marks(con, self.experiment_id, scope, applied_marks, session)
                return
            if seq != latest + 1:
                raise ValueError(f'SEQUENCE_GAP:{scope}:seq={seq}!=latest+1={latest + 1}')
            for i, e in enumerate(events or []):
                if e['type'] in ('fill', 'split', 'dividend_record', 'dividend_pay', 'settle',
                                 'nav', 'model_cost', 'model_cost_settlement', 'hold', 'missed',
                                 'protection_state', 'stop_update_applied'):
                    payload = {**e, '_sequence': seq, '_index': i}
                    _insert_event(con, scope, 'shadow:step', (seq, i), payload)
            con.execute('INSERT OR REPLACE INTO shadow_account_state VALUES (?,?,?,?,?)',
                        (self.experiment_id, scope, seq, state.state_hash(), body))
            con.execute('INSERT OR REPLACE INTO shadow_daily_nav VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                        (self.experiment_id, scope, nav['session'], nav.get('revision', 1),
                         nav['equity'], nav['cash_available'], nav['gross_exposure'],
                         nav['fees'], nav['valuation_status'], seq,
                         json.dumps(nav, ensure_ascii=False, sort_keys=True)))
            # 与状态同一事务：不留「已成交但未标记」的窗口
            _apply_marks(con, self.experiment_id, scope, applied_marks, session)

    def model_cost_so_far(self, scope: str) -> tuple[int, int]:
        """该账户**已计入**的模型成本与**金额未知**的尝试数（规划 §4.1 的调用预算用）。

        未知金额记 0 并挂账待补记，所以 `spent` 会**滞后于真实花费** —— 调用方必须把
        第二个返回值一并披露，不能拿它当准确账单。
        """
        row = self.latest_state(scope)
        if not row:
            return 0, 0
        body = row[1]
        return int(body.get('model_cost') or 0), len(body.get('model_cost_unsettled') or ())

    def in_flight_attempts(self, scope: str) -> int:
        """**正在飞**的模型尝试数（CALL_STARTED 且未终结）—— 调用预算必须预留它们。

        只看已结算成本会让 N 个持仓在同一轮里**同时**通过预算检查：每个都以为「剩下的钱够」，
        于是总额穿透上限。预留的量由调用方按 `model_call_reserve_micro` 折算。
        """
        with self.transaction(immediate=False) as con:
            rows = con.execute(
                "SELECT body FROM shadow_job_runs WHERE experiment_id=? AND status='CALL_STARTED'",
                (self.experiment_id,)).fetchall()
        count = 0
        for (body,) in rows:
            try:
                if json.loads(body).get('scope') == scope:
                    count += 1
            except (TypeError, ValueError):
                count += 1        # 读不懂的尝试按「在飞」算，宁可早停不可穿透
        return count

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


def _apply_marks(con, experiment_id: str, scope: str, marks, session) -> None:
    """在**调用方的事务内**应用「动作已应用到执行」标记（幂等）。

    mark 形如 {'opportunity_id': str, 'create': Application | None}：
    已有 Application 则标记 execution_applied；没有且给了 create 则补写一条
    （R 侧无模型决策，按设计 §6 补 INTENT_CREATED），列与 body 一起更新。
    """
    for mark in marks or ():
        oid = mark['opportunity_id']
        row = con.execute('SELECT body FROM shadow_applications WHERE experiment_id=? '
                          'AND scope=? AND opportunity_id=?',
                          (experiment_id, scope, oid)).fetchone()
        create = mark.get('create')
        if row is None and create is None:
            continue
        if row is None:
            con.execute('INSERT OR REPLACE INTO shadow_applications VALUES (?,?,?,?,?,?,?)',
                        (experiment_id, scope, oid, create.action, create.decision_id, 1,
                         json.dumps(_asdict(create), ensure_ascii=False, sort_keys=True)))
        else:
            body = {**json.loads(row[0]), 'execution_applied': True}
            con.execute('UPDATE shadow_applications SET execution_applied=1, body=? '
                        'WHERE experiment_id=? AND scope=? AND opportunity_id=?',
                        (json.dumps(body, ensure_ascii=False, sort_keys=True),
                         experiment_id, scope, oid))
        _insert_event(con, scope, 'shadow:execution_applied', (oid, session),
                      {'experiment_id': experiment_id, 'scope': scope,
                       'opportunity_id': oid, 'session': session})


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
