"""真实 LLM 模型 RealModel 测试（mock advisor，无真实 API 调用）。"""
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from scripts.portfolio_shadow.evidence import build_entry_packet
from scripts.portfolio_shadow.llm_overlay import RealModel, decide_overlay, resolve_overlay
from scripts.portfolio_shadow.schema import Opportunity, to_micro


def opp():
    return Opportunity(experiment_id='exp1', security_id='SEC-A', source_candidate_id='c1',
                       parent_version='1', signal_session='2026-01-04',
                       observed_at='2026-01-04T21:00:00+00:00',
                       planned_execution_session='2026-01-05', rank=1, entry_rule='b3',
                       stop_reference={'atr14_micro': to_micro(2.0)}, exit_policy_id='H60',
                       input_hash='h', terminal='READY')


QUOTE = {'price': to_micro(100.0), 'observed_at': '2026-01-04T21:00:00+00:00'}
# security_id 必须与 opp() 一致：VETO 只能靠**本证券**的证据支撑（市场级背景不算）
EVENT = {'summary': '公司下调指引', 'source': 'filing', 'kind': 'filing',
         'security_id': 'SEC-A',
         'published_at': '2026-01-04T12:00:00+00:00',
         'observed_at': '2026-01-04T13:00:00+00:00', 'content_hash': 'abc'}
DEADLINE = '2026-01-05T13:20:00+00:00'  # 决策截止（次日开盘前）
NOW = datetime(2026, 1, 5, 13, 0, tzinfo=timezone.utc)  # 截止前 20 分钟：够新，且不算晚到


def advisor_with(output, metadata):
    advisor = Mock()
    advisor.chat.return_value = output
    advisor.last_metadata = metadata
    return advisor


class RealModelTests(unittest.TestCase):
    def _packet(self):
        return build_entry_packet(opp(), QUOTE, [EVENT], {}, DEADLINE)

    def test_call_maps_ok_and_cost(self):
        p = self._packet()
        advisor = advisor_with(
            {'schema_version': 'entry-veto-v1', 'opportunity_id': p['opportunity_id'],
             'packet_id': p['packet_id'], 'action': 'VETO',
             'reason_code': 'MATERIAL_COMPANY_EVENT_RISK',
             'thesis_contrast': '规则计划未覆盖的测试事实',
             'evidence_ids': [p['events'][0]['evidence_id']], 'explanation': ''},
            {'cost_usd': 0.00123})
        mr = RealModel(advisor, now=lambda: NOW).call(p, DEADLINE)
        self.assertEqual(mr['status'], 'OK')
        self.assertEqual(mr['output']['action'], 'VETO')
        self.assertEqual(mr['cost_micro'], 1230)
        self.assertFalse(mr['cost_uncertain'])
        # prompt 含 entry_veto + opportunity_id
        prompt = advisor.chat.call_args[0][0]
        self.assertIn('entry_veto', prompt)
        self.assertIn(p['opportunity_id'], prompt)

    def test_chat_none_maps_to_failed(self):
        p = self._packet()
        advisor = advisor_with(None, {'cost_usd': 0.0})
        mr = RealModel(advisor, now=lambda: NOW).call(p, DEADLINE)
        self.assertEqual(mr['status'], 'FAILED')
        self.assertIsNone(mr['output'])
        self.assertEqual(mr['cost_micro'], 0)
        # resolve_overlay 把 FAILED 降级 ABSTAIN
        d = resolve_overlay(p, mr, DEADLINE)
        self.assertEqual(d.action, 'ABSTAIN')
        self.assertEqual(d.reason_code, 'FAILED')

    def test_historical_as_of_never_calls_the_model(self):
        p = self._packet()
        advisor = advisor_with({'action': 'VETO'}, {'cost_usd': 0.01})
        # now 远晚于 deadline（历史回放）：不得发起实时调用
        mr = RealModel(advisor, now=lambda: NOW).call(p, '2026-01-01T13:20:00+00:00')
        self.assertEqual(mr['status'], 'HISTORICAL_AS_OF')
        self.assertEqual(mr['cost_micro'], 0)
        advisor.chat.assert_not_called()
        d = resolve_overlay(p, mr, DEADLINE)
        self.assertEqual(d.action, 'ABSTAIN')
        self.assertEqual(d.reason_code, 'HISTORICAL_AS_OF')
        self.assertEqual(d.model_cost, 0)

    def test_unknown_cost_is_not_recorded_as_free(self):
        p = self._packet()
        # advisor 在 cost_uncertain 时故意返回 cost_usd=None（见 mutifactor/llm/advisor.py）
        advisor = advisor_with(None, {'cost_usd': None, 'cost_uncertain': True})
        mr = RealModel(advisor, now=lambda: NOW).call(p, DEADLINE)
        self.assertIsNone(mr['cost_micro'])
        self.assertTrue(mr['cost_uncertain'])
        d = resolve_overlay(p, mr, DEADLINE)
        # 金额 0 但标记待补记 —— 不是「免费调用」
        self.assertEqual(d.model_cost, 0)
        self.assertTrue(d.cost_uncertain)
        self.assertEqual(d.reason_code, 'FAILED')

    def test_decide_overlay_binds_attempt_id(self):
        p = self._packet()
        advisor = advisor_with(
            {'schema_version': 'entry-veto-v1', 'opportunity_id': p['opportunity_id'],
             'packet_id': p['packet_id'], 'action': 'PASS', 'reason_code': '',
             'evidence_ids': [], 'explanation': ''}, {'cost_usd': 0.002})
        d = decide_overlay(p, RealModel(advisor, now=lambda: NOW), DEADLINE,
                           attempt_id='llm_attempt_abc')
        self.assertEqual(d.action, 'PASS')
        self.assertEqual(d.attempt_id, 'llm_attempt_abc')
        self.assertEqual(d.model_cost, 2000)
        self.assertFalse(d.cost_uncertain)


