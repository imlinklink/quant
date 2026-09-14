"""Setup shadow 投影；使用现有 append-only 决策账本，不新增独立数据库。"""
from typing import Optional

from .decision_ledger.event_store import EventStore, digest, stable_id


class SetupStore:
    def __init__(self, registry):
        self.events = EventStore(registry)
        self.scope = registry.namespace

    def save_state(self, code: str, snapshot: dict, previous_state: str,
                   state: str, reason_codes: list) -> str:
        key = stable_id('setup_state', self.scope, code, snapshot.get('session'),
                        snapshot.get('feature_version'))
        body = dict(snapshot, code=code, previous_state=previous_state, state=state,
                    reason_codes=list(reason_codes), snapshot_id=key)
        old = self.events.get_snapshot('setup_state', key)
        if old is not None:
            # 重扫时间不同不是新特征；保留首次观察时间及不可变原文。
            comparable = lambda value: {k: v for k, v in value.items() if k != 'as_of'}
            if comparable(old) == comparable(body):
                return key
        with self.events.transaction() as con:
            self.events.snapshot(con, 'setup_state', key, 1, body)
        self.events.record('setup_state_observed', key, body, setup_state_id=key)
        return key

    def save_candidate(self, candidate: dict, state_snapshot_id: str) -> str:
        body = dict(candidate, state_snapshot_id=state_snapshot_id)
        setup_id = candidate['setup_id']
        with self.events.transaction() as con:
            self.events.snapshot(con, 'setup_candidate', setup_id, 1, body)
        self.events.record('setup_candidate_created', setup_id, body,
                           setup_id=setup_id,
                           decision_id=candidate.get('selection_decision_id'))
        return setup_id

    def latest_state(self, code: str, before_session: str = '') -> Optional[dict]:
        import json
        with self.events.transaction() as con:
            rows = con.execute(
                "SELECT body FROM decision_snapshots WHERE account_scope=? "
                "AND kind='setup_state' ORDER BY rowid DESC", (self.scope,)).fetchall()
        for (body,) in rows:
            item = json.loads(body)
            if (item.get('code') == code and
                    (not before_session or str(item.get('session', '')) < before_session)):
                return item
        return None

    def active_candidates(self, code: Optional[str] = None) -> list:
        import json
        with self.events.transaction() as con:
            rows = con.execute(
                "SELECT body FROM decision_snapshots WHERE account_scope=? "
                "AND kind='setup_candidate' ORDER BY rowid DESC", (self.scope,)).fetchall()
        items = [json.loads(r[0]) for r in rows]
        if code:
            items = [i for i in items if i.get('code') == code]
        return [i for i in items if i.get('status') == 'active']


def feature_snapshot_id(snapshot: dict) -> str:
    return digest(snapshot)
