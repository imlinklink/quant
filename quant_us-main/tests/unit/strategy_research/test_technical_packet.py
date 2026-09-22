"""技术证据包（规划 §6.2）与限定动作集（§6.1）。

三件事必须被钉死：
1. 每一项都带单位/算法版本/as_of/available_at/来源/**缺失原因**，缺数据时 value 为 None
   而**不是 0**（0 会被读成「没有风险」）；
2. 充分性标准是**它自己的**，与新闻无关 —— 这是本项目「不接公司事件源」之后，
   持仓角色还能被真实模型评审的唯一通路；
3. 不给技术包时，包与引入前**逐字段相同**（新增键会改变 packet_id）。
"""
import unittest
from pathlib import Path

import pandas as pd

from scripts.portfolio_shadow.position_overlay import (POSITION_ACTION_SCHEMA, POSITION_EXIT,
                                                       POSITION_HOLD, validate_position_output)
from scripts.portfolio_shadow.position_packet import build_position_packet
from scripts.portfolio_shadow.schema import Position, to_micro
from scripts.strategy_research.technical_packet import (ALGORITHM_VERSIONS, OPTIONAL, REQUIRED,
                                                        TECHNICAL_SCHEMA_VERSION, build_facts,
                                                        sufficiency, to_evidence_items)

SID = 'SEC-US-AAPL'
SESSION = '2026-01-05'
AS_OF = '2026-01-05T21:30:00+00:00'


def bars(n=300, *, close=200.0, atr=4.0, split_at=None, volume=1_000_000):
    idx = pd.date_range('2024-11-01', periods=n, freq='B').normalize()
    closes = [close] * n
    if split_at is not None:
        closes = [close * 2 if i < split_at else close for i in range(n)]
    return pd.DataFrame({'security_id': SID, 'session': idx, 'raw_open': closes,
                         'raw_high': [c * 1.01 for c in closes],
                         'raw_low': [c * 0.99 for c in closes], 'raw_close': closes,
                         'volume': volume, 'asof_atr': atr, 'scale_to_next': 1.0})


def position(**over):
    base = dict(security_id=SID, shares=100, entry_price_micro=to_micro(180.0),
                entry_session='2025-12-01', initial_stop_micro=to_micro(160.0),
                stop_micro=to_micro(170.0), exit_policy_id='H60', opportunity_id='opp-1',
                holding_sessions=25, initial_risk_micro=to_micro(2000.0))
    base.update(over)
    return Position(**base)


def account_facts(**over):
    base = {'risk_state': 'NORMAL', 'account_drawdown': 0.02, 'risk_budget_bp': 100,
            'cash_share': 0.4, 'gross_exposure': 0.6}
    base.update(over)
    return base


def facts(**over):
    return build_facts(security_id=SID, history=bars(), atr14_micro=to_micro(4.0),
                       position=position(**{k: v for k, v in over.items()
                                            if k in ('shares', 'stop_micro', 'entry_price_micro',
                                                     'holding_sessions', 'initial_risk_micro')}),
                       account_facts=account_facts(), session=SESSION,
                       mark_price_micro=to_micro(200.0), actions=pd.DataFrame())


