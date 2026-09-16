"""LLM 入场否决 overlay：验证器 + resolve + FakeModel（PR5）。"""
import unittest

from scripts.portfolio_shadow.evidence import build_entry_packet
from scripts.portfolio_shadow.llm_overlay import (FakeModel, OverlayDecision,
                                                  resolve_overlay, validate_model_output)
from scripts.portfolio_shadow.schema import Opportunity, to_micro


def opp():
    return Opportunity(experiment_id='exp1', security_id='SEC-A', source_candidate_id='c1',
                       parent_version='1', signal_session='2026-01-04',
                       observed_at='2026-01-04T21:00:00+00:00',
                       planned_execution_session='2026-01-05', rank=1, entry_rule='b3',
                       stop_reference={'atr14_micro': to_micro(2.0)}, exit_policy_id='H60',
                       input_hash='h', terminal='READY')


AS_OF = '2026-01-05T13:20:00+00:00'
DEADLINE = '2026-01-05T13:20:00+00:00'
QUOTE = {'price': to_micro(100.0), 'observed_at': '2026-01-04T21:00:00+00:00'}
EVENT = {'summary': '公司下调指引', 'source': 'filing', 'kind': 'filing',
         'published_at': '2026-01-04T12:00:00+00:00',
         'observed_at': '2026-01-04T13:00:00+00:00', 'content_hash': 'abc'}


def packet():
    return build_entry_packet(opp(), QUOTE, [EVENT], {}, AS_OF)


def valid_output(p, action='VETO'):
    return {'schema_version': 'entry-veto-v1', 'opportunity_id': p['opportunity_id'],
            'packet_id': p['packet_id'], 'action': action,
            'reason_code': 'MATERIAL_COMPANY_EVENT_RISK',
            'evidence_ids': [p['events'][0]['evidence_id']], 'explanation': ''}


class ValidatorTests(unittest.TestCase):
    def test_valid_veto_with_evidence_passes(self):
        p = packet()
        ok, errors = validate_model_output(valid_output(p), p)
        self.assertTrue(ok, errors)

    def test_valid_pass_requires_no_evidence(self):
        p = packet()
        out = valid_output(p, action='PASS')
        out['reason_code'] = ''
        out['evidence_ids'] = []
        ok, _ = validate_model_output(out, p)
        self.assertTrue(ok)

    def test_opportunity_mismatch_fails(self):
        p = packet()
        out = valid_output(p)
        out['opportunity_id'] = 'wrong'
        ok, errors = validate_model_output(out, p)
        self.assertFalse(ok)
        self.assertIn('OPPORTUNITY_MISMATCH', errors)

    def test_unknown_action_fails(self):
        p = packet()
        out = valid_output(p)
        out['action'] = 'SELL'
        ok, _ = validate_model_output(out, p)
        self.assertFalse(ok)

    def test_veto_without_evidence_fails(self):
        p = packet()
        out = valid_output(p)
        out['evidence_ids'] = []
        ok, errors = validate_model_output(out, p)
        self.assertFalse(ok)
        self.assertIn('VETO_NO_EVIDENCE', errors)

    def test_veto_with_disallowed_reason_fails(self):
        p = packet()
        out = valid_output(p)
        out['reason_code'] = 'INSUFFICIENT_FUNDS'
        ok, errors = validate_model_output(out, p)
        self.assertFalse(ok)
        self.assertIn('REASON_NOT_ALLOWED', errors)


class ResolveOverlayTests(unittest.TestCase):
    def test_veto_applies(self):
        p = packet()
        mr = {'status': 'OK', 'output': valid_output(p), 'completed_at': AS_OF, 'cost_micro': 100}
        d = resolve_overlay(p, mr, DEADLINE)
        self.assertEqual(d.action, 'VETO')
        self.assertEqual(d.model_cost, 100)

    def test_timeout_abstains(self):
        p = packet()
        mr = {'status': 'TIMED_OUT', 'output': None, 'completed_at': None, 'cost_micro': 100}
        d = resolve_overlay(p, mr, DEADLINE)
        self.assertEqual(d.action, 'ABSTAIN')
        self.assertEqual(d.reason_code, 'TIMED_OUT')

    def test_late_response_abstains_and_flags(self):
        p = packet()
        mr = {'status': 'OK', 'output': valid_output(p),
              'completed_at': '2026-01-05T14:00:00+00:00', 'cost_micro': 100}
        d = resolve_overlay(p, mr, DEADLINE)
        self.assertEqual(d.action, 'ABSTAIN')
        self.assertTrue(d.late_response_observed)
        self.assertEqual(d.reason_code, 'LATE_RESPONSE')

    def test_invalid_output_abstains(self):
        p = packet()
        out = valid_output(p)
        out['opportunity_id'] = 'wrong'
        mr = {'status': 'OK', 'output': out, 'completed_at': AS_OF, 'cost_micro': 100}
        d = resolve_overlay(p, mr, DEADLINE)
        self.assertEqual(d.action, 'ABSTAIN')
        self.assertEqual(d.reason_code, 'INVALID_OUTPUT')


class FakeModelTests(unittest.TestCase):
    def test_fake_model_returns_veto(self):
        p = packet()
        m = FakeModel(action='VETO', reason_code='MATERIAL_COMPANY_EVENT_RISK',
                      evidence_ids=[p['events'][0]['evidence_id']])
        d = resolve_overlay(p, m.call(p, DEADLINE), DEADLINE)
        self.assertEqual(d.action, 'VETO')
        self.assertEqual(d.model_cost, 100)


if __name__ == '__main__':
    unittest.main()
