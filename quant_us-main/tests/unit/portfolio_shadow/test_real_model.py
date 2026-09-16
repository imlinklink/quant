"""真实 LLM 模型 RealModel 测试（mock advisor，无真实 API 调用）。"""
import unittest
from unittest.mock import Mock

from scripts.portfolio_shadow.evidence import build_entry_packet
from scripts.portfolio_shadow.llm_overlay import RealModel, resolve_overlay
from scripts.portfolio_shadow.schema import Opportunity, to_micro


def opp():
    return Opportunity(experiment_id='exp1', security_id='SEC-A', source_candidate_id='c1',
                       parent_version='1', signal_session='2026-01-04',
                       observed_at='2026-01-04T21:00:00+00:00',
                       planned_execution_session='2026-01-05', rank=1, entry_rule='b3',
                       stop_reference={'atr14_micro': to_micro(2.0)}, exit_policy_id='H60',
                       input_hash='h', terminal='READY')


QUOTE = {'price': to_micro(100.0), 'observed_at': '2026-01-04T21:00:00+00:00'}
EVENT = {'summary': '公司下调指引', 'source': 'filing', 'kind': 'filing',
         'published_at': '2026-01-04T12:00:00+00:00',
         'observed_at': '2026-01-04T13:00:00+00:00', 'content_hash': 'abc'}
DEADLINE = '2026-01-05T13:20:00+00:00'


class RealModelTests(unittest.TestCase):
    def _packet(self):
        return build_entry_packet(opp(), QUOTE, [EVENT], {}, DEADLINE)

    def test_call_maps_ok_and_cost(self):
        p = self._packet()
        advisor = Mock()
        advisor.last_metadata = {'cost_usd': 0.00123}
        advisor.chat.return_value = {'schema_version': 'entry-veto-v1',
                                     'opportunity_id': p['opportunity_id'],
                                     'packet_id': p['packet_id'], 'action': 'VETO',
                                     'reason_code': 'MATERIAL_COMPANY_EVENT_RISK',
                                     'evidence_ids': [p['events'][0]['evidence_id']],
                                     'explanation': ''}
        mr = RealModel(advisor).call(p, DEADLINE)
        self.assertEqual(mr['status'], 'OK')
        self.assertEqual(mr['output']['action'], 'VETO')
        self.assertEqual(mr['cost_micro'], 1230)
        # prompt 含 entry_veto + opportunity_id
        prompt = advisor.chat.call_args[0][0]
        self.assertIn('entry_veto', prompt)
        self.assertIn(p['opportunity_id'], prompt)

    def test_chat_none_maps_to_failed(self):
        p = self._packet()
        advisor = Mock()
        advisor.last_metadata = {}
        advisor.chat.return_value = None
        mr = RealModel(advisor).call(p, DEADLINE)
        self.assertEqual(mr['status'], 'FAILED')
        self.assertIsNone(mr['output'])
        self.assertEqual(mr['cost_micro'], 0)
        # resolve_overlay 把 FAILED 降级 ABSTAIN
        d = resolve_overlay(p, mr, DEADLINE)
        self.assertEqual(d.action, 'ABSTAIN')
        self.assertEqual(d.reason_code, 'FAILED')


if __name__ == '__main__':
    unittest.main()
