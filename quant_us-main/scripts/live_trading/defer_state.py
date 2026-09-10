"""Defer 状态机（技术设计 §8.5）。

`wait_for_confirmation` 不再是无期限观察状态：每条延后决策有到期时间、最大复审次数、
触发器集合；同一 trigger 只消费一次；复审生成新 input snapshot 与 decision_id，不覆盖旧决策。

状态迁移：
    deferred -> queued   （trigger 命中）
    queued   -> deferred （仍延后，且剩余复审次数）
    queued   -> resolved （execute_now / 最终采纳）
    queued   -> rejected （reject）
    deferred -> expired  （expire_at 到期）
    deferred -> rejected （review_count 达到 max_reviews）

持久化：append-only 事件（review_deferred/review_triggered/review_expired）+ 版本化快照
（kind='deferred_review'），可从事件重放。快照与事件在同一事务提交。
"""
import time
from typing import Any, Dict, List, Optional

from .decision_ledger.event_store import (
    EventStore, insert_event, make_event, stable_id,
)

logger = __import__('logging').getLogger(__name__)

TRIGGER_TYPES = ('price_above', 'price_below', 'volume_ratio', 'new_event',
                 'option_quality_recovered', 'scheduled_time')

SNAPSHOT_KIND = 'deferred_review'


