"""决策运行存储（PR2，技术设计 §11/§13）。

职责：
  - 保存 DecisionRun 与 model attempt 的查询投影（llm_decision_runs / llm_model_attempts）；
  - 记录版本化快照（selection_input/entry_input/position_input / model_raw_response /
    validated_decision / quality_gate / permission_snapshot）；
  - 保证「输入先于模型调用持久化」「模型成功但保存失败不产生动作」的调用方顺序由
    DecisionEngine 负责，本模块只提供原子写入。
"""
import json
import logging
import time
from typing import Any, Dict, Optional

from .event_store import EventStore, digest, utc

logger = logging.getLogger(__name__)

from scripts.live_trading.decision_contracts import ROLE_CONTRACTS

# **从契约表派生，绝不另抄一份**：这里原先是硬编码的 ('selection','entry','position')，
# 于是新增角色时引擎能路由、快照层却报「非法 role」—— 两处定义漂移不会报错，只会让新
# 角色静默不可用。单一来源由测试钉死。
VALID_ROLES = tuple(ROLE_CONTRACTS)
SNAPSHOT_KIND_INPUT = {
    'selection': 'selection_input',
    'entry': 'entry_input',
    'position': 'position_input',
    'portfolio': 'portfolio_input',
    'review': 'review_input',
}


def finalize_decision_id(*, account_scope, role, subject_id, input_snapshot_id,
                         versions: Dict[str, str], model_id: str) -> str:
    """按 §4.1 生成绑定冻结输入的 decision_id（含 input_snapshot_id 内容哈希）。"""
    from .event_store import stable_id
    return stable_id(
        'decision', account_scope, role, subject_id, input_snapshot_id,
        versions.get('prompt', ''), versions.get('output_schema', ''), model_id)


def build_context(*, role, subject_type, subject_id, account_scope, as_of,
                  versions: Dict[str, str], model: Dict[str, Any],
                  market_session: str = 'closed',
                  input_snapshot_id: str = '') -> Dict[str, Any]:
    """构造 DecisionContext（技术设计 §4.1）。

    input_snapshot_id 为空时生成「临时」decision_id；最终 decision_id 必须在
    输入快照落库后由 DecisionEngine 用 finalize_decision_id 重算并回填，
    保证 decision_id 绑定冻结输入（相同输入+版本 → 相同 id）。
    """
    if role not in VALID_ROLES:
        raise ValueError(f'非法 role: {role}')
    ctx = {
        'role': role,
        'subject_type': subject_type,
        'subject_id': subject_id,
        'account_scope': account_scope,
        'as_of': as_of,
        'market_session': market_session,
        'versions': versions,
        'model': model,
    }
    # 无 input_snapshot_id 时 decision_id 为空（临时）；由 DecisionEngine 在快照落库后回填。
    ctx['decision_id'] = (
        finalize_decision_id(account_scope=account_scope, role=role, subject_id=subject_id,
                             input_snapshot_id=input_snapshot_id, versions=versions,
                             model_id=model.get('model_id', ''))
        if input_snapshot_id else '')
    return ctx


