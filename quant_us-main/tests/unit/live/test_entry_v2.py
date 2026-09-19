"""Entry Decision v2 契约测试（§8，技术设计 §20.1/§20.4）。"""
import copy
import json
import time
import unittest

from mutifactor.llm.contracts.common import build_evidence_item
from mutifactor.llm.contracts.entry_v2 import (
    ENTRY_DECISION_SCHEMA, build_entry_prompt, build_entry_templates,
    build_review_trigger, forced_entry_action, normalize_entry_output, validate_entry_v2,
)
from scripts.live_trading.decision_ledger.reason_codes import (reasons_for_role,
                                                               valid_for_role)

PLAN = {'plan_id': 'plan1', 'stock_code': 'US.AAPL',
        'risk': {'initial_stop': 90.0}, 'entry_constraints': {'price': 100.0}}


def _ev(eid, summary, subject='US.AAPL', kind='fundamental', cluster='c1'):
    return build_evidence_item(
        evidence_id=eid, subject_code=subject, kind=kind, source='internal:test',
        source_grade=2, summary=summary, observed_at=time.time(), cluster_id=cluster)


def _packet(templates, evidence=(), quality_uses=('rank', 'entry')):
    return {
        'plan': PLAN,
        'templates': templates,
        'evidence': list(evidence),
        'quality_gate': {'status': 'pass', 'allowed_uses': list(quality_uses)},
        'signal': {},
    }


def _templates():
    return build_entry_templates(
        plan=PLAN, standard_quantity=100, entry_price=100.0, initial_stop=90.0,
        review_triggers=[build_review_trigger('t1', 'price_above', {'price': 101.0})],
        expires_at='2999-01-01T00:00:00+00:00')


def _valid_raw(action='execute_now', template_id='plan1:standard'):
    e1 = _ev('e1', '财报超预期，营收同比增长 20%')
    e2 = _ev('e2', '估值处于历史高位', kind='fundamental', cluster='c2')
    return {
        'status': 'complete', 'action': action, 'template_id': template_id,
        'confidence': 'high', 'reason_codes': ['OPTIONS_CONFIRM'],
        'facts': [{'text': '财报超预期，营收同比增长 20%', 'claim_type': 'fact',
                   'evidence_ids': ['e1']}],
        'inferences': [{'text': '基本面转强，可入场', 'claim_type': 'inference',
                        'evidence_ids': ['e1']}],
        'counterevidence': [{'text': '估值处于历史高位', 'claim_type': 'counterevidence',
                             'evidence_ids': ['e2']}],
        'missing_information': [],
        'selected_review_trigger_ids': [],
        'thesis_seed': {'summary': '营收超预期', 'evidence_ids': ['e1'],
                        'invalidation_condition_ids': []},
    }


class EntryV2TemplateTests(unittest.TestCase):
    def test_build_templates_quantities(self):
        ts = _templates()
        by_kind = {t['kind']: t for t in ts}
        self.assertEqual(by_kind['standard']['quantity'], 100)
        self.assertEqual(by_kind['half_size']['quantity'], 50)
        self.assertEqual(by_kind['wait_for_confirmation']['quantity'], 0)
        self.assertEqual(by_kind['reject']['quantity'], 0)
        self.assertEqual(len(by_kind['wait_for_confirmation']['review_triggers']), 1)

    def test_schema_version_fields(self):
        self.assertEqual(ENTRY_DECISION_SCHEMA['type'], 'object')


class EntryV2ValidationTests(unittest.TestCase):
    def test_valid_execute_now(self):
        packet = _packet(_templates(),
                         evidence=[_ev('e1', '财报超预期，营收同比增长 20%'),
                                   _ev('e2', '估值处于历史高位', cluster='c2')])
        errs = validate_entry_v2(_valid_raw(), packet)
        self.assertEqual(errs, [])

    def test_action_template_mismatch(self):
        packet = _packet(_templates(),
                         evidence=[_ev('e1', '财报超预期，营收同比增长 20%'),
                                   _ev('e2', '估值处于历史高位', cluster='c2')])
        raw = _valid_raw(action='execute_now', template_id='plan1:reject')
        errs = validate_entry_v2(raw, packet)
        self.assertTrue(any('不允许模板' in e for e in errs))

    def test_defer_requires_trigger_selection(self):
        packet = _packet(_templates(),
                         evidence=[_ev('e1', '财报超预期，营收同比增长 20%'),
                                   _ev('e2', '估值处于历史高位', cluster='c2')])
        raw = _valid_raw(action='defer', template_id='plan1:wait_for_confirmation')
        # 未选择触发器 → 报错
        errs = validate_entry_v2(raw, packet)
        self.assertTrue(any('defer 模板' in e for e in errs))
        # 选择存在的触发器 → 通过
        raw['selected_review_trigger_ids'] = ['t1']
        errs = validate_entry_v2(raw, packet)
        self.assertEqual(errs, [])

    def test_cross_stock_evidence_rejected(self):
        packet = _packet(_templates(),
                         evidence=[_ev('e1', '财报超预期，营收同比增长 20%', subject='US.MSFT'),
                                   _ev('e2', '估值处于历史高位', cluster='c2')])
        errs = validate_entry_v2(_valid_raw(), packet)
        self.assertTrue(any('跨股票引用' in e for e in errs))

    def test_market_and_declared_sector_evidence_allowed(self):
        for subject in ('MARKET', 'US.SOXX', 'semis'):
            packet = _packet(_templates(), evidence=[
                _ev('e1', '财报超预期，营收同比增长 20%', subject=subject),
                _ev('e2', '估值处于历史高位', subject=subject, cluster='c2')])
            packet['identity'] = {'sector': 'US.SOXX', 'risk_group': 'semis'}
            self.assertEqual(validate_entry_v2(_valid_raw(), packet), [])

    def test_undeclared_sector_and_missing_subject_rejected(self):
        for subject in ('US.XLE', None):
            packet = _packet(_templates(), evidence=[
                _ev('e1', '财报超预期，营收同比增长 20%', subject=subject),
                _ev('e2', '估值处于历史高位', cluster='c2')])
            packet['identity'] = {'sector': 'US.SOXX', 'risk_group': 'semis'}
            errs = validate_entry_v2(_valid_raw(), packet)
            self.assertTrue(any(('跨股票引用' in e or '缺少归属' in e) for e in errs))


