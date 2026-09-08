"""建议池时效与来源（任务 B）回归：时区/未来/陈旧/损坏 JSON/原子写。"""
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.live_trading.llm_suggestions import freshness, store


class FreshnessParsing(unittest.TestCase):
    def test_timezone_aware_new(self):
        now = time.time()
        r = freshness.parse_time(datetime.now(timezone.utc).isoformat(), now)
        self.assertEqual(r['status'], 'ok')
        self.assertIsNotNone(r['epoch'])

    def test_naive_time_marked_unknown_tz_not_utc(self):
        # 无时区旧记录：不默认为 UTC，标 unknown_tz
        r = freshness.parse_time('2026-09-04T10:00:00')
        self.assertEqual(r['status'], 'unknown_tz')

    def test_future_time(self):
        now = time.time()
        r = freshness.parse_time(datetime.now(timezone.utc) + timedelta(days=1), now)
        self.assertEqual(r['status'], 'future')

    def test_invalid_and_missing(self):
        self.assertEqual(freshness.parse_time('not-a-time')['status'], 'invalid')
        self.assertEqual(freshness.parse_time(None)['status'], 'missing')
        self.assertEqual(freshness.parse_time('')['status'], 'missing')


class FreshnessAssess(unittest.TestCase):
    def test_stale_generated(self):
        now = time.time()
        old = datetime.fromtimestamp(now - 10 * 86400, timezone.utc).isoformat()
        out = freshness.assess({'generated_at': old, 'reports': {}, 'candidates': []}, {}, now)
        self.assertEqual(out['_freshness'], 'stale')

    def test_fresh_generated_but_stale_source(self):
        now = time.time()
        fresh_gen = datetime.fromtimestamp(now, timezone.utc).isoformat()
        old_src = now - 10 * 86400
        data = {'generated_at': fresh_gen,
                'reports': {'pre_mtime': old_src, 'post_mtime': None},
                'candidates': []}
        out = freshness.assess(data, {}, now)
        self.assertEqual(out['_freshness'], 'fresh')
        self.assertEqual(out['_sources']['pre']['fresh'], 'stale')
        self.assertEqual(out['_sources']['post']['status'], 'missing')

    def test_naive_generated_kept_unknown(self):
        now = time.time()
        data = {'generated_at': '2026-09-04T10:00:00', 'reports': {}, 'candidates': []}
        out = freshness.assess(data, {}, now)
        self.assertEqual(out['_generated']['status'], 'unknown_tz')


class StoreAtomic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        patcher = patch.object(store, 'LATEST_PATH', self.p / 'us_latest.json')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_atomic_write_leaves_no_tmp_and_reads_back(self):
        store.save_latest({'generated_at': 'x', 'candidates': [{'id': '1'}]})
        self.assertFalse((self.p / 'us_latest.json.tmp').exists())
        self.assertEqual(store.load_latest()['candidates'][0]['id'], '1')

    def test_update_item_status_atomic(self):
        store.save_latest({'generated_at': 'x', 'candidates': [{'id': 'sug-1', 'status': 'pending'}]})
        self.assertTrue(store.update_item_status('sug-1', 'added'))
        self.assertEqual(store.load_latest()['candidates'][0]['status'], 'added')
        self.assertFalse((self.p / 'us_latest.json.tmp').exists())

    def test_corrupt_json_falls_back_to_default(self):
        (self.p / 'us_latest.json').write_text('{broken', encoding='utf-8')
        data = store.load_latest()
        self.assertEqual(data.get('candidates'), [])
        self.assertIsNone(data.get('generated_at'))


if __name__ == '__main__':
    unittest.main()
