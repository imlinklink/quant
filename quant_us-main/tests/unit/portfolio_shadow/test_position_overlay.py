"""持仓 overlay + 冻结证据包：契约、质量门、动作映射与租约复用。"""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.portfolio_shadow.overlay_review import OverlaySpec
from scripts.portfolio_shadow.position_overlay import (
    POSITION_ABSTAIN, POSITION_ACTION_SCHEMA_VERSION, POSITION_EXIT, POSITION_HOLD,
    POSITION_TIGHTEN_NOT_APPLIED, FakePositionModel, applied_action, reduce_action,
    reduce_tier, decide_position_overlay, resolve_position_overlay, validate_position_output)
from scripts.portfolio_shadow.position_packet import build_position_packet
from scripts.portfolio_shadow.position_review import (POSITION_ACTION_SPEC, PositionReviewer,
                                                      PositionSubject)
from scripts.portfolio_shadow.schema import Manifest, to_micro
from scripts.portfolio_shadow.store import ShadowStore

SUMMARY = '测试用事实陈述'
CODE = 'US.AAPL'


def event(security_id=CODE, published='2026-01-05T21:00:00+00:00',
          observed='2026-01-05T21:05:00+00:00', summary=SUMMARY, cluster='c1'):
    return {'evidence_id': f'ev-{security_id}-{cluster}', 'security_id': security_id,
            'source': 'test', 'source_url': 'http://x', 'kind': 'news',
            'event_type': 'news', 'published_at': published, 'observed_at': observed,
            'cluster_id': cluster, 'title': 't', 'summary': summary, 'excerpt': summary,
            'summary_truncated': False, 'content_hash': 'h1'}


def build_packet(events=None, *, shares=100, mark=to_micro(100), stop=to_micro(92),
                 as_of='2026-01-05T22:00:00+00:00'):
    return build_position_packet(
        security_id=CODE, trade={'entry_session': '2025-12-01'},
        protection={'active_stop': stop, 'initial_stop': stop,
                    'hard_exit_authoritative': True},
        events=events if events is not None else [event()], as_of=as_of,
        execution_session='2026-01-06', account_scope='SHADOW:x:L', experiment_id='x',
        opportunity_id='opp1', reviewed_session='2026-01-05', shares=shares,
        entry_price_micro=to_micro(95), mark_price_micro=mark,
        market_context={'observed_at': '2026-01-05T21:00:00+00:00'})


def template_id(packet, action, tier=None):
    for t in packet['allowed_actions']:
        if t['action'] != action:
            continue
        if tier is not None and (t.get('constraints') or {}).get('tier') != tier:
            continue
        return t['template_id']
    raise AssertionError(f'no template {action} {tier}')


def output(packet, action, *, template=None, reason='THESIS_WEAKENED',
           evidence_ids=(f'ev-{CODE}-c1',), counterevidence=(), missing=('缺口',)):
    return {
        'schema_version': 'position-v2', 'packet_id': packet['packet_id'],
        'status': 'complete', 'thesis_state': 'CONFIRMED', 'action': action,
        'action_template_id': template, 'confidence': 'medium', 'reason_codes': [reason],
        'facts': [{'text': SUMMARY, 'claim_type': 'fact', 'evidence_ids': list(evidence_ids)}]
                 if evidence_ids else [],
        'inferences': [], 'counterevidence': [],
        'missing_information': list(missing),
    }


