"""Defer 状态机测试（§8.5 / §20.4）。"""
import tempfile
import time
import unittest
from pathlib import Path

from scripts.live_trading.defer_state import DeferStore
from scripts.live_trading.position_registry import PositionRegistry


def _triggers():
    return [
        {'trigger_id': 't1', 'type': 'price_above', 'params': {'price': 101.0}},
        {'trigger_id': 't2', 'type': 'volume_ratio', 'params': {'threshold': 2.0}},
    ]


class DeferStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 's.db', 'DRY-RUN')
        self.store = DeferStore(self.registry)

    def _create(self, expire_at=None, max_reviews=2, signal_id='sig1', decision_id='dec1'):
        return self.store.create(
            signal_id=signal_id, decision_id=decision_id, triggers=_triggers(),
            expire_at=expire_at if expire_at is not None else time.time() + 3600,
            max_reviews=max_reviews)

    def test_create_and_get_roundtrip(self):
        rec = self._create()
        got = self.store.get(rec['defer_id'])
        self.assertEqual(got['status'], 'deferred')
        self.assertEqual(got['trigger_ids'], ['t1', 't2'])
        self.assertEqual(got['review_count'], 0)

    def test_create_is_idempotent(self):
        first = self._create()
        second = self._create()
        self.assertEqual(first['defer_id'], second['defer_id'])
        self.assertEqual(second['created_at'], first['created_at'])

    def test_trigger_hit_consumes_once(self):
        rec = self._create()
        matched = self.store.evaluate(rec['defer_id'], {'price': 102.0})
        self.assertEqual(matched, ['t1'])
        # 消费 t1 → queued
        updated = self.store.trigger(rec['defer_id'], 't1')
        self.assertEqual(updated['status'], 'queued')
        # 再次消费同一 trigger → 拒绝
        again = self.store.trigger(rec['defer_id'], 't1')
        self.assertEqual(again.get('error'), 'status=queued')

    def test_defer_again_until_max_reviews(self):
        rec = self._create(max_reviews=2)
        # 第一次复审后仍延后
        self.store.trigger(rec['defer_id'], 't1')
        r1 = self.store.defer_again(rec['defer_id'])
        self.assertEqual(r1['status'], 'deferred')
        self.assertEqual(r1['review_count'], 1)
        # 第二次复审后达到 max_reviews → rejected
        self.store.trigger(rec['defer_id'], 't2')
        r2 = self.store.defer_again(rec['defer_id'])
        self.assertEqual(r2['status'], 'rejected')
        self.assertEqual(r2['review_count'], 2)

    def test_resolve_and_reject(self):
        rec = self._create()
        self.store.trigger(rec['defer_id'], 't1')
        self.assertEqual(self.store.resolve(rec['defer_id'])['status'], 'resolved')
        # resolved 后不能再 reject
        self.assertEqual(self.store.reject(rec['defer_id']).get('error'), 'status=resolved')

    def test_expire_only_after_deadline(self):
        rec = self._create(expire_at=time.time() + 100)
        # 未到期
        self.assertEqual(self.store.expire(rec['defer_id'], time.time()).get('error'), 'not_expired_yet')
        # 到期后
        r = self.store.expire(rec['defer_id'], time.time() + 200)
        self.assertEqual(r['status'], 'expired')

    def test_expire_due_batch(self):
        self._create(expire_at=time.time() + 1, signal_id='sig1', decision_id='dec1')
        self._create(expire_at=time.time() + 1, signal_id='sig2', decision_id='dec2')
        expired = self.store.expire_due(time.time() + 10)
        self.assertEqual(len(expired), 2)

    def test_queued_review_can_expire(self):
        rec = self._create(expire_at=time.time() + 1)
        self.store.trigger(rec['defer_id'], 't1')
        expired = self.store.expire_due(time.time() + 10)
        self.assertEqual([r['defer_id'] for r in expired], [rec['defer_id']])

    def test_trigger_types(self):
        from scripts.live_trading.defer_state import _trigger_hit
        self.assertTrue(_trigger_hit({'type': 'price_above', 'params': {'price': 100}}, {'price': 101}))
        self.assertTrue(_trigger_hit({'type': 'price_below', 'params': {'price': 100}}, {'price': 99}))
        self.assertTrue(_trigger_hit({'type': 'volume_ratio', 'params': {'threshold': 2}}, {'volume_ratio': 3}))
        self.assertTrue(_trigger_hit({'type': 'option_quality_recovered', 'params': {}}, {'option_quality': 'good'}))
        self.assertTrue(_trigger_hit({'type': 'scheduled_time', 'params': {'at': 100}}, {'now': 200}))
        self.assertTrue(_trigger_hit({'type': 'new_event', 'params': {'event_ids': ['e1']}}, {'new_events': ['e1']}))


if __name__ == '__main__':
    unittest.main()
