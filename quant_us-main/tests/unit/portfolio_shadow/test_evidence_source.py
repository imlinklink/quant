"""证据来源适配器测试（设计 §5.2）。

最关键的一条：`observed_at` 必须是**系统实际入库时间**，不由导入文件追溯指定 ——
点对点可得性要证明的是「我们当时确实看得到」，这件事只有采集管道知道。
"""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.portfolio_shadow.evidence_source import (JsonlEvidenceSource,
                                                      import_evidence_jsonl)

INGEST = '2026-01-05T22:00:00+00:00'
CUTOFF = '2026-01-05T23:00:00+00:00'


def row(sid='SEC-A', *, published='2026-01-04T12:00:00Z', **extra):
    return {'security_id': sid, 'event_type': 'filing', 'summary': '公司下调全年指引',
            'excerpt': '原文摘录', 'source_url': 'file://x', 'source_type': 'filing',
            'published_at': published, 'quality_status': 'verified', **extra}


def build(tmp, rows, *, ingested_at=INGEST, window_days=30, max_events=50):
    src = Path(tmp) / 'raw.jsonl'
    src.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows), encoding='utf-8')
    store = Path(tmp) / 'evidence.csv'
    import_evidence_jsonl(src, store, ingested_at=ingested_at)
    return JsonlEvidenceSource(store, window_days=window_days, max_events=max_events)


class ImportSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_observed_at_is_ingest_time_not_the_file_claim(self):
        """文件自述的观测时间不能当作我们的观测证据。"""
        source = build(self.tmp, [row(claimed_observed_at='2015-01-01T00:00:00Z')])
        fetch = source.load_events('SEC-A', CUTOFF)
        self.assertEqual(fetch.status, 'OK')
        self.assertEqual(len(fetch.events), 1)
        self.assertEqual(fetch.events[0]['observed_at'], INGEST)
        self.assertNotEqual(fetch.events[0]['observed_at'], '2015-01-01T00:00:00Z')

    def test_file_claim_is_kept_as_separate_metadata(self):
        # 声称值仍保留在存储里（可审计），只是不参与可得性判定
        source = build(self.tmp, [row(claimed_observed_at='2015-01-01T00:00:00Z')])
        stored = source._records()
        self.assertIn('claimed_observed_at', stored.columns)
        self.assertEqual(stored.iloc[0]['claimed_observed_at'], '2015-01-01T00:00:00Z')

    def test_ingest_time_is_stable_across_repeated_reads(self):
        source = build(self.tmp, [row()])
        first = source.load_events('SEC-A', CUTOFF).events[0]['observed_at']
        second = source.load_events('SEC-A', CUTOFF).events[0]['observed_at']
        self.assertEqual(first, second)

    def test_missing_fields_do_not_leak_nan_strings(self):
        """回归：float('nan') 是 truthy 的，`str(x or '')` 会把缺失字段变成字面量 'nan'。"""
        source = build(self.tmp, [row(source_url=None, excerpt=None)])
        event = source.load_events('SEC-A', CUTOFF).events[0]
        for key, value in event.items():
            self.assertNotIn(str(value).lower(), ('nan', 'nat'), f'{key} 泄漏了缺失值')


class WindowAndCapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_window_excludes_events_older_than_the_configured_days(self):
        # cutoff 2026-01-05，窗口 30 天 → 2025-11-01 的事件在窗外
        source = build(self.tmp, [row(published='2025-11-01T12:00:00Z'),
                                  row(published='2026-01-02T12:00:00Z')],
                       window_days=30)
        events = source.load_events('SEC-A', CUTOFF).events
        self.assertEqual([e['published_at'][:10] for e in events], ['2026-01-02'])

    def test_capacity_truncation_is_recorded_not_silent(self):
        """设计 §5.1：预算不足不得伪装成「没有风险」。"""
        rows = [row(published=f'2026-01-0{i}T12:00:00Z') for i in (1, 2, 3)]
        source = build(self.tmp, rows, max_events=2)
        fetch = source.load_events('SEC-A', CUTOFF)
        self.assertEqual(len(fetch.events), 2)
        self.assertEqual(fetch.meta['exclusion_reasons'].get('CAPACITY_TRUNCATED'), 1)
        # 保留最近的两条
        self.assertEqual([e['published_at'][:10] for e in fetch.events],
                         ['2026-01-02', '2026-01-03'])

    def test_invalid_window_policy_is_rejected(self):
        src = Path(self.tmp) / 'raw.jsonl'
        src.write_text(json.dumps(row()), encoding='utf-8')
        store = Path(self.tmp) / 'e.csv'
        import_evidence_jsonl(src, store, ingested_at=INGEST)
        with self.assertRaises(ValueError):
            JsonlEvidenceSource(store, window_days=0, max_events=10)


class FailureModesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_missing_store_is_failed_not_silently_empty(self):
        source = JsonlEvidenceSource(Path(self.tmp) / 'nope.csv')
        fetch = source.load_events('SEC-A', CUTOFF)
        self.assertEqual(fetch.status, 'FAILED')
        self.assertTrue(fetch.error)
        self.assertFalse(fetch.is_usable)

    def test_invalid_cutoff_is_failed(self):
        source = build(self.tmp, [row()])
        fetch = source.load_events('SEC-A', 'not-a-time')
        self.assertEqual(fetch.status, 'FAILED')
        self.assertIn('CUTOFF_INVALID', fetch.error)

    def test_no_matching_events_is_empty_not_failed(self):
        source = build(self.tmp, [row(sid='SEC-A')])
        fetch = source.load_events('SEC-OTHER', CUTOFF)
        self.assertEqual(fetch.status, 'EMPTY')
        self.assertFalse(fetch.is_usable)

    def test_import_rejects_rows_without_security_id_or_published_at(self):
        src = Path(self.tmp) / 'raw.jsonl'
        src.write_text(json.dumps(row(published=None)), encoding='utf-8')
        with self.assertRaises(ValueError) as ctx:
            import_evidence_jsonl(src, Path(self.tmp) / 'e.csv', ingested_at=INGEST)
        self.assertIn('EVIDENCE_PUBLISHED_AT_MISSING', str(ctx.exception))