class PacketTests(unittest.TestCase):
    def test_packet_shape_is_position_v2_compatible(self):
        packet = build_packet()
        for key in ('context', 'trade', 'identity', 'protection', 'new_evidence',
                    'allowed_actions', 'data_quality', 'packet_id', 'subject_key'):
            self.assertIn(key, packet)
        self.assertEqual(packet['data_quality']['level'], 'OK')
        # 主体键按执行日定键：结算时可直接按执行日取回
        self.assertEqual(packet['subject_key'], 'opp1@pos:2026-01-06')

    def test_missing_mark_price_blocks(self):
        packet = build_packet(mark=None)
        self.assertEqual(packet['data_quality']['level'], 'BLOCK')
        self.assertIn('trade.mark_price', packet['data_quality']['critical_missing'])

    def test_zero_shares_blocks(self):
        packet = build_packet(shares=0)
        self.assertEqual(packet['data_quality']['level'], 'BLOCK')

    def test_no_events_is_llm_insufficient_not_block(self):
        packet = build_packet(events=[])
        self.assertEqual(packet['data_quality']['level'], 'LLM_INSUFFICIENT')

    def test_evidence_keeps_true_subject(self):
        # 市场级证据不得被改写成公司事实：subject_code 保留真实归属
        packet = build_packet(events=[event(), event(security_id='MARKET', cluster='m1')])
        subjects = {e['subject_code'] for e in packet['new_evidence']}
        self.assertEqual(subjects, {CODE, 'MARKET'})

    def test_future_event_is_dropped(self):
        packet = build_packet(events=[event(published='2026-01-05T23:00:00+00:00',
                                            observed='2026-01-05T23:01:00+00:00')])
        self.assertEqual(packet['data_quality']['level'], 'LLM_INSUFFICIENT')
        self.assertEqual(packet['data_quality']['dropped_event_count'], 1)

    def test_packet_id_is_content_addressed(self):
        self.assertEqual(build_packet()['packet_id'], build_packet()['packet_id'])
        self.assertNotEqual(build_packet()['packet_id'],
                            build_packet(shares=101)['packet_id'])


class ValidationTests(unittest.TestCase):
    def test_packet_binding_is_required(self):
        packet = build_packet()
        bad = output(packet, 'hold')
        bad['packet_id'] = 'other'
        ok, errors = validate_position_output(bad, packet)
        self.assertFalse(ok)
        self.assertIn('PACKET_MISMATCH', errors)

    def test_hold_passes_and_delegates_to_v2(self):
        packet = build_packet()
        ok, errors = validate_position_output(output(packet, 'hold'), packet)
        self.assertTrue(ok, errors)

    def test_complete_without_any_claim_is_rejected(self):
        # 无证据 ⇒ facts/inferences 必空 ⇒ status='complete' 非法（v2 的既有规则）。
        # 这不影响可用性：这种包必然是 LLM_INSUFFICIENT，质量门在调用前就短路了。
        packet = build_packet()
        ok, errors = validate_position_output(
            output(packet, 'hold', evidence_ids=(), missing=()), packet)
        self.assertFalse(ok)
        self.assertTrue(any('complete' in e for e in errors), errors)

    def test_v2_rules_still_apply(self):
        packet = build_packet()
        # reduce 必须选模板
        ok, errors = validate_position_output(output(packet, 'reduce'), packet)
        self.assertFalse(ok)
        self.assertTrue(any('必须选择 action_template_id' in e for e in errors), errors)

    def test_reduce_can_be_driven_by_market_evidence(self):
        """归属不再拦截（见 `llm_overlay.validate_model_output` 同一处说明）。

        此前 `POSITION_NO_COMPANY_EVIDENCE` 要求 reduce/exit 引用本证券证据；实测证据供给
        只有市场级日报 ⇒ reduce/exit **结构上不可达**、L 恒等于 R。现放宽到设计与审计的原始
        要求：≥1 条有效引用即可，归属构成由报告披露。
        """
        packet = build_packet(events=[event(security_id='MARKET', cluster='m1')])
        market_reduce = output(packet, 'reduce', template=template_id(packet, 'reduce', 0.25),
                              evidence_ids=('ev-MARKET-m1',))
        ok, errors = validate_position_output(market_reduce, packet)
        self.assertTrue(ok, errors)

    def test_reduce_without_any_citation_is_rejected(self):
        packet = build_packet()
        bad = output(packet, 'reduce', template=template_id(packet, 'reduce', 0.25),
                     evidence_ids=(), missing=())
        ok, errors = validate_position_output(bad, packet)
        self.assertFalse(ok)
        self.assertTrue(any('引用' in e for e in errors), errors)

    def test_reduce_with_company_evidence_passes(self):
        packet = build_packet(events=[event(), event(security_id='MARKET', cluster='m1')])
        good = output(packet, 'reduce', template=template_id(packet, 'reduce', 0.25))
        ok, errors = validate_position_output(good, packet)
        self.assertTrue(ok, errors)


