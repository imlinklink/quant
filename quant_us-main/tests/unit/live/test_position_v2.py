"""Position Decision v2 契约测试（§9，技术设计 §20.5）。"""
import copy
import json
import time
import unittest

from mutifactor.llm.contracts.common import build_evidence_item
from mutifactor.llm.contracts.position_v2 import (
    POSITION_DECISION_SCHEMA, build_position_action_templates, build_position_prompt,
    legacy_thesis_state, normalize_position_output, transition_thesis, validate_position_v2,
)
from scripts.live_trading.decision_ledger.reason_codes import (reasons_for_role,
                                                               valid_for_role)


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


class ReasonCodeEnumTests(unittest.TestCase):
    """`reason_codes` 的**合法闭集必须出现在提示词里**。

    这是一条回归。校验器对原因码 fail-closed（`valid_for_role(rc, 'position')`），而提示词
    原先只发 packet + schema —— 而 schema 里 `reason_codes` 是**任意字符串**。模型只能猜，
    实测真实模型自造了 5 个看似合理但不在注册表里的码
    （`SUBJECT_ONLY_MARKET_EVIDENCE` / `NO_SUBJECT_SPECIFIC_NEW_EVIDENCE` / …）⇒ **全条被拒**、
    降级成 ABSTAIN。**夹具路径永远看不见**：`FakePositionModel` 的注释写着"给一个自造的会被
    校验器拒"，于是它手工只发合法码 —— 阅读这段注释时看到的是"已处理"，实际是"绕开了"。

    不变量：**提示词告诉模型的集合 == 校验器接受的集合**，两个方向都要。
    """

    def _enum(self, prompt):
        schema = json.loads(prompt)['output_schema']
        return set(schema['properties']['reason_codes']['items']['enum'])

    def test_live_prompt_carries_exactly_the_accepted_codes(self):
        enum = self._enum(build_position_prompt(_packet([])))
        self.assertEqual(enum, set(reasons_for_role('position')))
        for code in sorted(enum):
            self.assertTrue(valid_for_role(code, 'position'), code)

    def test_shadow_action_prompt_carries_exactly_the_accepted_codes(self):
        """影子侧走的是另一条提示词（`build_position_action_prompt`），同样要带枚举。"""
        from scripts.portfolio_shadow.position_overlay import \
            build_position_action_prompt
        enum = self._enum(build_position_action_prompt(_packet([])))
        self.assertEqual(enum, set(reasons_for_role('position')))
        for code in sorted(enum):
            self.assertTrue(valid_for_role(code, 'position'), code)

    def test_building_a_prompt_does_not_mutate_the_shared_schema(self):
        """schema 对象被多方共享（还进快照、进哈希），就地改会串味。"""
        before = copy.deepcopy(POSITION_DECISION_SCHEMA['properties']['reason_codes'])
        build_position_prompt(_packet([]))
        self.assertEqual(POSITION_DECISION_SCHEMA['properties']['reason_codes'], before)


class NonVerbatimFactDowngradeTests(unittest.TestCase):
    """非逐字 `fact` 必须**降级**而不是让整条决策作废。

    校验器的判据（`fact 未逐字匹配证据摘要`）本身是对的 —— 不许把释义当事实。但它
    fail-closed，而真实模型引的是 6000 字市场日报里的**片段**，与整段全等**在长度上就不可能**
    ⇒ 每条 fact 都失败 ⇒ 整批降级 ABSTAIN，L 路恒等于 R。夹具里摘要是 `'测试用缺口声明'`
    这种一句话，所以测试全都看不见。

    实测（真实模型，2026-09-19）：修复前 `INVALID_OUTPUT` + 12 条错误；修复后
    `validation_errors=None`、`reason_code=THESIS_WEAKENED`。
    """

    def _long_evidence(self):
        return [_ev('e1', '很长的一段市场日报……' * 200)]

    def test_nonverbatim_fact_is_downgraded_not_rejected(self):
        packet = _packet([], evidence=self._long_evidence())
        raw = {'status': 'complete', 'thesis_state': 'CONFIRMED', 'action': 'hold',
               'action_template_id': None, 'confidence': 'medium',
               'reason_codes': ['THESIS_WEAKENED'],
               'facts': [{'text': '📉 逆势：LITE −2.81%', 'claim_type': 'fact',
                          'evidence_ids': ['e1']}],
               'inferences': [], 'counterevidence': [], 'missing_information': ['缺口']}
        # 不降级时：校验必然失败（夹具就是这么绕过这个坑的）
        self.assertTrue(validate_position_v2(raw, packet))
        normalized = normalize_position_output(raw, packet)
        self.assertEqual(normalized['facts'][0]['claim_type'], 'inference')
        self.assertEqual(validate_position_v2(normalized, packet), [])

    def test_verbatim_fact_is_kept(self):
        """逐字匹配的 fact 不该被误降级 —— 否则这条修复就把真事实也削弱了。"""
        summary = 'US.AAPL 收盘 100.0'
        packet = _packet([], evidence=[_ev('e1', summary)])
        raw = {'status': 'complete', 'thesis_state': 'CONFIRMED', 'action': 'hold',
               'action_template_id': None, 'confidence': 'medium',
               'reason_codes': ['THESIS_WEAKENED'],
               'facts': [{'text': summary, 'claim_type': 'fact', 'evidence_ids': ['e1']}],
               'inferences': [], 'counterevidence': [], 'missing_information': ['缺口']}
        self.assertEqual(normalize_position_output(raw, packet)['facts'][0]['claim_type'],
                         'fact')

    def test_normalize_does_not_mutate_the_input(self):
        """归一化返回副本：raw response 要原样留痕，不能被就地改写。"""
        packet = _packet([], evidence=self._long_evidence())
        raw = {'facts': [{'text': 'x', 'claim_type': 'fact', 'evidence_ids': ['e1']}],
               'inferences': [], 'counterevidence': []}
        before = copy.deepcopy(raw)
        normalize_position_output(raw, packet)
        self.assertEqual(raw, before)
