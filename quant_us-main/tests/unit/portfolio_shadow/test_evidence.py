"""Evidence Packet 双时间校验 + 质量分级测试（PR5）。"""
import unittest

from scripts.portfolio_shadow.evidence import build_entry_packet
from scripts.portfolio_shadow.schema import Opportunity, to_micro


def opp():
    return Opportunity(experiment_id='exp1', security_id='SEC-A', source_candidate_id='c1',
                       parent_version='1', signal_session='2026-01-04',
                       observed_at='2026-01-04T21:00:00+00:00',
                       planned_execution_session='2026-01-05', rank=1, entry_rule='b3',
                       stop_reference={'atr14_micro': to_micro(2.0)}, exit_policy_id='H60',
                       input_hash='h', terminal='READY')


AS_OF = '2026-01-05T13:20:00+00:00'
QUOTE = {'price': to_micro(100.0), 'observed_at': '2026-01-04T21:00:00+00:00'}
EVENT = {'summary': '公司下调指引', 'source': 'filing', 'kind': 'filing',
         'published_at': '2026-01-04T12:00:00+00:00',
         'observed_at': '2026-01-04T13:00:00+00:00',
         'content_hash': 'abc'}


class EvidenceQualityTests(unittest.TestCase):
    def test_ok_with_quote_and_event(self):
        p = build_entry_packet(opp(), QUOTE, [EVENT], {}, AS_OF)
        self.assertEqual(p['data_quality']['level'], 'OK')
        self.assertTrue(p['packet_id'].startswith('entry_packet_'))

    def test_block_on_missing_quote_price(self):
        p = build_entry_packet(opp(), {'price': None, 'observed_at': QUOTE['observed_at']},
                               [EVENT], {}, AS_OF)
        self.assertEqual(p['data_quality']['level'], 'BLOCK')

    def test_block_on_future_quote_observed(self):
        p = build_entry_packet(opp(), {'price': to_micro(100), 'observed_at': '2026-01-06T00:00:00+00:00'},
                               [EVENT], {}, AS_OF)
        self.assertEqual(p['data_quality']['level'], 'BLOCK')

    def test_llm_insufficient_when_no_events_or_fundamentals(self):
        p = build_entry_packet(opp(), QUOTE, [], {}, AS_OF)
        self.assertEqual(p['data_quality']['level'], 'LLM_INSUFFICIENT')

    def test_event_with_future_time_is_dropped(self):
        future_event = dict(EVENT, observed_at='2026-01-06T00:00:00+00:00')
        p = build_entry_packet(opp(), QUOTE, [future_event], {}, AS_OF)
        self.assertEqual(p['data_quality']['dropped_event_count'], 1)
        self.assertEqual(p['events'], [])

    def test_packet_id_deterministic(self):
        a = build_entry_packet(opp(), QUOTE, [EVENT], {}, AS_OF)
        b = build_entry_packet(opp(), QUOTE, [EVENT], {}, AS_OF)
        self.assertEqual(a['packet_id'], b['packet_id'])


if __name__ == '__main__':
    unittest.main()