class ActionMappingTests(unittest.TestCase):
    def test_reduce_action_round_trips_tier(self):
        self.assertEqual(reduce_action(0.25), 'POSITION_REDUCE_25')
        self.assertEqual(reduce_tier('POSITION_REDUCE_25'), 0.25)
        self.assertEqual(reduce_tier('POSITION_REDUCE_50'), 0.5)
        self.assertIsNone(reduce_tier(POSITION_HOLD))

    def test_actions_map_to_application_vocabulary(self):
        packet = build_packet()
        cases = {
            ('hold', None): POSITION_HOLD,
            ('post_exit_review', None): POSITION_HOLD,
            ('exit', None): POSITION_EXIT,
            ('tighten_protection', None): POSITION_TIGHTEN_NOT_APPLIED,
            ('reduce', 0.25): 'POSITION_REDUCE_25',
            ('reduce', 0.5): 'POSITION_REDUCE_50',
        }
        for (action, tier), expected in cases.items():
            tmpl = template_id(packet, action, tier) if action not in ('hold',
                                                                       'post_exit_review') else None
            out = output(packet, action, template=tmpl)
            self.assertEqual(applied_action(out, packet), expected, action)


class ResolveTests(unittest.TestCase):
    def test_hold_model_result_resolves_to_hold(self):
        packet = build_packet()
        d = resolve_position_overlay(packet, {
            'status': 'OK', 'output': output(packet, 'hold'),
            'completed_at': '2026-01-05T21:30:00+00:00', 'cost_micro': 250,
            'cost_uncertain': False}, '2026-01-06T14:20:00+00:00')
        self.assertEqual(d.action, POSITION_HOLD)
        self.assertEqual(d.model_cost, 250)

    def test_late_response_degrades_to_program_abstain(self):
        packet = build_packet()
        d = resolve_position_overlay(packet, {
            'status': 'OK', 'output': output(packet, 'hold'),
            'completed_at': '2026-01-06T15:00:00+00:00', 'cost_micro': 250,
            'cost_uncertain': False}, '2026-01-06T14:20:00+00:00')
        self.assertEqual(d.action, POSITION_ABSTAIN)
        self.assertEqual(d.reason_code, 'LATE_RESPONSE')
        self.assertTrue(d.late_response_observed)
        self.assertEqual(d.model_cost, 250)  # 迟到也要计费

    def test_invalid_output_degrades_but_raw_action_is_kept(self):
        packet = build_packet()
        bad = output(packet, 'exit')
        bad['packet_id'] = 'other'
        d = resolve_position_overlay(packet, {
            'status': 'OK', 'output': bad, 'completed_at': '2026-01-05T21:30:00+00:00',
            'cost_micro': 100, 'cost_uncertain': False}, '2026-01-06T14:20:00+00:00')
        self.assertEqual(d.action, POSITION_ABSTAIN)
        self.assertEqual(d.reason_code, 'INVALID_OUTPUT')
        self.assertEqual(d.raw_action, 'exit')

    def test_unknown_cost_is_not_treated_as_free(self):
        packet = build_packet()
        d = resolve_position_overlay(packet, {
            'status': 'OK', 'output': output(packet, 'hold'),
            'completed_at': '2026-01-05T21:30:00+00:00', 'cost_micro': None,
            'cost_uncertain': True}, '2026-01-06T14:20:00+00:00')
        self.assertEqual(d.model_cost, 0)
        self.assertTrue(d.cost_uncertain)

    def test_program_status_short_circuits(self):
        packet = build_packet()
        d = resolve_position_overlay(packet, {'status': 'MODEL_KNOWLEDGE_CUTOFF'},
                                    '2026-01-06T14:20:00+00:00')
        self.assertEqual(d.action, POSITION_ABSTAIN)
        self.assertEqual(d.reason_code, 'MODEL_KNOWLEDGE_CUTOFF')