class EntryV2ForcedActionTests(unittest.TestCase):
    def test_quality_gate_no_entry_forces_defer(self):
        packet = _packet(_templates(), quality_uses=['rank'])
        self.assertEqual(forced_entry_action(packet, []), 'defer')

    def test_future_evidence_forces_reject(self):
        from mutifactor.llm.contracts.common import utc as _utc
        future_ts = time.time() + 100000
        future = build_evidence_item(
            evidence_id='e1', subject_code='US.AAPL', kind='news', source='internal:test',
            source_grade=2, summary='s', observed_at=future_ts, effective_at=future_ts)
        packet = _packet(_templates(), evidence=[future])
        self.assertEqual(forced_entry_action(packet, [], as_of=_utc()), 'reject')

    def test_cross_stock_error_forces_reject(self):
        packet = _packet(_templates())
        self.assertEqual(forced_entry_action(packet, ['跨股票引用: e1 -> US.MSFT']), 'reject')

    def test_clean_packet_no_force(self):
        packet = _packet(_templates())
        self.assertIsNone(forced_entry_action(packet, []))


if __name__ == '__main__':
    unittest.main()


class ReasonCodeEnumTests(unittest.TestCase):
    """`reason_codes` 的合法闭集必须出现在提示词里（与 position 同一条不变量）。

    校验器 `valid_for_role(rc, 'entry')` fail-closed，而提示词原先只发 packet + schema，
    schema 里 `reason_codes` 是任意字符串 ⇒ 模型只能猜，猜错整批决策作废。
    证据 ID 的同类问题在 selection 上修过一次（提示词闭集）；entry/position 漏了这一课。
    """

    def test_prompt_carries_exactly_the_accepted_codes(self):
        enum = set(json.loads(build_entry_prompt(_packet([])))['output_schema']
                   ['properties']['reason_codes']['items']['enum'])
        self.assertEqual(enum, set(reasons_for_role('entry')))
        for code in sorted(enum):
            self.assertTrue(valid_for_role(code, 'entry'), code)

    def test_building_a_prompt_does_not_mutate_the_shared_schema(self):
        before = copy.deepcopy(ENTRY_DECISION_SCHEMA['properties']['reason_codes'])
        build_entry_prompt(_packet([]))
        self.assertEqual(ENTRY_DECISION_SCHEMA['properties']['reason_codes'], before)


class NonVerbatimFactDowngradeTests(unittest.TestCase):
    """非逐字 `fact` 必须**降级**而不是让整条 entry 决策作废（与 position 同一规则）。

    真实模型引的是长日报的**片段**，与整段摘要全等不可能；夹具用一句话摘要，看不见。
    """

    def test_nonverbatim_fact_is_downgraded_not_rejected(self):
        packet = _packet(_templates(), evidence=[_ev('e1', '很长的一段市场日报……' * 200)])
        raw = {'status': 'complete', 'action': 'reject', 'template_id': 'plan1:reject',
               'confidence': 'medium', 'reason_codes': ['INSUFFICIENT_EVIDENCE'],
               'facts': [{'text': '📉 逆势：LITE −2.81%', 'claim_type': 'fact',
                          'evidence_ids': ['e1']}],
               'inferences': [], 'counterevidence': [], 'missing_information': ['缺口']}
        self.assertTrue(validate_entry_v2(raw, packet))
        normalized = normalize_entry_output(raw, packet)
        self.assertEqual(normalized['facts'][0]['claim_type'], 'inference')
        self.assertEqual(validate_entry_v2(normalized, packet), [])

    def test_verbatim_fact_is_kept(self):
        summary = 'US.AAPL 收盘 100.0'
        packet = _packet(_templates(), evidence=[_ev('e1', summary)])
        raw = {'status': 'complete', 'action': 'reject', 'template_id': 'plan1:reject',
               'confidence': 'medium', 'reason_codes': ['INSUFFICIENT_EVIDENCE'],
               'facts': [{'text': summary, 'claim_type': 'fact', 'evidence_ids': ['e1']}],
               'inferences': [], 'counterevidence': [], 'missing_information': ['缺口']}
        self.assertEqual(normalize_entry_output(raw, packet)['facts'][0]['claim_type'],
                         'fact')


class ContractNormalizeHookTests(unittest.TestCase):
    """每个角色契约都必须声明 `normalize`。

    原缺：`entry`/`position` 的契约**没有** `normalize`（selection/portfolio/review 都有）。
    后果不是"少做一次清洗"，而是 `fact 未逐字匹配证据摘要` 直接让**整条决策作废** ——
    真实模型因此永远拿不到有效动作，L 路恒等于 R。这条钉死钩子存在：新增角色若漏掉，
    会静默回到老毛病（而且夹具路径照样绿）。
    """

    def test_every_role_contract_declares_normalize(self):
        from scripts.live_trading.decision_engine import _CONTRACTS
        missing = sorted(role for role, factory in _CONTRACTS.items()
                         if not factory().get('normalize'))
        self.assertEqual(missing, [], f'这些角色契约没有 normalize 钩子: {missing}')