class FactContractTests(unittest.TestCase):
    def test_every_fact_carries_the_contract_fields(self):
        for name, fact in facts().items():
            item = fact.as_dict()
            self.assertEqual(set(item), {'name', 'value', 'unit', 'algorithm_version',
                                         'as_of', 'available_at', 'source',
                                         'missing_reason'}, name)
            self.assertTrue(item['unit'], name)
            self.assertTrue(item['algorithm_version'], name)
            self.assertEqual(item['as_of'], SESSION)
            self.assertEqual(item['available_at'], SESSION)
            self.assertIn(item['source'], ('program:technical_packet',))
            self.assertFalse(str(item['value']).startswith('nan'), name)

    def test_missing_data_sets_a_reason_and_never_fabricates_a_value(self):
        """历史不足 ⇒ 该项**缺失**（value=None + 原因），不是 0 —— 0 会被读成「没问题」。"""
        short = build_facts(security_id=SID, history=bars(n=30),
                            atr14_micro=to_micro(4.0), position=position(),
                            account_facts=account_facts(), session=SESSION,
                            mark_price_micro=to_micro(200.0), actions=pd.DataFrame())
        for name in ('ma200', 'high_252', 'drawdown_frac', 'ma200_slope_20'):
            self.assertIsNone(short[name].value, name)
            self.assertTrue(short[name].missing_reason, name)
        # ATR 只依赖当日值 ⇒ 短历史**不影响**必需项
        self.assertIsNotNone(short['atr14'].value)
        self.assertEqual(sufficiency(short)['level'], 'OK')
        # 而 ATR 真的缺时，必需项少一个 ⇒ 不充分（不调用模型）
        no_atr = build_facts(security_id=SID, history=bars(n=30), atr14_micro=None,
                             position=position(), account_facts=account_facts(),
                             session=SESSION, mark_price_micro=to_micro(200.0),
                             actions=pd.DataFrame())
        self.assertEqual(sufficiency(no_atr)['level'], 'INSUFFICIENT')
        self.assertIn('atr14', sufficiency(no_atr)['required_missing'])

    def test_sufficiency_flips_when_a_required_fact_is_missing(self):
        good = facts()
        self.assertEqual(sufficiency(good)['level'], 'OK')
        for name in REQUIRED:
            broken = dict(good)
            broken[name] = type(good[name])(**{**good[name].as_dict(),
                                               'value': None,
                                               'missing_reason': 'INJECTED'})
            result = sufficiency(broken)
            self.assertEqual(result['level'], 'INSUFFICIENT', name)
            self.assertIn(name, result['required_missing'])
        # 缺可选项不拦
        optional = OPTIONAL[0]
        broken = dict(good)
        broken[optional] = type(good[optional])(**{**good[optional].as_dict(), 'value': None,
                                                   'missing_reason': 'INJECTED'})
        self.assertEqual(sufficiency(broken)['level'], 'OK')

    def test_ma200_is_computed_on_the_adjusted_series(self):
        """2:1 拆股：用原始价会让 MA200 与现价差一倍 ⇒ 假的趋势破坏。"""
        n = 300
        split_at = n - 40
        history = bars(n=n, close=100.0, split_at=split_at)
        actions = pd.DataFrame([{'security_id': SID, 'action_type': 'split',
                                 'ex_date': history.session.iloc[split_at], 'ratio': 2,
                                 'cash_amount': 0.0}])
        f = build_facts(security_id=SID, history=history, atr14_micro=to_micro(4.0),
                        position=position(), account_facts=account_facts(),
                        session=history.session.iloc[-1], mark_price_micro=to_micro(200.0),
                        actions=actions)
        # 复权后全序列都是 200（拆股前 100 × 2），所以 MA200 == 现价
        self.assertAlmostEqual(f['ma200'].value / f['close'].value, 1.0, places=6)

    def test_algorithm_versions_are_declared_per_quantity(self):
        used = {f.algorithm_version for f in facts().values()}
        self.assertTrue(used <= set(ALGORITHM_VERSIONS.values()), used)
        self.assertEqual(TECHNICAL_SCHEMA_VERSION, 'technical-packet-v1')


class EvidenceTests(unittest.TestCase):
    def test_evidence_items_are_citable_and_skip_missing_facts(self):
        f = facts()
        f['ma200'] = type(f['ma200'])(**{**f['ma200'].as_dict(), 'value': None,
                                         'missing_reason': 'INJECTED'})
        items = to_evidence_items(f, security_id=SID, as_of=AS_OF)
        names = {i['title'].split(' ')[0] for i in items}
        self.assertNotIn('ma200', names, '缺失的项不该生成可引用条目')
        for item in items:
            self.assertEqual(item['subject_code'], SID)
            self.assertEqual(item['kind'], 'technical')
            self.assertEqual(item['quality'], 'ok')
            self.assertLessEqual(item['published_at'], AS_OF)
        ids = [i['evidence_id'] for i in items]
        self.assertEqual(len(ids), len(set(ids)), 'evidence_id 必须互不相同')

    def test_a_fact_claim_citing_a_technical_item_passes_validation(self):
        """模型必须能**逐字引用**技术事实 —— 否则判断只剩原因码可说，不可解释。"""
        packet = _packet()
        item = packet['new_evidence'][0]
        output = {'schema_version': 'v2', 'packet_id': packet['packet_id'],
                  'status': 'complete', 'thesis_state': 'WEAKENING', 'action': 'exit',
                  'action_template_id': None, 'confidence': 'medium',
                  'reason_codes': ['THESIS_WEAKENED'],
                  'facts': [{'text': item['summary'], 'claim_type': 'fact',
                             'evidence_ids': [item['evidence_id']]}],
                  'inferences': [], 'counterevidence': [],
                  'missing_information': ['无公司事件证据']}
        ok, errors = validate_position_output(output, packet)
        self.assertTrue(ok, errors)

    def test_a_paraphrased_fact_on_technical_evidence_is_downgraded_not_rejected(self):
        packet = _packet()
        item = packet['new_evidence'][0]
        output = {'schema_version': 'v2', 'packet_id': packet['packet_id'],
                  'status': 'complete', 'thesis_state': 'WEAKENING', 'action': 'exit',
                  'action_template_id': None, 'confidence': 'medium',
                  'reason_codes': ['THESIS_WEAKENED'],
                  'facts': [{'text': '趋势已经转弱', 'claim_type': 'fact',
                             'evidence_ids': [item['evidence_id']]}],
                  'inferences': [], 'counterevidence': [],
                  'missing_information': ['无公司事件证据']}
        ok, errors = validate_position_output(output, packet)
        self.assertTrue(ok, errors)      # 降级为 inference，而不是整条作废


