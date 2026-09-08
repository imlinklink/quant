"""Evidence Packet（阶段 F1）回归：可复现、数据质量显式标记、证据不可变。"""
import unittest

from scripts.live_trading.decision_ledger.evidence_packet import (
    build_data_quality, build_evidence_packet,
)


class EvidencePacketContracts(unittest.TestCase):
    def _args(self, **overrides):
        args = dict(
            code='US.A', name='A', market='US', sector='semis', risk_group='semis',
            quote={'price': 100, 'observed_at': '2026-09-04T10:00:00+00:00'},
            events=[{'summary': '公司发布业绩', 'source': 'filing',
                     'published_at': '2026-09-03T00:00:00+00:00', 'kind': 'filing'}],
            now='2026-09-04T10:00:00+00:00',
        )
        args.update(overrides)
        return args

    def test_packet_reproducible(self):
        p1 = build_evidence_packet(**self._args())
        p2 = build_evidence_packet(**self._args())
        self.assertEqual(p1['packet_id'], p2['packet_id'])

    def test_packet_differs_on_different_input(self):
        p1 = build_evidence_packet(**self._args())
        p2 = build_evidence_packet(**self._args(code='US.B'))
        self.assertNotEqual(p1['packet_id'], p2['packet_id'])

    def test_data_quality_marks_missing_and_future(self):
        dq = build_data_quality(
            {'price': None},
            [{'summary': 'x', 'source': 'news', 'published_at': '2027-01-01T00:00:00+00:00'}],
            {'revenue_change': None},
            '2026-09-04T10:00:00+00:00',
        )
        self.assertFalse(dq['ok'])
        self.assertIn('quote.price', dq['checks']['missing'])
        self.assertIn('fundamentals.revenue_change', dq['checks']['missing'])
        self.assertTrue(dq['checks']['future'])  # 2027 年事件相对 now 是未来

    def test_events_get_evidence_ids(self):
        p = build_evidence_packet(**self._args())
        self.assertTrue(all(e.get('evidence_id') for e in p['events']))
        self.assertTrue(all(e.get('content_hash') for e in p['events']))

    def test_missing_quote_flagged_not_defaulted(self):
        p = build_evidence_packet(**self._args(quote=None, events=[]))
        self.assertFalse(p['data_quality']['ok'])
        self.assertIn('quote.price', p['data_quality']['checks']['missing'])


if __name__ == '__main__':
    unittest.main()