class QualityGateTests(unittest.TestCase):
    """数据质量门：BLOCK / LLM_INSUFFICIENT 直接 ABSTAIN，不调模型、不产生费用。"""

    def test_no_evidence_abstains_without_calling_model(self):
        p = build_entry_packet(opp(), QUOTE, [], {}, DEADLINE)
        self.assertEqual(p['data_quality']['level'], 'LLM_INSUFFICIENT')
        model = Mock()
        d = decide_overlay(p, model, DEADLINE, attempt_id='a1')
        self.assertEqual(d.action, 'ABSTAIN')
        self.assertEqual(d.reason_code, 'INSUFFICIENT_EVIDENCE')
        self.assertEqual(d.model_cost, 0)
        self.assertFalse(d.cost_uncertain)
        model.call.assert_not_called()

    def test_blocked_quote_blocks_without_calling_model(self):
        p = build_entry_packet(opp(), {'price': None, 'observed_at': QUOTE['observed_at']},
                               [EVENT], {}, DEADLINE)
        self.assertEqual(p['data_quality']['level'], 'BLOCK')
        model = Mock()
        d = decide_overlay(p, model, DEADLINE, attempt_id='a1')
        # BLOCK 不是 ABSTAIN：ABSTAIN＝采用父策略（照常成交），BLOCK＝剔除
        self.assertEqual(d.action, 'BLOCK')
        self.assertEqual(d.reason_code, 'DATA_BLOCKED_QUOTE')
        self.assertEqual(d.model_cost, 0)
        model.call.assert_not_called()

    def test_veto_with_evidence_is_reachable(self):
        """真实路径的可达性：证据正文进包 → VETO 能引用 → 校验通过（回归 #3）。"""
        p = build_entry_packet(opp(), QUOTE, [EVENT], {}, DEADLINE)
        self.assertEqual(p['data_quality']['level'], 'OK')
        self.assertEqual(p['events'][0]['summary'], '公司下调指引')  # 正文未被丢弃
        advisor = advisor_with(
            {'schema_version': 'entry-veto-v1', 'opportunity_id': p['opportunity_id'],
             'packet_id': p['packet_id'], 'action': 'VETO',
             'reason_code': 'MATERIAL_THESIS_CONTRADICTION',
             'thesis_contrast': '规则计划未覆盖的测试事实',
             'evidence_ids': [p['events'][0]['evidence_id']], 'explanation': ''},
            {'cost_usd': 0.0})
        d = decide_overlay(p, RealModel(advisor, now=lambda: NOW), DEADLINE, attempt_id='a1')
        self.assertEqual(d.action, 'VETO')
        self.assertEqual(d.reason_code, 'MATERIAL_THESIS_CONTRADICTION')