class DeferStore:
    """延后决策的持久化 + 状态机。"""

    def __init__(self, registry):
        self.events = EventStore(registry)
        self.scope = registry.namespace

    # ---------- 创建 / 读取 ----------

    def create(self, *, signal_id: str, decision_id: str,
               triggers: List[Dict[str, Any]], expire_at: float,
               max_reviews: int = 2) -> Dict[str, Any]:
        """创建一条延后决策。triggers: [{trigger_id, type, params}]。"""
        existing_id = stable_id('defer', self.scope, signal_id, decision_id)
        existing = self.get(existing_id)
        if existing is not None:
            return existing
        if not triggers:
            raise ValueError('defer 必须至少一个复审触发器')
        if not signal_id or not decision_id:
            raise ValueError('signal_id / decision_id 不能为空')
        if int(max_reviews) < 1:
            raise ValueError('max_reviews 必须至少为 1')
        trigger_ids = [t.get('trigger_id') for t in triggers]
        if any(not tid for tid in trigger_ids) or len(set(trigger_ids)) != len(trigger_ids):
            raise ValueError('trigger_id 不能为空且不得重复')
        if any(t.get('type') not in TRIGGER_TYPES for t in triggers):
            raise ValueError('包含未知复审触发器类型')
        defer_id = existing_id
        now = time.time()
        if float(expire_at) <= now:
            raise ValueError('expire_at 必须晚于创建时间')
        rec = {
            'defer_id': defer_id,
            'signal_id': signal_id,
            'decision_id': decision_id,
            'trigger_ids': trigger_ids,
            'triggers': {t['trigger_id']: t for t in triggers},
            'consumed_triggers': [],
            'review_count': 0,
            'max_reviews': int(max_reviews),
            'next_review_after': None,
            'expire_at': float(expire_at),
            'status': 'deferred',
            'created_at': now,
            'updated_at': now,
        }
        with self.events.transaction() as con:
            self.events.snapshot(con, SNAPSHOT_KIND, defer_id, 1, rec)
            insert_event(con, make_event(self.scope, 'review_deferred', defer_id, dict(rec),
                                         signal_id=signal_id, decision_id=decision_id))
        return rec

    def get(self, defer_id: str) -> Optional[Dict[str, Any]]:
        return self._load_latest(defer_id)

    def list_deferred(self) -> List[Dict[str, Any]]:
        import json as _json
        with self.events.transaction() as con:
            rows = con.execute(
                'SELECT id, version, body FROM decision_snapshots '
                'WHERE account_scope=? AND kind=?',
                (self.scope, SNAPSHOT_KIND)).fetchall()
        latest: Dict[str, tuple] = {}
        for sid, ver, body in rows:
            if sid not in latest or ver > latest[sid][0]:
                latest[sid] = (ver, _json.loads(body))
        return [rec for _, rec in latest.values() if rec.get('status') == 'deferred']

    # ---------- 触发器 ----------

    def evaluate(self, defer_id: str, market: Dict[str, Any]) -> List[str]:
        """返回命中的（且未消费的）触发器 id 列表。不改变状态。"""
        rec = self.get(defer_id)
        if rec is None or rec.get('status') != 'deferred':
            return []
        matched = []
        for tid in rec['trigger_ids']:
            if tid in rec.get('consumed_triggers', []):
                continue
            t = rec['triggers'].get(tid)
            if t and _trigger_hit(t, market):
                matched.append(tid)
        return matched

    def trigger(self, defer_id: str, trigger_id: str, now: Optional[float] = None) -> Dict[str, Any]:
        """deferred -> queued：消费一个 trigger。返回更新后的记录（非法时返回原记录并带 error）。"""
        now = time.time() if now is None else now
        with self.events.transaction() as con:
            rec = self._load(con, defer_id)
            if rec is None:
                return {'error': 'not_found'}
            if rec.get('status') != 'deferred':
                return dict(rec, error=f'status={rec.get("status")}')
            if trigger_id not in rec.get('trigger_ids', []):
                return dict(rec, error='trigger_not_in_defer')
            if trigger_id in rec.get('consumed_triggers', []):
                return dict(rec, error='trigger_already_consumed')
            rec['consumed_triggers'] = list(rec.get('consumed_triggers', [])) + [trigger_id]
            rec['status'] = 'queued'
            rec['updated_at'] = now
            self._commit(con, rec, 'review_triggered',
                         {'defer_id': defer_id, 'trigger_id': trigger_id},
                         decision_id=rec.get('decision_id'))
            return rec

    def defer_again(self, defer_id: str, now: Optional[float] = None) -> Dict[str, Any]:
        """queued -> deferred（剩余次数）或 rejected（达到 max_reviews）。"""
        now = time.time() if now is None else now
        with self.events.transaction() as con:
            rec = self._load(con, defer_id)
            if rec is None:
                return {'error': 'not_found'}
            if rec.get('status') != 'queued':
                return dict(rec, error=f'status={rec.get("status")}')
            rec['review_count'] = int(rec.get('review_count', 0)) + 1
            if rec['review_count'] >= int(rec.get('max_reviews', 0)):
                rec['status'] = 'rejected'
            else:
                rec['status'] = 'deferred'
            rec['updated_at'] = now
            self._commit(con, rec, 'review_deferred', dict(rec),
                         decision_id=rec.get('decision_id'))
            return rec

    def resolve(self, defer_id: str, now: Optional[float] = None) -> Dict[str, Any]:
        """queued -> resolved（execute_now 被采纳）。"""
        now = time.time() if now is None else now
        with self.events.transaction() as con:
            rec = self._load(con, defer_id)
            if rec is None:
                return {'error': 'not_found'}
            if rec.get('status') != 'queued':
                return dict(rec, error=f'status={rec.get("status")}')
            rec['status'] = 'resolved'
            rec['updated_at'] = now
            self._commit(con, rec, 'review_resolved', {'defer_id': defer_id},
                         decision_id=rec.get('decision_id'))
            return rec

    def reject(self, defer_id: str, now: Optional[float] = None) -> Dict[str, Any]:
        """queued/deferred -> rejected。"""
        now = time.time() if now is None else now
        with self.events.transaction() as con:
            rec = self._load(con, defer_id)
            if rec is None:
                return {'error': 'not_found'}
            if rec.get('status') not in ('queued', 'deferred'):
                return dict(rec, error=f'status={rec.get("status")}')
            rec['status'] = 'rejected'
            rec['updated_at'] = now
            self._commit(con, rec, 'review_rejected', {'defer_id': defer_id},
                         decision_id=rec.get('decision_id'))
            return rec

    def expire(self, defer_id: str, now: Optional[float] = None) -> Dict[str, Any]:
        """deferred/queued -> expired（now >= expire_at）。"""
        now = time.time() if now is None else now
        with self.events.transaction() as con:
            rec = self._load(con, defer_id)
            if rec is None:
                return {'error': 'not_found'}
            if rec.get('status') not in ('deferred', 'queued'):
                return dict(rec, error=f'status={rec.get("status")}')
            if now < float(rec.get('expire_at', 0)):
                return dict(rec, error='not_expired_yet')
            rec['status'] = 'expired'
            rec['updated_at'] = now
            self._commit(con, rec, 'review_expired',
                         {'defer_id': defer_id, 'expire_at': rec['expire_at']},
                         decision_id=rec.get('decision_id'))
            return rec

    def expire_due(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """把到期的 deferred/queued 全部置为 expired。返回受影响记录。"""
        out = []
        for rec in self.list_active():
            r = self.expire(rec['defer_id'], now)
            if r.get('status') == 'expired':
                out.append(r)
        return out

    def list_active(self) -> List[Dict[str, Any]]:
        """列出仍可能到期的 deferred/queued 记录。"""
        import json as _json
        with self.events.transaction() as con:
            rows = con.execute(
                'SELECT id, version, body FROM decision_snapshots '
                'WHERE account_scope=? AND kind=?',
                (self.scope, SNAPSHOT_KIND)).fetchall()
        latest: Dict[str, tuple] = {}
        for sid, ver, body in rows:
            if sid not in latest or ver > latest[sid][0]:
                latest[sid] = (ver, _json.loads(body))
        return [rec for _, rec in latest.values()
                if rec.get('status') in ('deferred', 'queued')]

    # ---------- 内部 ----------

    def _load(self, con, defer_id):
        row = con.execute(
            'SELECT body FROM decision_snapshots WHERE account_scope=? AND kind=? AND id=? '
            'ORDER BY version DESC LIMIT 1',
            (self.scope, SNAPSHOT_KIND, defer_id)).fetchone()
        if not row:
            return None
        import json as _json
        return _json.loads(row[0])

    def _load_latest(self, defer_id):
        with self.events.transaction() as con:
            return self._load(con, defer_id)

    def _commit(self, con, rec, event_type, payload, **links):
        """同一事务内：写版本化快照 + 追加事件（事件 key 含版本，保证 append-only）。"""
        row = con.execute(
            'SELECT MAX(version) FROM decision_snapshots WHERE account_scope=? AND kind=? AND id=?',
            (self.scope, SNAPSHOT_KIND, rec['defer_id'])).fetchone()
        version = (row[0] or 0) + 1
        self.events.snapshot(con, SNAPSHOT_KIND, rec['defer_id'], version, rec)
        insert_event(con, make_event(self.scope, event_type, [rec['defer_id'], version],
                                     payload, **links))


def _trigger_hit(trigger: Dict[str, Any], market: Dict[str, Any]) -> bool:
    """按类型判断触发器是否命中。market 是程序计算的行情/事件快照。"""
    typ = trigger.get('type')
    params = trigger.get('params') or {}
    if typ == 'price_above':
        return market.get('price') is not None and float(market['price']) > float(params.get('price', 0))
    if typ == 'price_below':
        return market.get('price') is not None and float(market['price']) < float(params.get('price', float('inf')))
    if typ == 'volume_ratio':
        return market.get('volume_ratio') is not None and float(market['volume_ratio']) > float(params.get('threshold', 0))
    if typ == 'option_quality_recovered':
        return market.get('option_quality') == 'good'
    if typ == 'scheduled_time':
        return float(market.get('now', 0)) >= float(params.get('at', 0))
    if typ == 'new_event':
        wanted = set(params.get('event_ids', []))
        return bool(wanted & set(market.get('new_events', [])))
    return False
