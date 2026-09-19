"""Position Decision v2 契约测试（§9，技术设计 §20.5）。"""
import time
import unittest

from mutifactor.llm.contracts.common import build_evidence_item
from mutifactor.llm.contracts.position_v2 import (
    build_position_action_templates, legacy_thesis_state, transition_thesis,
    validate_position_v2,
)


def _ev(eid, summary, subject='US.AAPL', kind='fundamental', cluster='c1'):
    return build_evidence_item(
        evidence_id=eid, subject_code=subject, kind=kind, source='internal:test',
        source_grade=2, summary=summary, observed_at=time.time(), cluster_id=cluster)


def _packet(templates, evidence=(), remaining=100.0, active_stop=90.0):
    return {
        'trade': {'trade_id': 't1', 'code': 'US.AAPL', 'direction': 'long',
                  'remaining_qty': remaining, 'entry_price': 100.0},
        'protection': {'active_stop': active_stop},
        'new_evidence': list(evidence),
        'allowed_actions': templates,
    }


def _templates(remaining=100.0, active_stop=90.0):
    return build_position_action_templates(
        trade={'trade_id': 't1', 'code': 'US.AAPL', 'direction': 'long',
               'remaining_qty': remaining},
        active_stop=active_stop, expires_at='2999-01-01T00:00:00+00:00')


def _valid_raw(action='reduce', template_id='t1:reduce:50'):
    return {
        'status': 'complete', 'thesis_state': 'WEAKENING', 'action': action,
        'action_template_id': template_id, 'confidence': 'medium',
        'reason_codes': ['THESIS_WEAKENED'],
        'facts': [{'text': '财报低于预期', 'claim_type': 'fact', 'evidence_ids': ['e1']}],
        'inferences': [{'text': '基本面转弱，建议减仓', 'claim_type': 'inference',
                        'evidence_ids': ['e1']}],
        'counterevidence': [{'text': '估值已回落', 'claim_type': 'counterevidence',
                             'evidence_ids': ['e2']}],
        'missing_information': [],
        'thesis_delta': {'added_evidence_ids': ['e1'], 'removed_evidence_ids': [],
                         'summary': '基本面转弱'},
        'next_review_trigger_ids': [],
    }


class ThesisStateMachineTests(unittest.TestCase):
    def test_closed_is_final(self):
        self.assertEqual(transition_thesis('CLOSED', 'CONFIRMED', True)['state'], 'CLOSED')

    def test_terminal_not_recoverable(self):
        self.assertEqual(transition_thesis('INVALIDATED', 'CONFIRMED', True)['state'], 'INVALIDATED')
        self.assertEqual(transition_thesis('REALIZED', 'WEAKENING', True)['state'], 'REALIZED')

    def test_unknown_keeps_and_flags(self):
        r = transition_thesis('CONFIRMED', 'UNKNOWN', True)
        self.assertEqual(r['state'], 'CONFIRMED')
        self.assertTrue(r['review_required'])

    def test_no_new_evidence_no_change(self):
        self.assertEqual(transition_thesis('CONFIRMED', 'WEAKENING', False)['state'], 'CONFIRMED')

    def test_weakening_to_confirmed_needs_counterevidence(self):
        self.assertEqual(
            transition_thesis('WEAKENING', 'CONFIRMED', True, counterevidence_added=False)['state'],
            'WEAKENING')
        self.assertEqual(
            transition_thesis('WEAKENING', 'CONFIRMED', True, counterevidence_added=True)['state'],
            'CONFIRMED')

    def test_legacy_mapping(self):
        self.assertEqual(legacy_thesis_state('established'), 'CONFIRMED')
        self.assertEqual(legacy_thesis_state('weakened'), 'WEAKENING')
        self.assertEqual(legacy_thesis_state('invalidated'), 'INVALIDATED')
        self.assertEqual(legacy_thesis_state('closed'), 'CLOSED')
        self.assertEqual(legacy_thesis_state(None), 'UNKNOWN')


class PositionV2ValidationTests(unittest.TestCase):
    def test_valid_reduce(self):
        packet = _packet(_templates(),
                         evidence=[_ev('e1', '财报低于预期'),
                                   _ev('e2', '估值已回落', cluster='c2')])
        errs = validate_position_v2(_valid_raw(), packet)
        self.assertEqual(errs, [])

    def test_tighten_below_stop_rejected(self):
        # 收紧保护线低于当前保护线 → 报错
        packet = _packet(_templates(),
                         evidence=[_ev('e1', '财报低于预期'),
                                   _ev('e2', '估值已回落', cluster='c2')])
        raw = _valid_raw(action='tighten_protection', template_id='t1:tighten_protection')
        # 模板 new_protection_price == active_stop(90)，此处构造一个更低的新模板覆盖
        packet['allowed_actions'] = [{
            'template_id': 't1:tighten_protection', 'action': 'tighten_protection',
            'quantity': 0.0, 'new_protection_price': 85.0,
            'expires_at': '2999-01-01T00:00:00+00:00', 'constraints': {}}]
        errs = validate_position_v2(raw, packet)
        self.assertTrue(any('不得低于当前保护线' in e for e in errs))

    def test_exit_quantity_must_match_remaining(self):
        packet = _packet(_templates(),
                         evidence=[_ev('e1', '财报低于预期'),
                                   _ev('e2', '估值已回落', cluster='c2')])
        raw = _valid_raw(action='exit', template_id='t1:exit')
        # 剩余 100，exit 模板 qty=100 → 通过
        errs = validate_position_v2(raw, packet)
        self.assertEqual(errs, [])
        # 剩余 50，exit 模板 qty=100 → 报错
        packet2 = _packet(_templates(remaining=50.0),
                          evidence=[_ev('e1', '财报低于预期'),
                                    _ev('e2', '估值已回落', cluster='c2')])
        errs = validate_position_v2(raw, packet2)
        self.assertTrue(any('剩余数量' in e for e in errs))

    def test_reason_code_role_scope(self):
        packet = _packet(_templates(),
                         evidence=[_ev('e1', '财报低于预期'),
                                   _ev('e2', '估值已回落', cluster='c2')])
        raw = _valid_raw()
        raw['reason_codes'] = ['TECHNICAL_NOT_CONFIRMED']  # 仅 selection/entry，不适用 position
        errs = validate_position_v2(raw, packet)
        self.assertTrue(any('原因码' in e for e in errs))

    def test_market_and_declared_sector_evidence_allowed(self):
        for subject in ('MARKET', 'US.SOXX', 'semis'):
            packet = _packet(_templates(), evidence=[
                _ev('e1', '财报低于预期', subject=subject),
                _ev('e2', '估值已回落', subject=subject, cluster='c2')])
            packet['identity'] = {'sector': 'US.SOXX', 'risk_group': 'semis'}
            self.assertEqual(validate_position_v2(_valid_raw(), packet), [])

    def test_reduce_requires_cited_evidence_even_when_incomplete(self):
        packet = _packet(_templates(), evidence=[])
        raw = _valid_raw(action='reduce', template_id='t1:reduce:25')
        raw.update(status='insufficient_information', facts=[], inferences=[],
                   counterevidence=[], missing_information=['只有市场背景'])
        errs = validate_position_v2(raw, packet)
        self.assertTrue(any('必须引用至少一条证据' in e for e in errs))


if __name__ == '__main__':
    unittest.main()