if __name__ == '__main__':
    unittest.main()


class KnowledgeCutoffTests(unittest.TestCase):
    """模型训练数据截止披露：决策时点早于它时，as-of 证据过滤修不好泄漏，必须拒绝。"""

    def _packet(self, cutoff=None):
        return build_entry_packet(opp(), QUOTE, [EVENT], {}, DEADLINE,
                                  model_knowledge_cutoff=cutoff)

    def test_cutoff_is_frozen_into_the_packet(self):
        p = self._packet('2026-06-01T00:00:00+00:00')
        self.assertEqual(p['model_knowledge_cutoff'], '2026-06-01T00:00:00+00:00')
        # 换了声明值就是换了包，不能与旧包混为一谈
        self.assertNotEqual(p['packet_id'], self._packet()['packet_id'])

    def test_session_before_cutoff_is_refused_without_calling(self):
        p = self._packet('2026-06-01T00:00:00+00:00')  # 决策在 2026-01-05，早于截止
        advisor = advisor_with({'action': 'VETO'}, {'cost_usd': 0.01})
        mr = RealModel(advisor, now=lambda: NOW,
                       knowledge_cutoff='2026-06-01T00:00:00+00:00').call(p, DEADLINE)
        self.assertEqual(mr['status'], 'MODEL_KNOWLEDGE_CUTOFF')
        self.assertEqual(mr['cost_micro'], 0)
        advisor.chat.assert_not_called()
        d = resolve_overlay(p, mr, DEADLINE)
        self.assertEqual(d.action, 'ABSTAIN')
        self.assertEqual(d.reason_code, 'MODEL_KNOWLEDGE_CUTOFF')

    def test_session_after_cutoff_proceeds(self):
        p = self._packet('2025-01-01T00:00:00+00:00')
        advisor = advisor_with(
            {'schema_version': 'entry-veto-v1', 'opportunity_id': p['opportunity_id'],
             'packet_id': p['packet_id'], 'action': 'PASS', 'reason_code': '',
             'evidence_ids': [], 'explanation': ''}, {'cost_usd': 0.0})
        mr = RealModel(advisor, now=lambda: NOW,
                       knowledge_cutoff='2025-01-01T00:00:00+00:00').call(p, DEADLINE)
        self.assertEqual(mr['status'], 'OK')

    def test_unknown_cutoff_declaration_skips_the_check(self):
        # 'unknown' 无法解析 ⇒ 不做检查（但 manifest 的 freeze 门保证它被显式声明过）
        p = self._packet('unknown')
        advisor = advisor_with(
            {'schema_version': 'entry-veto-v1', 'opportunity_id': p['opportunity_id'],
             'packet_id': p['packet_id'], 'action': 'PASS', 'reason_code': '',
             'evidence_ids': [], 'explanation': ''}, {'cost_usd': 0.0})
        mr = RealModel(advisor, now=lambda: NOW, knowledge_cutoff='unknown').call(p, DEADLINE)
        self.assertEqual(mr['status'], 'OK')