def _packet(*, with_technical=True, open_actions=('hold', 'exit'), events=()):
    return build_position_packet(
        security_id=SID, trade={'entry_session': '2025-12-01'},
        protection={'active_stop': to_micro(170.0), 'initial_stop': to_micro(160.0),
                    'hard_exit_authoritative': True},
        events=list(events), as_of=AS_OF, execution_session='2026-01-06',
        account_scope='SHADOW:x:L', experiment_id='x', opportunity_id='opp-1',
        reviewed_session=SESSION, shares=100, entry_price_micro=to_micro(180.0),
        mark_price_micro=to_micro(200.0), evidence={'evidence_mode': 'strict'},
        technical=(facts() if with_technical else None), open_actions=open_actions)


class PacketGateTests(unittest.TestCase):
    def test_technical_packet_opens_the_gate_without_any_news(self):
        """**本批的关键行为改变**：没有新闻也能评审 —— 否则模型永不被调用、L 恒等于 R。"""
        packet = _packet()
        self.assertEqual(packet['data_quality']['level'], 'OK')
        self.assertEqual(packet['data_quality']['technical_sufficiency'], 'OK')
        self.assertEqual(packet['data_quality']['news_events'], 0)
        self.assertGreater(packet['data_quality']['technical_facts'], 0)
        self.assertIn('technical', packet)

    def test_without_the_technical_packet_the_old_gate_applies(self):
        packet = _packet(with_technical=False)
        self.assertEqual(packet['data_quality']['level'], 'LLM_INSUFFICIENT')

    def test_insufficient_technical_facts_gate_as_llm_insufficient(self):
        f = facts()
        f['atr14'] = type(f['atr14'])(**{**f['atr14'].as_dict(), 'value': None,
                                         'missing_reason': 'INJECTED'})
        packet = build_position_packet(
            security_id=SID, trade={'entry_session': '2025-12-01'},
            protection={'active_stop': to_micro(170.0)}, events=[], as_of=AS_OF,
            execution_session='2026-01-06', account_scope='SHADOW:x:L', experiment_id='x',
            opportunity_id='opp-1', reviewed_session=SESSION, shares=100,
            entry_price_micro=to_micro(180.0), mark_price_micro=to_micro(200.0),
            evidence={'evidence_mode': 'strict'}, technical=f, open_actions=('hold', 'exit'))
        self.assertEqual(packet['data_quality']['level'], 'LLM_INSUFFICIENT')
        self.assertIn('atr14', packet['data_quality']['technical_required_missing'])

    def test_legacy_packet_gains_no_new_keys(self):
        """关闭新功能时逐字段与引入前相同 —— 新增键会改变 packet_id（即另一份包）。"""
        packet = _packet(with_technical=False, open_actions=None)
        self.assertNotIn('technical', packet)
        self.assertNotIn('allowed_action_set', packet)
        self.assertEqual(set(packet['data_quality']),
                         {'level', 'critical_missing', 'dropped_event_count', 'fetch_status',
                          'evidence_mode'})


