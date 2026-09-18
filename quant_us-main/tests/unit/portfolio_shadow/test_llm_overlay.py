"""LLM 入场否决 overlay：验证器 + resolve + FakeModel（PR5）。"""
import unittest

from scripts.portfolio_shadow.evidence import build_entry_packet
from scripts.portfolio_shadow.llm_overlay import (ENTRY_VETO_SYSTEM, FakeModel,
                                                  OverlayDecision, is_program_abstain,
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
# security_id 必须与 opp() 一致：VETO 只能靠**本证券**的证据支撑（市场级背景不算）
EVENT = {'summary': '公司下调指引', 'source': 'filing', 'kind': 'filing',
         'security_id': 'SEC-A',
         'published_at': '2026-01-04T12:00:00+00:00',
         'observed_at': '2026-01-04T13:00:00+00:00', 'content_hash': 'abc'}


def packet():
    return build_entry_packet(opp(), QUOTE, [EVENT], {}, AS_OF)


def valid_output(p, action='VETO'):
    return {'schema_version': 'entry-veto-v1', 'opportunity_id': p['opportunity_id'],
            'packet_id': p['packet_id'], 'action': action,
            'reason_code': 'MATERIAL_COMPANY_EVENT_RISK',
            'thesis_contrast': '规则计划未覆盖的测试事实',
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


MARKET_EVENT = {'summary': '盘前投研：科技板块整体走弱', 'source': 'daily_market',
                'kind': 'market_digest', 'security_id': 'MARKET',
                'published_at': '2026-01-04T12:00:00+00:00',
                'observed_at': '2026-01-04T13:00:00+00:00', 'content_hash': 'mkt'}


def market_only_packet():
    """真实形态：包里只有市场级日报（生产包正是「市场事件在前、证券事件在后」）。"""
    return build_entry_packet(opp(), QUOTE, [MARKET_EVENT], {}, AS_OF)


class CompanySpecificVetoTests(unittest.TestCase):
    """VETO 只能靠**本证券自己**的证据（审计 §4.2）。

    市场级日报对每个候选都可见、且排在证券事件之前 —— 那是背景。市场事实规则计划已经用
    市场门（MARKET_GATE_OPEN）算过了，所以拿它取消一笔交易等于让模型复述规则已经编码的东西。
    用户选定的口径是**严格**：两个 VETO 原因都要求本证券证据，不按原因区分。
    """

    def output(self, p, *, reason='MATERIAL_COMPANY_EVENT_RISK', contrast='规则计划漏看了 X',
               evidence_ids=None):
        return {'schema_version': 'entry-veto-v1', 'opportunity_id': p['opportunity_id'],
                'packet_id': p['packet_id'], 'action': 'VETO', 'reason_code': reason,
                'thesis_contrast': contrast,
                'evidence_ids': list(evidence_ids if evidence_ids is not None
                                     else [e['evidence_id'] for e in p['events']])}

    def test_只有市场级证据不能否决(self):
        p = market_only_packet()
        ok, errors = validate_model_output(self.output(p), p)
        self.assertFalse(ok)
        self.assertIn('VETO_NO_COMPANY_EVIDENCE', errors)

    def test_两个VETO原因都要求本证券证据(self):
        p = market_only_packet()
        for reason in ('MATERIAL_COMPANY_EVENT_RISK', 'MATERIAL_THESIS_CONTRADICTION'):
            with self.subTest(reason=reason):
                ok, errors = validate_model_output(self.output(p, reason=reason), p)
                self.assertIn('VETO_NO_COMPANY_EVIDENCE', errors)

    def test_市场证据可以一起引用只是不能单独(self):
        p = build_entry_packet(opp(), QUOTE, [MARKET_EVENT, EVENT], {}, AS_OF)
        market_id, company_id = (p['events'][0]['evidence_id'], p['events'][1]['evidence_id'])
        self.assertEqual(p['events'][0]['security_id'], 'MARKET')     # 生产顺序：市场在前
        ok, errors = validate_model_output(
            self.output(p, evidence_ids=[market_id, company_id]), p)
        self.assertTrue(ok, errors)
        _, errors = validate_model_output(self.output(p, evidence_ids=[market_id]), p)
        self.assertIn('VETO_NO_COMPANY_EVIDENCE', errors)

    def test_缺thesis_contrast被拒(self):
        p = build_entry_packet(opp(), QUOTE, [MARKET_EVENT, EVENT], {}, AS_OF)
        for value in (None, '', '   '):
            with self.subTest(value=repr(value)):
                _, errors = validate_model_output(self.output(p, contrast=value), p)
                self.assertIn('VETO_NO_THESIS_CONTRAST', errors)

    def test_PASS与ABSTAIN不要求thesis_contrast(self):
        """收紧只针对 VETO：PASS/ABSTAIN 采用父策略，不需要说明冲突。"""
        p = market_only_packet()
        for action in ('PASS', 'ABSTAIN'):
            with self.subTest(action=action):
                out = {'schema_version': 'entry-veto-v1',
                       'opportunity_id': p['opportunity_id'], 'packet_id': p['packet_id'],
                       'action': action}
                ok, errors = validate_model_output(out, p)
                self.assertTrue(ok, errors)

    def test_包缺证券身份时否决不成立(self):
        """身份核不了就不放行（fail closed），而不是默认通过。"""
        p = {**market_only_packet(), 'security_id': ''}
        _, errors = validate_model_output(self.output(p), p)
        self.assertIn('VETO_PACKET_HAS_NO_SECURITY', errors)

    def test_市场级否决被明确降级而不是静默通过(self):
        """端到端：单靠市场背景的否决 → ABSTAIN/INVALID_OUTPUT，并计入**故障降级**
        （§4.3 的口径）—— 而不是被当成模型的一次表态。"""
        p = market_only_packet()
        mr = {'status': 'OK', 'completed_at': '2026-01-04T13:10:00+00:00', 'cost_micro': 5,
              'output': self.output(p)}
        d = resolve_overlay(p, mr, DEADLINE)
        self.assertEqual(d.action, 'ABSTAIN')
        self.assertEqual(d.reason_code, 'INVALID_OUTPUT')
        self.assertTrue(is_program_abstain(d.reason_code))

    def test_system_prompt说明了这两条规则(self):
        """模型必须被告知它将被据以评判的规则 —— 否则是在罚它没读心术。"""
        self.assertIn('thesis_contrast', ENTRY_VETO_SYSTEM)
        self.assertIn('至少有一条属于本证券自己', ENTRY_VETO_SYSTEM)

    def test_夹具模型优先引用本证券证据(self):
        """生产包市场事件在前，直接取第一条会拿到 MARKET —— 夹具必须优先取本证券的。"""
        p = build_entry_packet(opp(), QUOTE, [MARKET_EVENT, EVENT], {}, AS_OF)
        got = FakeModel(action='VETO', reason_code='MATERIAL_COMPANY_EVENT_RISK',
                        evidence_from_packet=True).call(p, DEADLINE)['output']['evidence_ids']
        self.assertEqual(got, [p['events'][1]['evidence_id']])