class DecideGateTests(unittest.TestCase):
    def test_block_gate_does_not_call_model_and_costs_nothing(self):
        packet = build_packet(mark=None)

        class Exploding:
            def call(self, p, d):
                raise AssertionError('质量门短路时不得调用模型')

        d = decide_position_overlay(packet, Exploding(), '2026-01-06T14:20:00+00:00')
        self.assertEqual(d.action, POSITION_ABSTAIN)
        self.assertEqual(d.reason_code, 'DATA_BLOCKED_QUOTE')
        self.assertEqual(d.model_cost, 0)
        self.assertFalse(d.cost_uncertain)

    def test_insufficient_evidence_gate_does_not_call_model(self):
        packet = build_packet(events=[])

        class Exploding:
            def call(self, p, d):
                raise AssertionError('质量门短路时不得调用模型')

        d = decide_position_overlay(packet, Exploding(), '2026-01-06T14:20:00+00:00')
        self.assertEqual(d.action, POSITION_ABSTAIN)
        self.assertEqual(d.reason_code, 'INSUFFICIENT_EVIDENCE')

    def test_ok_packet_calls_model(self):
        packet = build_packet()
        model = FakePositionModel(action='hold', evidence_from_packet=True)
        d = decide_position_overlay(packet, model, '2026-01-06T14:20:00+00:00')
        self.assertEqual(d.action, POSITION_HOLD)


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ShadowStore(Path(self.tmp.name) / 's.db', 'x')
        self.store.save_experiment(Manifest(
            experiment_id='x', status='DRAFT', parent_strategy_id='B3',
            parent_version='1', parent_code_hash='abc', universe_id='u',
            universe_hash='uh', account_scopes=('SHADOW:x:R', 'SHADOW:x:L'),
            initial_cash=to_micro(100000),
            risk_policy={'single_position_risk_bp': 100, 'max_weight_bp': 2000,
                         'max_positions': 5},
            execution_policy={'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': 60},
            llm_policy={'overlay': 'entry_veto', 'evidence_mode': 'strict',
                        'evidence_window_days': 7, 'evidence_max_events': 50},
            calendar_version='v1',
            evaluation_protocol={'main_metric': 'L_minus_R_return',
                                 'enrollment_window': '3-6 months',
                                 'review_date': '2026-12-31',
                                 'cost_allocation': 'L_pays_model_cost'}).freeze('2026-01-02'))
        self.subject = PositionSubject(opportunity_id='opp1', security_id=CODE,
                                       reviewed_session='2026-01-05',
                                       execution_session='2026-01-06')

    def tearDown(self):
        self.tmp.cleanup()

    def reviewer(self, model):
        return PositionReviewer(self.store, scope='SHADOW:x:L',
                                model_factory=lambda: model, model_id='fixture')

    def test_review_freezes_action_and_rerun_reuses_without_second_call(self):
        packet = build_packet()
        calls = []

        def factory():
            calls.append(1)
            return FakePositionModel(action='reduce',
                                     template_id=template_id(packet, 'reduce', 0.25),
                                     evidence_from_packet=True)
        reviewer = PositionReviewer(self.store, scope='SHADOW:x:L', model_factory=factory,
                                    model_id='fixture')
        first = reviewer.review(self.subject, packet, '2026-01-06T14:20:00+00:00')
        self.assertTrue(first.frozen)
        self.assertEqual(first.decision.action, 'POSITION_REDUCE_25')
        self.assertEqual(len(calls), 1)

        second = reviewer.review(self.subject, packet, '2026-01-06T14:20:00+00:00')
        self.assertEqual(second.note, 'reused_frozen')
        self.assertEqual(len(calls), 1, '重跑不得再次调用付费模型')

        app = self.store.application('SHADOW:x:L', self.subject.key())
        self.assertEqual(app['action'], 'POSITION_REDUCE_25')
        self.assertTrue(app['decision_frozen'])
        self.assertFalse(app['execution_applied'], '冻结 ≠ 成交')

    def test_changed_packet_on_rerun_is_rejected(self):
        packet = build_packet()
        reviewer = self.reviewer(FakePositionModel(action='hold',
                                                   evidence_from_packet=True))
        reviewer.review(self.subject, packet, '2026-01-06T14:20:00+00:00')
        changed = build_packet(shares=101)
        with self.assertRaises(ValueError) as ctx:
            reviewer.review(self.subject, changed, '2026-01-06T14:20:00+00:00')
        self.assertIn('PACKET_CHANGED_ON_RERUN', str(ctx.exception))

    def test_gated_packet_freezes_abstain_at_zero_cost(self):
        packet = build_packet(mark=None)

        def factory():
            raise AssertionError('质量门短路时不得构造模型')

        reviewer = PositionReviewer(self.store, scope='SHADOW:x:L', model_factory=factory,
                                    model_id='fixture')
        out = reviewer.review(self.subject, packet, '2026-01-06T14:20:00+00:00')
        self.assertEqual(out.decision.action, POSITION_ABSTAIN)
        self.assertEqual(out.note, 'gated')
        app = self.store.application('SHADOW:x:L', self.subject.key())
        self.assertEqual(app['model_cost'], 0)
        self.assertFalse(app['cost_uncertain'])


