"""历史证据快照确定性测试（技术设计 §3.6）。"""
import unittest
from datetime import timedelta

import pandas as pd

from scripts.evidence.evidence_store import (build_packet, content_hash, market_close,
                                             market_time, normalize_evidence, packet_hash,
                                             validate_labels)

D = '2022-03-10'          # 周四
CUTOFF = market_close(D).isoformat()


def rec(**over):
    base = {'security_id': 'SEC-A', 'symbol_as_published': 'US.A', 'kind': 'filing',
            'source_id': 'src1', 'source_record_id': 'r1', 'source_url_or_archive_path': 'archive://1',
            'event_at': '2022-02-01T00:00:00Z', 'published_at': '2022-03-09T21:05:00Z',
            'observed_at': '2022-03-09T21:06:00Z', 'ingested_at': '2026-09-12T00:00:00Z',
            'version_id': 'v1', 'supersedes_id': None, 'content_hash': 'body-v1',
            'summary_hash': 's1', 'quality_status': 'verified',
            'availability_proof': 'source timestamp + archive snapshot',
            'license_tag': 'research-retention-allowed'}
    base.update(over)
    return base


def records(*rows):
    return normalize_evidence(pd.DataFrame(list(rows)))


class EvidenceStoreTests(unittest.TestCase):
    def test_announcement_after_close_waits_for_next_cutoff(self):
        announced = market_time(D, 16, 5)                       # 收盘后 16:05 ET 发布
        data = records(rec(event_at=market_time(D, 14).isoformat(),
                           published_at=announced.isoformat(),
                           observed_at=(announced + timedelta(minutes=1)).isoformat()))
        packet, exclusions = build_packet(data, 'SEC-A', CUTOFF)
        self.assertEqual(packet['events'], [])
        self.assertEqual(exclusions[0]['reason'], 'FUTURE_PUBLICATION')
        later, _ = build_packet(data, 'SEC-A', market_close(pd.Timestamp(D) + timedelta(days=1)).isoformat())
        self.assertEqual(len(later['events']), 1)               # 下一决策时点可见

    def test_event_past_but_publication_future_rejected(self):
        data = records(rec(event_at='2020-01-01T00:00:00Z', published_at='2023-01-01T00:00:00Z',
                           observed_at='2023-01-01T00:00:00Z'))
        _, exclusions = build_packet(data, 'SEC-A', '2022-01-01T00:00:00Z')
        self.assertEqual(exclusions[0]['reason'], 'FUTURE_PUBLICATION')

    def test_publication_past_but_observation_future_rejected(self):
        data = records(rec(published_at='2021-01-01T00:00:00Z', observed_at='2023-01-01T00:00:00Z'))
        _, exclusions = build_packet(data, 'SEC-A', '2022-01-01T00:00:00Z')
        self.assertEqual(exclusions[0]['reason'], 'FUTURE_OBSERVATION')

    def test_missing_observed_at_strict_rejects_diagnostic_allows(self):
        data = records(rec(observed_at=None))
        _, exclusions = build_packet(data, 'SEC-A', CUTOFF)
        self.assertEqual(exclusions[0]['reason'], 'OBSERVED_AT_UNPROVEN')
        packet, _ = build_packet(data, 'SEC-A', CUTOFF, policy={'require_observed_at': False})
        self.assertEqual(len(packet['events']), 1)

    def test_revision_does_not_override_original(self):
        data = records(
            rec(version_id='v1', content_hash='body-v1', source_record_id='r1',
                observed_at='2022-03-01T00:00:00Z'),
            rec(version_id='v2', supersedes_id='ev1', content_hash='body-v2', source_record_id='r1',
                observed_at='2022-06-01T00:00:00Z'))
        early, exc1 = build_packet(data, 'SEC-A', '2022-04-01T00:00:00Z')
        self.assertEqual([e['kind'] for e in early['events']], ['filing'])   # 仍是 v1
        self.assertTrue(any(e['reason'] == 'FUTURE_OBSERVATION' for e in exc1))
        late, exc2 = build_packet(data, 'SEC-A', '2022-07-01T00:00:00Z')
        self.assertEqual(len(late['events']), 1)
        self.assertTrue(any(e['reason'] == 'REVISED_AFTER_CUTOFF' for e in exc2))  # v1 被取代

    def test_symbol_resolution_and_ambiguity(self):
        def resolver(symbol, when):
            return ({'security_id': 'SEC-A', 'status': 'resolved'} if symbol == 'US.A'
                    else {'security_id': None, 'status': 'ambiguous'})
        data = records(rec(security_id='SEC-OTHER', symbol_as_published='US.A'),
                       rec(source_record_id='r2', security_id='SEC-OTHER',
                           symbol_as_published='US.AMB'))
        packet, exclusions = build_packet(data, 'SEC-A', CUTOFF, resolver=resolver)
        self.assertEqual(len(packet['events']), 1)              # US.A 正确归属
        self.assertTrue(any(e['reason'] == 'SYMBOL_AMBIGUOUS' for e in exclusions))

    def test_reprints_count_as_one_cluster(self):
        same = rec(source_record_id='r1', content_hash='same')
        data = records(same,
                       dict(same, observed_at='2022-03-09T21:07:00Z'),
                       dict(same, observed_at='2022-03-09T21:08:00Z'))
        packet, exclusions = build_packet(data, 'SEC-A', CUTOFF)
        self.assertEqual(packet['event_clusters'], 1)
        self.assertEqual(len(packet['events']), 1)
        self.assertTrue(all(e['reason'] == 'DUPLICATE_EVENT' for e in exclusions))

    def test_timezone_dst_weekend_and_half_day(self):
        self.assertEqual(market_close('2022-01-03').hour, 21)   # 冬令时 16:00 EST
        self.assertEqual(market_close('2022-07-05').hour, 20)   # 夏令时 16:00 EDT
        self.assertEqual(market_close('2022-07-05', half_day=True).hour, 17)  # 半日市 13:00 ET
        self.assertEqual(market_close('2022-03-12').hour, 21)   # 周末仍可计算（调用方须给交易日）

    def test_packet_hash_stable_and_sensitive(self):
        data = records(rec())
        p1, _ = build_packet(data, 'SEC-A', CUTOFF)
        p2, _ = build_packet(data, 'SEC-A', CUTOFF)
        self.assertEqual(p1['packet_hash'], p2['packet_hash'])
        self.assertEqual(p1['packet_hash'], packet_hash({k: v for k, v in p1.items()
                                                         if k != 'packet_hash'}))
        changed = records(rec(content_hash='body-v2'))
        p3, _ = build_packet(changed, 'SEC-A', CUTOFF)
        self.assertNotEqual(p1['packet_hash'], p3['packet_hash'])
        moved = records(rec(observed_at='2022-03-09T22:00:00Z'))
        p4, _ = build_packet(moved, 'SEC-A', CUTOFF)
        self.assertNotEqual(p1['packet_hash'], p4['packet_hash'])

    def test_label_citing_outside_packet_fails(self):
        packet, _ = build_packet(records(rec()), 'SEC-A', CUTOFF)
        inside = packet['events'][0]['evidence_id']
        ok = validate_labels([{'setup_id': 's1', 'packet_hash': packet['packet_hash'],
                               'llm_decision': 'candidate', 'cited_evidence_ids': [inside]}],
                             {'s1': packet})
        self.assertEqual(ok, [])
        bad = validate_labels([{'setup_id': 's1', 'packet_hash': packet['packet_hash'],
                                'llm_decision': 'candidate',
                                'cited_evidence_ids': ['ev_not_in_packet']}], {'s1': packet})
        self.assertTrue(any(e.startswith('CITATION_OUTSIDE_PACKET') for e in bad))

    def test_missing_decision_not_treated_as_choice(self):
        packet, _ = build_packet(records(rec()), 'SEC-A', CUTOFF)
        errors = validate_labels([{'setup_id': 's1', 'packet_hash': packet['packet_hash'],
                                   'llm_decision': 'missing'}], {'s1': packet})
        self.assertTrue(any(e.startswith('MISSING_TREATED_AS_DECISION') for e in errors))

    def test_cutoff_monotonicity(self):
        announced = market_time(D, 16, 5)
        data = records(rec(published_at=announced.isoformat(),
                           observed_at=(announced + timedelta(minutes=1)).isoformat()))
        t = pd.Timestamp(announced).tz_convert('UTC')
        early, _ = build_packet(data, 'SEC-A', (t - timedelta(minutes=1)).isoformat())
        late, _ = build_packet(data, 'SEC-A', t.isoformat())
        early_ids = {e['evidence_id'] for e in early['events']}
        late_ids = {e['evidence_id'] for e in late['events']}
        self.assertTrue(early_ids <= late_ids)                  # 单调：前移不新增未来事件
        self.assertEqual(early_ids, set())


if __name__ == '__main__':
    unittest.main()
