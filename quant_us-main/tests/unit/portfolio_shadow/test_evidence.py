"""Evidence Packet 双时间校验 + 质量分级测试（PR5）。"""
import unittest

from scripts.live_trading.decision_ledger.event_store import digest
from scripts.portfolio_shadow.evidence import (MAX_SUMMARY_CHARS, build_entry_packet,
                                               entry_decision_cutoff, entry_response_deadline)
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


class EvidenceReadabilityTests(unittest.TestCase):
    """#3：证据正文必须进包（模型才有判断依据），但要有上限且可审计。"""

    def test_summary_is_preserved(self):
        p = build_entry_packet(opp(), QUOTE, [EVENT], {}, AS_OF)
        self.assertEqual(p['events'][0]['summary'], '公司下调指引')
        self.assertFalse(p['events'][0]['summary_truncated'])

    def test_long_summary_is_capped_but_hashed_in_full(self):
        long_text = 'x' * (MAX_SUMMARY_CHARS + 500)
        # 不给 content_hash，走本地计算路径
        src = {k: v for k, v in EVENT.items() if k != 'content_hash'}
        p = build_entry_packet(opp(), QUOTE, [dict(src, summary=long_text)], {}, AS_OF)
        ev = p['events'][0]
        self.assertEqual(len(ev['summary']), MAX_SUMMARY_CHARS)
        self.assertTrue(ev['summary_truncated'])
        self.assertEqual(ev['content_hash'],
                         digest(long_text))  # 哈希对全文，不因截断而变

    def test_evidence_id_is_content_addressed_not_index_derived(self):
        """前面少一条事件，后面事件的 id 不得错位，否则已记录的 VETO 无法复验。"""
        other = dict(EVENT, summary='另一条', content_hash='zzz',
                     published_at='2026-01-04T01:00:00+00:00',
                     observed_at='2026-01-04T02:00:00+00:00')
        alone = build_entry_packet(opp(), QUOTE, [EVENT], {}, AS_OF)
        paired = build_entry_packet(opp(), QUOTE, [other, EVENT], {}, AS_OF)
        self.assertEqual(alone['events'][0]['evidence_id'],
                         paired['events'][1]['evidence_id'])


class EntryDeadlineTests(unittest.TestCase):
    """截止时刻必须走市场日历（含夏令时/半日市），不能硬编码 T13:20。"""

    def test_decision_cutoff_is_session_close(self):
        # 1 月 = EST(UTC-5) → 16:00 ET = 21:00Z；7 月 = EDT(UTC-4) → 20:00Z
        self.assertTrue(entry_decision_cutoff('2026-01-05').startswith('2026-01-05T21:00'))
        self.assertTrue(entry_decision_cutoff('2026-07-06').startswith('2026-07-06T20:00'))

    def test_response_deadline_is_pre_open_of_exec_session(self):
        # 09:20 ET → EST 14:20Z / EDT 13:20Z
        self.assertTrue(entry_response_deadline('2026-01-06').startswith('2026-01-06T14:20'))
        self.assertTrue(entry_response_deadline('2026-07-07').startswith('2026-07-07T13:20'))


if __name__ == '__main__':
    unittest.main()
