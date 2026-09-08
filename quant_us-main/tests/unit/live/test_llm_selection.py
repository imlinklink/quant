"""LLM 选股影子排序（首个迭代）回归：越权/引用/失败/空列表/无订单副作用。"""
import unittest

from mutifactor.llm.selection_review import validate_selection
from scripts.live_trading.decision_ledger.evidence_packet import build_evidence_packet
from scripts.live_trading.llm_selection import rank


class _FakeAdvisor:
    def __init__(self, raw=None, enabled=True, model='fake'):
        self.raw = raw
        self.enabled = enabled
        self.model = model

    def chat(self, prompt, system=None):
        return self.raw


def _packet(code):
    return build_evidence_packet(
        code, quote={'price': 100, 'observed_at': '2026-09-04T10:00:00+00:00'},
        events=[{'summary': '公司发布业绩', 'source': 'filing',
                 'published_at': '2026-09-03T00:00:00+00:00', 'kind': 'filing'}],
        now='2026-09-04T10:00:00+00:00',
    )


def _candidate(code, catalyst=(), counter=()):
    return {'code': code, 'rank': 1, 'horizon': '1-5_sessions', 'thesis': 'x',
            'catalyst_evidence_ids': list(catalyst), 'counterevidence_ids': list(counter),
            'preferred_entry_mode': 'none', 'watch_conditions': [], 'invalidators': [],
            'confidence_bucket': 'medium', 'missing_information': []}


class SelectionContracts(unittest.TestCase):
    def setUp(self):
        self.universe = ['US.A', 'US.B']
        self.packets = [_packet(c) for c in self.universe]

    def test_validate_selection_rejects_out_of_universe(self):
        raw = {'candidates': [_candidate('US.OUT')]}
        with self.assertRaisesRegex(ValueError, '越权'):
            validate_selection(raw, self.universe, self.packets)

    def test_validate_selection_rejects_bad_reference(self):
        raw = {'candidates': [_candidate('US.A', catalyst=['unknown-id'])]}
        with self.assertRaisesRegex(ValueError, '引用'):
            validate_selection(raw, self.universe, self.packets)

    def test_validate_selection_accepts_valid_reference(self):
        eid = self.packets[0]['events'][0]['evidence_id']
        raw = {'candidates': [_candidate('US.A', catalyst=[eid])]}
        self.assertEqual(len(validate_selection(raw, self.universe, self.packets)), 1)

    def test_rank_llm_failed_records_error(self):
        batch = rank(_FakeAdvisor(raw=None), self.universe, self.packets)
        self.assertEqual(batch['error'], 'llm_failed')
        self.assertEqual(batch['candidates'], [])

    def test_rank_llm_disabled(self):
        batch = rank(_FakeAdvisor(enabled=False), self.universe, self.packets)
        self.assertEqual(batch['error'], 'llm_disabled')

    def test_rank_empty_candidates_ok(self):
        batch = rank(_FakeAdvisor(raw={'candidates': []}), self.universe, self.packets)
        self.assertIsNone(batch['error'])
        self.assertEqual(batch['candidates'], [])
        self.assertIsNotNone(batch['research_batch_id'])

    def test_rank_out_of_universe_no_order_side_effect(self):
        raw = {'candidates': [_candidate('US.OUT')]}
        batch = rank(_FakeAdvisor(raw=raw), self.universe, self.packets)
        self.assertIn('validate_failed', batch['error'])
        self.assertNotIn('proposal_id', batch)
        self.assertNotIn('order_id', batch)

    def test_research_batch_persisted(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from scripts.live_trading.llm_suggestions import store as sstore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        b1 = {'research_batch_id': 'b1', 'universe_hash': 'h', 'candidates': []}
        with patch.object(sstore, 'RESEARCH_BATCH_PATH', Path(tmp.name) / 'batches.jsonl'):
            sstore.save_research_batch(b1)
            sstore.save_research_batch(dict(b1, research_batch_id='b2'))
            batches = sstore.load_research_batches()
            self.assertEqual(len(batches), 2)
            self.assertEqual(sstore.load_latest_research_batch()['research_batch_id'], 'b2')


if __name__ == '__main__':
    unittest.main()