if __name__ == '__main__':
    unittest.main()


class FixtureTemplateResolutionTests(unittest.TestCase):
    """夹具必须**从包里解析出真实模板 id**，不能用后缀冒充。

    回归背景：`model_parts` 曾把 `':25'` 当模板 id 传给夹具，而真实 id 形如
    `US.AAPL@2026-01-05:reduce:25` ⇒ 校验器判「模板不存在」⇒ 整个决策降级
    ABSTAIN —— `--fixture-action reduce_25` 从来就没生效过，且**不报错**。
    """

    def test_tier_resolves_to_a_real_template_id(self):
        packet = build_packet()
        model = FakePositionModel(action='reduce', tier=0.25, evidence_from_packet=True)
        out = model.call(packet, '2026-01-06T14:20:00+00:00')['output']
        ids = {t['template_id'] for t in packet['allowed_actions']}
        self.assertIn(out['action_template_id'], ids)
        ok, errors = validate_position_output(out, packet)
        self.assertTrue(ok, errors)

    def test_the_resolved_template_carries_the_requested_tier(self):
        packet = build_packet()
        model = FakePositionModel(action='reduce', tier=0.5, evidence_from_packet=True)
        out = model.call(packet, '2026-01-06T14:20:00+00:00')['output']
        template = next(t for t in packet['allowed_actions']
                        if t['template_id'] == out['action_template_id'])
        self.assertEqual(template['constraints']['tier'], 0.5)

    def test_an_explicit_template_id_still_wins(self):
        packet = build_packet()
        wanted = next(t['template_id'] for t in packet['allowed_actions']
                      if t.get('constraints', {}).get('tier') == 0.25)
        model = FakePositionModel(action='reduce', template_id=wanted,
                                  evidence_from_packet=True)
        out = model.call(packet, '2026-01-06T14:20:00+00:00')['output']
        self.assertEqual(out['action_template_id'], wanted)

    def test_cli_fixture_action_maps_to_a_tier_not_a_suffix(self):
        from scripts.portfolio_shadow.cli import model_parts
        self.assertEqual(model_parts('reduce_25'), ('reduce', 0.25))
        self.assertEqual(model_parts('reduce_50'), ('reduce', 0.5))
        self.assertEqual(model_parts('exit'), ('exit', None))
        self.assertEqual(model_parts('hold'), ('hold', None))