class DecisionRunStore:
    """决策运行的投影 + 快照写入。"""

    def __init__(self, registry):
        self.events = EventStore(registry)
        self.scope = registry.namespace

    # ---------- 快照（不可覆盖） ----------

    def save_input_snapshot(self, role: str, subject_id: str, packet: Dict[str, Any],
                            version: int = 1) -> str:
        """保存冻结输入快照，返回 input_snapshot_id。"""
        kind = SNAPSHOT_KIND_INPUT.get(role)
        if kind is None:
            raise ValueError(f'未知 role: {role}')
        snapshot_id = digest({'kind': kind, 'subject_id': subject_id,
                              'packet': packet, 'version': version})
        with self.events.transaction() as con:
            self.events.snapshot(con, kind, snapshot_id, version, packet)
        return snapshot_id

    def save_snapshot(self, kind: str, key: str, payload: Dict[str, Any],
                      version: int = 1) -> str:
        """保存任意版本化快照（model_raw_response/validated_decision/...）。

        按 key 存储，get_snapshot 用同一 key 读取（对称）。返回 key。
        """
        with self.events.transaction() as con:
            self.events.snapshot(con, kind, key, version, payload)
        return key

    def get_snapshot(self, kind: str, key: str, version: int = 1):
        return self.events.get_snapshot(kind, key, version)

    def next_snapshot_version(self, kind: str, key: str) -> int:
        """返回该 (kind, key) 下一个可用快照版本（重试时递增，避免不可覆盖冲突）。"""
        with self.events.transaction() as con:
            row = con.execute(
                'SELECT MAX(version) FROM decision_snapshots '
                'WHERE account_scope=? AND kind=? AND id=?',
                (self.scope, kind, key)).fetchone()
            return (row[0] or 0) + 1

    def get_snapshot_latest(self, kind: str, key: str):
        """读取该 (kind, key) 最新版本的快照。"""
        import json as _json
        with self.events.transaction() as con:
            row = con.execute(
                'SELECT body FROM decision_snapshots '
                'WHERE account_scope=? AND kind=? AND id=? ORDER BY version DESC LIMIT 1',
                (self.scope, kind, key)).fetchone()
            return _json.loads(row[0]) if row else None

    # ---------- 投影行 ----------

    def save_run(self, run: Dict[str, Any]) -> None:
        """upsert 一条 llm_decision_runs 投影（幂等 by decision_id）。"""
        required = ('decision_id', 'role', 'subject_type', 'subject_id', 'as_of',
                    'status', 'input_snapshot_id', 'prompt_version',
                    'output_schema_version', 'feature_version', 'rule_version',
                    'permission_version', 'provider', 'model_id', 'created_at')
        for k in required:
            if k not in run:
                raise ValueError(f'DecisionRun 缺字段: {k}')
        cols = ['account_scope', 'decision_id', 'role', 'subject_type', 'subject_id',
                'as_of', 'status', 'input_snapshot_id', 'prompt_version',
                'output_schema_version', 'feature_version', 'rule_version',
                'permission_version', 'provider', 'model_id',
                'selected_attempt_id', 'effective_action', 'created_at']
        row = {c: run.get(c) for c in cols}
        row['account_scope'] = self.scope
        sql = ('INSERT OR REPLACE INTO llm_decision_runs (' + ','.join(cols) + ') VALUES (' +
               ','.join('?' for _ in cols) + ')')
        with self.events.transaction() as con:
            con.execute(sql, [row[c] for c in cols])

    def save_attempt(self, attempt: Dict[str, Any]) -> None:
        """insert 一条 llm_model_attempts 投影（attempt 不可覆盖）。"""
        required = ('attempt_id', 'decision_id', 'status')
        for k in required:
            if k not in attempt:
                raise ValueError(f'ModelAttempt 缺字段: {k}')
        cols = ['account_scope', 'attempt_id', 'decision_id', 'started_at',
                'completed_at', 'status', 'raw_response', 'parsed_response',
                'validation_errors', 'latency_ms', 'input_tokens', 'output_tokens']
        row = {c: attempt.get(c) for c in cols}
        row['account_scope'] = self.scope
        # 序列化可能带 dict 的字段
        for c in ('raw_response', 'parsed_response', 'validation_errors'):
            v = row.get(c)
            if isinstance(v, (dict, list)):
                row[c] = json.dumps(v, ensure_ascii=False, default=str)
        sql = ('INSERT OR REPLACE INTO llm_model_attempts (' + ','.join(cols) + ') VALUES (' +
               ','.join('?' for _ in cols) + ')')
        with self.events.transaction() as con:
            con.execute(sql, [row[c] for c in cols])

    # ---------- 查询投影（供 replay / metrics） ----------

    def get_run(self, decision_id: str) -> Optional[Dict[str, Any]]:
        with self.events.transaction() as con:
            cols = ['account_scope', 'decision_id', 'role', 'subject_type', 'subject_id',
                    'as_of', 'status', 'input_snapshot_id', 'prompt_version',
                    'output_schema_version', 'feature_version', 'rule_version',
                    'permission_version', 'provider', 'model_id',
                    'selected_attempt_id', 'effective_action', 'created_at']
            row = con.execute(
                'SELECT ' + ','.join(cols) + ' FROM llm_decision_runs '
                'WHERE account_scope=? AND decision_id=?', (self.scope, decision_id)).fetchone()
            return dict(zip(cols, row)) if row else None

    def list_runs(self, role: Optional[str] = None, limit: int = 100) -> list:
        with self.events.transaction() as con:
            sql = ('SELECT decision_id,role,subject_type,subject_id,as_of,status,'
                   'input_snapshot_id,prompt_version,output_schema_version,effective_action,'
                   'provider,model_id FROM llm_decision_runs WHERE account_scope=?')
            args = [self.scope]
            if role:
                sql += ' AND role=?'
                args.append(role)
            sql += ' ORDER BY as_of DESC LIMIT ?'
            args.append(limit)
            return [dict(zip(('decision_id', 'role', 'subject_type', 'subject_id', 'as_of',
                              'status', 'input_snapshot_id', 'prompt_version',
                              'output_schema_version', 'effective_action',
                              'provider', 'model_id'), r))
                    for r in con.execute(sql, args).fetchall()]