class OpenActionSetTests(unittest.TestCase):
    def test_only_the_open_actions_are_offered(self):
        packet = _packet()
        self.assertEqual(packet['allowed_action_set'], ['exit', 'hold'])
        self.assertEqual({t['action'] for t in packet['allowed_actions']}, {'hold', 'exit'})

    def test_an_action_outside_the_open_set_is_rejected(self):
        """`post_exit_review` 不要求选模板，靠「模板里没有」挡不住 —— 必须显式拒。"""
        packet = _packet()
        output = {'schema_version': 'v2', 'packet_id': packet['packet_id'],
                  'status': 'complete', 'thesis_state': 'CONFIRMED',
                  'action': 'post_exit_review', 'action_template_id': None,
                  'confidence': 'medium', 'reason_codes': [], 'facts': [],
                  'inferences': [], 'counterevidence': [],
                  'missing_information': ['缺口']}
        ok, errors = validate_position_output(output, packet)
        self.assertFalse(ok)
        self.assertTrue(any('ACTION_NOT_OPEN' in e for e in errors), errors)

    def test_exit_is_accepted_when_it_cites_a_technical_fact(self):
        packet = _packet()
        item = packet['new_evidence'][0]
        output = {'schema_version': 'v2', 'packet_id': packet['packet_id'],
                  'status': 'complete', 'thesis_state': 'INVALIDATED', 'action': 'exit',
                  'action_template_id': None, 'confidence': 'medium',
                  'reason_codes': ['THESIS_INVALIDATED'],
                  'facts': [{'text': item['summary'], 'claim_type': 'fact',
                             'evidence_ids': [item['evidence_id']]}],
                  'inferences': [], 'counterevidence': [],
                  'missing_information': ['无公司事件证据']}
        ok, errors = validate_position_output(output, packet)
        self.assertTrue(ok, errors)

    def test_exit_is_structurally_impossible_without_citable_evidence(self):
        """`validate_position_v2` 对 reduce/exit 要求**至少引用一条证据**（契约第 276-280 行）。

        没有公司事件源时，唯一能引用的就是技术事实 —— 所以技术包不是「锦上添花」，
        它是 EXIT 这个动作**能不能存在**的前提。本测试把这条依赖关系固定下来：
        去掉技术条目后，同一个 exit 输出立刻被拒。
        """
        packet = _packet()
        empty = {**packet, 'new_evidence': []}
        output = {'schema_version': 'v2', 'packet_id': packet['packet_id'],
                  'status': 'complete', 'thesis_state': 'INVALIDATED', 'action': 'exit',
                  'action_template_id': None, 'confidence': 'medium',
                  'reason_codes': ['THESIS_INVALIDATED'], 'facts': [], 'inferences': [],
                  'counterevidence': [], 'missing_information': ['无公司事件证据']}
        ok, errors = validate_position_output(output, empty)
        self.assertFalse(ok)
        self.assertTrue(any('必须引用至少一条证据' in e for e in errors), errors)

    def test_packets_without_the_declaration_keep_accepting_every_contract_action(self):
        packet = _packet(open_actions=None)
        output = {'schema_version': 'v2', 'packet_id': packet['packet_id'],
                  'status': 'complete', 'thesis_state': 'CONFIRMED',
                  'action': 'post_exit_review', 'action_template_id': None,
                  'confidence': 'medium', 'reason_codes': [], 'facts': [], 'inferences': [],
                  'counterevidence': [], 'missing_information': ['缺口']}
        ok, errors = validate_position_output(output, packet)
        self.assertFalse(any('ACTION_NOT_OPEN' in e for e in errors),
                         f'未声明动作集时不得改变原有行为：{errors}')


class SchemaKeysTests(unittest.TestCase):
    def test_the_new_llm_policy_keys_are_validated(self):
        from scripts.portfolio_shadow.schema import POLICY_KEYS
        self.assertIn('technical_packet', POLICY_KEYS['llm_policy'])
        self.assertIn('open_actions', POLICY_KEYS['llm_policy'])

    def test_manifest_rejects_half_configured_batches(self):
        from scripts.portfolio_shadow.schema import Manifest
        base = dict(experiment_id='x', status='DRAFT', parent_strategy_id='B3',
                    parent_version='1', parent_code_hash='a', universe_id='u',
                    universe_hash='h', account_scopes=('SHADOW:x:R',),
                    initial_cash=to_micro(100000),
                    risk_policy={'single_position_risk_bp': 100, 'max_weight_bp': 2000,
                                 'max_positions': 5},
                    execution_policy={'entry_rule': 'b3', 'exit_policy_id': 'H60',
                                      'horizon': 60},
                    calendar_version='v1',
                    evaluation_protocol={'main_metric': 'L_minus_R_return',
                                         'enrollment_window': '3-6 months',
                                         'review_date': '2026-12-31',
                                         'cost_allocation': 'L_pays_model_cost'})
        both = {'overlay': 'fixed_pass', 'position_overlay': 'position_action',
                'evidence_mode': 'strict', 'evidence_window_days': 5,
                'evidence_max_events': 50, 'technical_packet': True,
                'open_actions': ['hold', 'exit']}
        self.assertEqual(Manifest(llm_policy=dict(both), **base).validate(), [])
        # 只开技术包、不给动作集 ⇒ 限定角色失效
        only_tech = {**both}
        only_tech.pop('open_actions')
        self.assertTrue(Manifest(llm_policy=only_tech, **base).validate())
        # 动作拼错 ⇒ 必须报错（拼错的键会被静默忽略，安全门就无声失效）
        typo = {**both, 'open_actions': ['hold', 'exitt']}
        self.assertTrue(Manifest(llm_policy=typo, **base).validate())
        # 技术包但持仓角色没开 ⇒ 没有消费者
        no_role = {**both, 'position_overlay': 'off'}
        self.assertTrue(Manifest(llm_policy=no_role, **base).validate())


if __name__ == '__main__':
    unittest.main()
