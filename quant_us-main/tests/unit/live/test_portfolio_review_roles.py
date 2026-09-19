"""Portfolio（§6.3）与 Review（§6.5）：契约、构造性约束与"不得自动生效"。

这两个角色是 P2，安全性靠**构造**而不是事后拦截，所以测试的重点是"不可能发生"：
  - Portfolio：任何模板都不突破限额、不含未合格证券 —— 因为唯一的分配者是程序。
  - Review：没有任何动作能改动配置或权限；候选必须人工批准，且批准也不改配置。
"""
import json
import tempfile
import unittest
from pathlib import Path

from mutifactor.llm.contracts.portfolio_v1 import (PORTFOLIO_ACTIONS,
                                                   apply_portfolio_choice,
                                                   build_portfolio_templates,
                                                   normalize_portfolio_output,
                                                   validate_portfolio_v1)
from mutifactor.llm.contracts.review_v1 import (CHANGEABLE_VARIABLES,
                                                FORBIDDEN_VARIABLES,
                                                normalize_review_output,
                                                protocol_candidate_from, validate_review_v1)
from scripts.live_trading.decision_contracts import ROLE_CONTRACTS
from scripts.live_trading.decision_ledger.decision_run_store import VALID_ROLES as STORE_ROLES
from scripts.live_trading.decision_engine import VALID_ROLES as ENGINE_ROLES
from scripts.live_trading.decision_ledger.protocol_changes import (approve, candidates,
                                                                  pending, record_candidate,
                                                                  reject)
from scripts.live_trading.decision_ledger.event_store import EventStore
from scripts.live_trading.llm_permission import ROLE_PERMISSIONS

LIMITS = {'max_positions': 2, 'max_total_risk_bp': 150, 'max_group_risk_bp': 100,
          'max_name_risk_bp': 100, 'cash_available_micro': 100_000}


def cand(code, rank, portfolio_rank, group='semis', risk=100, cost=1000):
    return {'security_id': code, 'rank': rank, 'portfolio_rank': portfolio_rank,
            'risk_bp': risk, 'risk_group': group, 'estimated_cost_micro': cost}


def portfolio_packet(**over):
    templates = build_portfolio_templates(candidates=over.pop(
        'candidates', [cand('US.A', 1, 2), cand('US.B', 2, 1)]),
        positions=over.pop('positions', []), limits=over.pop('limits', LIMITS))
    packet = {'packet_id': 'pkt', 'subject_code': 'US.A', 'identity': {'risk_group': 'semis'},
              'candidates': [cand('US.A', 1, 2), cand('US.B', 2, 1)],
              'new_evidence': [], 'templates': templates['templates'],
              'consult_required': templates['consult_required']}
    packet.update(over)
    return packet


def portfolio_output(packet, template_id, **over):
    out = {'schema_version': 'portfolio-v1', 'packet_id': packet['packet_id'],
           'status': 'complete', 'chosen_template_id': template_id,
           'reason_codes': [], 'facts': [], 'inferences': [], 'counterevidence': [],
           'missing_information': ['缺口']}
    out.update(over)
    return out


class TemplateConstructionTests(unittest.TestCase):
    def test_templates_never_breach_the_limits(self):
        """构造性保证：唯一分配者是程序，任何模板都落在限额内。"""
        candidates = [cand('US.A', 1, 1, group='semis'), cand('US.B', 2, 2, group='semis'),
                      cand('US.C', 3, 3, group='china'), cand('US.D', 4, 4, group='china')]
        built = build_portfolio_templates(candidates=candidates, positions=[],
                                          limits=LIMITS)
        self.assertTrue(built['templates'])
        for template in built['templates']:
            with self.subTest(template=template['template_id']):
                self.assertLessEqual(template['total_risk_bp'],
                                     LIMITS['max_total_risk_bp'])
                self.assertLessEqual(len(template['allocations']),
                                     LIMITS['max_positions'])
                self.assertLessEqual(template['cash_used_micro'],
                                     LIMITS['cash_available_micro'])
                self.assertGreaterEqual(template['cash_left_micro'], 0)
                for group, risk in template['group_risk_bp'].items():
                    self.assertLessEqual(risk, LIMITS['max_group_risk_bp'], group)

    def test_existing_positions_count_against_the_group_cap(self):
        built = build_portfolio_templates(
            candidates=[cand('US.A', 1, 1, group='semis')],
            positions=[{'security_id': 'US.X', 'risk_group': 'semis', 'risk_bp': 100}],
            limits=LIMITS)
        for template in built['templates']:
            self.assertEqual(template['allocations'], [])

    def test_templates_only_contain_eligible_candidates(self):
        built = build_portfolio_templates(
            candidates=[cand('US.A', 1, 1), cand('US.B', 2, 2)], positions=[],
            limits=LIMITS)
        eligible = {'US.A', 'US.B'}
        for template in built['templates']:
            self.assertTrue({a['security_id'] for a in template['allocations']} <= eligible)

    def test_model_order_template_only_when_it_differs(self):
        same = build_portfolio_templates(candidates=[cand('US.A', 1, 1)],
                                         positions=[], limits=LIMITS)
        self.assertNotIn('select_ranked_subset',
                         [t['template_id'] for t in same['templates']])
        different = build_portfolio_templates(
            candidates=[cand('US.A', 1, 2), cand('US.B', 2, 1)], positions=[],
            limits=LIMITS)
        self.assertIn('select_ranked_subset',
                      [t['template_id'] for t in different['templates']])

    def test_consult_required_only_when_capacity_is_contested(self):
        roomy = dict(LIMITS, max_positions=5, max_total_risk_bp=500,
                     max_group_risk_bp=500)
        self.assertFalse(build_portfolio_templates(
            candidates=[cand('US.A', 1, 1)], positions=[], limits=roomy)['consult_required'])
        self.assertTrue(build_portfolio_templates(
            candidates=[cand('US.A', 1, 1), cand('US.B', 2, 2)],
            positions=[], limits=LIMITS)['consult_required'])

    def test_no_candidates_means_nothing_to_allocate(self):
        built = build_portfolio_templates(candidates=[], positions=[], limits=LIMITS)
        self.assertEqual(built['templates'], [])
        self.assertFalse(built['consult_required'])


class PortfolioValidationTests(unittest.TestCase):
    def test_unknown_template_is_rejected(self):
        """模型不得发明模板 —— 这是 Portfolio 的主要安全界。"""
        packet = portfolio_packet()
        errors = validate_portfolio_v1(portfolio_output(packet, 'invent_a_template'),
                                       packet)
        self.assertTrue(any('模板不存在' in e for e in errors), errors)

    def test_valid_choice_passes(self):
        packet = portfolio_packet()
        errors = validate_portfolio_v1(
            portfolio_output(packet, 'keep_rule_allocation'), packet)
        self.assertEqual(errors, [])

    def test_packet_binding_is_required(self):
        packet = portfolio_packet()
        bad = portfolio_output(packet, 'keep_rule_allocation', packet_id='other')
        self.assertIn('PACKET_MISMATCH', validate_portfolio_v1(bad, packet))

    def test_path_changing_choice_requires_a_citation(self):
        packet = portfolio_packet(candidates=[cand('US.A', 1, 2), cand('US.B', 2, 1)])
        errors = validate_portfolio_v1(
            portfolio_output(packet, 'select_ranked_subset'), packet)
        self.assertTrue(any('比较依据' in e for e in errors), errors)

    def test_template_containing_an_ineligible_security_is_rejected(self):
        packet = portfolio_packet()
        packet['templates'] = [{'template_id': 'sneaky', 'action': 'hold_cash_buffer',
                                'allocations': [{'security_id': 'US.UNVETTED',
                                                 'risk_bp': 1, 'estimated_cost_micro': 1}],
                                'total_risk_bp': 1, 'cash_used_micro': 1,
                                'cash_left_micro': 1, 'group_risk_bp': {},
                                'rejected': []}]
        errors = validate_portfolio_v1(portfolio_output(packet, 'sneaky'), packet)
        self.assertTrue(any('未通过 Selection/Entry' in e for e in errors), errors)

    def test_normalize_maps_the_template_to_the_action_vocabulary(self):
        packet = portfolio_packet()
        out = normalize_portfolio_output(
            portfolio_output(packet, 'select_ranked_subset'), packet)
        self.assertEqual(out['action'], 'select_ranked_subset')
        self.assertIn(out['action'], PORTFOLIO_ACTIONS)

    def test_apply_falls_back_to_the_rule_allocation(self):
        packet = portfolio_packet()
        plan = apply_portfolio_choice(portfolio_output(packet, 'nope'), packet)
        self.assertEqual(plan['template_id'], 'keep_rule_allocation')
        self.assertTrue(plan['fell_back_to_rule'])

    def test_apply_never_returns_allocations_outside_a_template(self):
        packet = portfolio_packet()
        plan = apply_portfolio_choice(
            portfolio_output(packet, 'keep_rule_allocation'), packet)
        template = next(t for t in packet['templates']
                        if t['template_id'] == 'keep_rule_allocation')
        self.assertEqual(plan['allocations'], template['allocations'])


def review_packet(**over):
    packet = {'packet_id': 'rpkt', 'protocol_version': 'v1',
              'sample_groups': [{'name': 'semis/THESIS_WEAKENED'}],
              'role_stats': {}, 'changeable_variables': sorted(CHANGEABLE_VARIABLES)}
    packet.update(over)
    return packet


def review_output(packet, **over):
    out = {'schema_version': 'review-v1', 'packet_id': packet['packet_id'],
           'status': 'complete', 'failure_patterns': [],
           'proposed_change': None, 'expected_improvement': None,
           'possible_regression': [], 'validation_plan': None,
           'reason_codes': [], 'missing_information': ['样本不足']}
    out.update(over)
    return out


PROPOSAL = {'variable': 'execution_policy.horizon', 'from_value': 60, 'to_value': 45,
            'direction': 'decrease'}


class ReviewValidationTests(unittest.TestCase):
    def test_no_change_with_missing_information_is_accepted(self):
        """样本不足时"不提改动"才是正确答案，不是失败。"""
        packet = review_packet()
        self.assertEqual(validate_review_v1(review_output(packet), packet), [])

    def test_complete_without_a_change_must_explain_the_gap(self):
        packet = review_packet()
        errors = validate_review_v1(
            review_output(packet, missing_information=[]), packet)
        self.assertTrue(any('missing_information' in e for e in errors), errors)

    def test_a_proposal_needs_a_validation_plan_and_a_regression_risk(self):
        packet = review_packet()
        errors = validate_review_v1(
            review_output(packet, proposed_change=PROPOSAL,
                          expected_improvement={'metric': 'calmar', 'direction': 'increase'},
                          possible_regression=[], validation_plan=None), packet)
        self.assertTrue(any('possible_regression' in e for e in errors), errors)
        self.assertTrue(any('validation_plan' in e for e in errors), errors)

    def test_stop_condition_is_mandatory(self):
        packet = review_packet()
        errors = validate_review_v1(review_output(
            packet, proposed_change=PROPOSAL,
            expected_improvement={'metric': 'calmar', 'direction': 'increase'},
            possible_regression=[{'metric': 'turnover', 'direction': 'increase'}],
            validation_plan={'window_sessions': 60, 'min_samples': 30,
                             'stop_condition': '   '}), packet)
        self.assertTrue(any('停止条件' in e for e in errors), errors)

    def test_safety_switches_can_never_be_proposed(self):
        """白名单刻意不含任何安全开关：放松自身约束必须是构造上不可能的。"""
        packet = review_packet()
        for variable in FORBIDDEN_VARIABLES:
            with self.subTest(variable=variable):
                errors = validate_review_v1(review_output(
                    packet, proposed_change={**PROPOSAL, 'variable': variable},
                    possible_regression=[{'metric': 'x', 'direction': 'increase'}],
                    validation_plan={'window_sessions': 1, 'min_samples': 1,
                                     'stop_condition': 'x'}), packet)
                self.assertTrue(any('禁止提议修改安全相关变量' in e for e in errors), errors)

    def test_unknown_variable_is_rejected(self):
        packet = review_packet()
        errors = validate_review_v1(review_output(
            packet, proposed_change={**PROPOSAL, 'variable': 'something.else'},
            possible_regression=[{'metric': 'x', 'direction': 'increase'}],
            validation_plan={'window_sessions': 1, 'min_samples': 1,
                             'stop_condition': 'x'}), packet)
        self.assertTrue(any('白名单' in e for e in errors), errors)

    def test_direction_whitelist_is_enforced(self):
        """风险预算只能**降低** —— 让模型有权提议加风险是最不该有的能力。"""
        packet = review_packet()
        errors = validate_review_v1(review_output(
            packet, proposed_change={'variable': 'risk_policy.single_position_risk_bp',
                                     'from_value': 100, 'to_value': 200,
                                     'direction': 'increase'},
            possible_regression=[{'metric': 'x', 'direction': 'increase'}],
            validation_plan={'window_sessions': 1, 'min_samples': 1,
                             'stop_condition': 'x'}), packet)
        self.assertTrue(any('只允许方向' in e for e in errors), errors)

    def test_failure_pattern_must_use_a_program_supplied_group(self):
        packet = review_packet()
        errors = validate_review_v1(review_output(
            packet, failure_patterns=[{'pattern': 'p', 'sample_group': 'invented',
                                       'observed': {}}]), packet)
        self.assertTrue(any('未知样本组' in e for e in errors), errors)

    def test_normalize_maps_to_the_action_vocabulary(self):
        packet = review_packet()
        self.assertEqual(
            normalize_review_output(review_output(packet,
                                                  proposed_change=PROPOSAL),
                                    packet)['action'], 'propose_change')
        self.assertEqual(normalize_review_output(review_output(packet), packet)['action'],
                         'no_change')

    def test_candidate_extraction_returns_none_without_a_change(self):
        packet = review_packet()
        self.assertIsNone(protocol_candidate_from(review_output(packet), packet))
        candidate = protocol_candidate_from(
            review_output(packet, proposed_change=PROPOSAL), packet)
        self.assertEqual(candidate['variable'], 'execution_policy.horizon')


class ProtocolChangeLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from scripts.live_trading.position_registry import PositionRegistry
        self.registry = PositionRegistry(Path(self.tmp.name) / 'x.sqlite3', 'DRY-RUN')

    def tearDown(self):
        self.tmp.cleanup()

    def candidate(self, **over):
        base = {'packet_id': 'pkt', 'variable': 'execution_policy.horizon',
                'from_value': 60, 'to_value': 45, 'base_protocol_version': 'v1',
                'validation_plan': {'window_sessions': 60, 'min_samples': 30,
                                    'stop_condition': 'x'}}
        base.update(over)
        return base

    def test_candidate_without_a_validation_plan_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            record_candidate(self.registry, self.candidate(validation_plan=None))
        self.assertIn('VALIDATION_PLAN', str(ctx.exception))

    def test_approval_requires_a_named_approver(self):
        cid = record_candidate(self.registry, self.candidate())
        with self.assertRaises(ValueError) as ctx:
            approve(self.registry, cid, approver='  ', target_version='v2')
        self.assertIn('APPROVER_REQUIRED', str(ctx.exception))

    def test_approval_must_point_at_a_new_version(self):
        """批准而不指向新版本，事后无法判断配置到底改没改 —— 是一张空头支票。"""
        from scripts.live_trading.decision_ledger.protocol_changes import adjudication
        cid = record_candidate(self.registry, self.candidate())
        with self.assertRaises(ValueError) as ctx:
            approve(self.registry, cid, approver='ops', target_version='')
        self.assertIn('TARGET_VERSION_REQUIRED', str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            approve(self.registry, cid, approver='ops', target_version='v1')
        self.assertIn('TARGET_VERSION_NOT_NEW', str(ctx.exception))
        approve(self.registry, cid, approver='ops', target_version='v2')
        row = candidates(self.registry)[0]
        self.assertEqual(row['decision']['target_protocol_version'], 'v2')
        # 配置版本还没推进到 v2 ⇒ 已批准但**未兑现**
        self.assertFalse(adjudication(row, current_version='v1')['carried_out'])
        self.assertTrue(adjudication(row, current_version='v2')['carried_out'])

    def test_rejection_requires_a_reason(self):
        cid = record_candidate(self.registry, self.candidate())
        with self.assertRaises(ValueError) as ctx:
            reject(self.registry, cid, approver='ops', note='  ')
        self.assertIn('REJECT_REASON_REQUIRED', str(ctx.exception))

    def test_approval_records_an_event_and_changes_nothing_else(self):
        """§6.5：批准也不改配置 —— 新版协议由人另行创建。"""
        cid = record_candidate(self.registry, self.candidate())
        before = json.dumps([e['event_type'] for e in EventStore(self.registry).events()])
        approve(self.registry, cid, approver='ops', target_version='v2', note='ok')
        self.assertEqual(json.dumps([c['candidate_id'] for c in candidates(self.registry)]),
                         json.dumps([cid]))
        self.assertEqual(candidates(self.registry)[0]['decision']['decision'], 'approved')
        self.assertNotEqual(before, json.dumps(
            [e['event_type'] for e in EventStore(self.registry).events()]))

    def test_report_flags_approved_but_not_carried_out(self):
        """「已批准」被读成「已经改了」是这条链上最容易发生的误解。"""
        from scripts.live_trading.decision_ledger.protocol_changes import report, render
        cid = record_candidate(self.registry, self.candidate())
        approve(self.registry, cid, approver='ops', target_version='v2')
        stale = report(self.registry, current_version='v1')
        self.assertEqual(len(stale['approved_not_carried_out']), 1)
        self.assertIn('未兑现', render(stale))
        fresh = report(self.registry, current_version='v2')
        self.assertEqual(fresh['approved_not_carried_out'], [])
        self.assertIn('已裁决', render(fresh))

    def test_pending_lists_only_unadjudicated_candidates(self):
        first = record_candidate(self.registry, self.candidate(to_value=45))
        record_candidate(self.registry, self.candidate(to_value=50))
        reject(self.registry, first, approver='ops', note='no')
        remaining = pending(self.registry)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]['to_value'], 50)

    def test_recording_the_same_proposal_twice_is_idempotent(self):
        first = record_candidate(self.registry, self.candidate())
        second = record_candidate(self.registry, self.candidate())
        self.assertEqual(first, second)
        self.assertEqual(len(candidates(self.registry)), 1)


class RoleWiringTests(unittest.TestCase):
    def test_five_roles_are_registered_everywhere(self):
        expected = {'selection', 'entry', 'position', 'portfolio', 'review'}
        self.assertEqual(set(ROLE_CONTRACTS), expected)
        self.assertEqual(set(ROLE_PERMISSIONS), expected)
        self.assertEqual(set(ENGINE_ROLES), expected)

    def test_role_lists_come_from_one_source(self):
        """曾有两份 VALID_ROLES：引擎从契约派生、快照层硬编码三个角色。
        新增角色时引擎能路由、快照层报「非法 role」——**静默不可用**。"""
        self.assertEqual(tuple(STORE_ROLES), tuple(ENGINE_ROLES))
        self.assertEqual(set(STORE_ROLES), set(ROLE_CONTRACTS))


class EngineRoutingTests(unittest.TestCase):
    """两个新角色必须真正走通 `DecisionEngine._decide`。

    这是被一次真 bug 逼出来的：`normalize` 跑在 `validate` **之前**，而 schema 是
    `additionalProperties: False` —— normalize 回填的 `action` 不在 properties 里，
    校验必然失败。只测纯函数（`validate_*` / `normalize_*`）**完全看不出这个问题**，
    因为它只在"归一化后再校验"这个顺序下才出现。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        from scripts.live_trading.position_registry import PositionRegistry
        self.registry = PositionRegistry(Path(self.tmp.name) / 'x.sqlite3', 'DRY-RUN')

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, output):
        from scripts.live_trading.decision_engine import DecisionEngine
        return DecisionEngine(self.registry, config={},
                              call_model=lambda contract, packet: output)

    def test_portfolio_choice_survives_the_engine(self):
        from scripts.live_trading.decision_bridge import build_portfolio_packet
        packet = build_portfolio_packet(
            candidates=[cand('US.A', 1, 2), cand('US.B', 2, 1)], positions=[],
            limits=LIMITS, new_evidence=[], account_scope='DRY-RUN', subject_id='pf',
            as_of='2026-09-19T00:00:00+00:00')
        output = {'schema_version': 'portfolio-v1', 'packet_id': packet['packet_id'],
                  'status': 'complete', 'chosen_template_id': 'keep_rule_allocation',
                  'reason_codes': [], 'facts': [], 'inferences': [], 'counterevidence': [],
                  'missing_information': ['缺口']}
        result = self.engine(output).decide_portfolio(packet)
        self.assertEqual(result.status, 'validated', result.validation_errors)
        self.assertEqual(result.model_action, 'keep_rule_allocation')
        self.assertEqual(result.permission_level, 'shadow')

    def test_review_proposal_survives_the_engine(self):
        from scripts.live_trading.decision_bridge import build_review_packet
        packet = build_review_packet(
            sample_groups=[{'name': 'g1'}], role_stats={}, protocol_version='v1',
            account_scope='DRY-RUN', subject_id='rw',
            as_of='2026-09-19T00:00:00+00:00')
        output = {'schema_version': 'review-v1', 'packet_id': packet['packet_id'],
                  'status': 'complete', 'failure_patterns': [],
                  'proposed_change': {'variable': 'execution_policy.horizon',
                                      'from_value': 60, 'to_value': 45,
                                      'direction': 'decrease'},
                  'expected_improvement': {'metric': 'calmar', 'direction': 'increase'},
                  'possible_regression': [{'metric': 'turnover', 'direction': 'increase'}],
                  'validation_plan': {'window_sessions': 60, 'min_samples': 30,
                                      'stop_condition': 'x'},
                  'reason_codes': [], 'missing_information': []}
        result = self.engine(output).decide_review(packet)
        self.assertEqual(result.status, 'validated', result.validation_errors)
        self.assertEqual(result.model_action, 'propose_change')

    def test_review_no_change_is_a_valid_outcome_not_a_failure(self):
        from scripts.live_trading.decision_bridge import build_review_packet
        packet = build_review_packet(
            sample_groups=[], role_stats={}, protocol_version='v1',
            account_scope='DRY-RUN', subject_id='rw',
            as_of='2026-09-19T00:00:00+00:00')
        output = {'schema_version': 'review-v1', 'packet_id': packet['packet_id'],
                  'status': 'complete', 'failure_patterns': [], 'proposed_change': None,
                  'expected_improvement': None, 'possible_regression': [],
                  'validation_plan': None, 'reason_codes': [],
                  'missing_information': ['样本不足']}
        result = self.engine(output).decide_review(packet)
        self.assertEqual(result.status, 'validated', result.validation_errors)
        self.assertEqual(result.model_action, 'no_change')


class ApprovalCliTests(unittest.TestCase):
    """人工批准入口（CLI）。**只写事件**，绝不改配置。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry_path = Path(self.tmp.name) / 'x.sqlite3'
        from scripts.live_trading.position_registry import PositionRegistry
        self.registry = PositionRegistry(self.registry_path, 'DRY-RUN')
        self.config_path = Path(self.tmp.name) / 'config.yaml'
        self.write_config('v1')
        self.cid = record_candidate(self.registry, {
            'packet_id': 'pkt', 'variable': 'execution_policy.horizon',
            'from_value': 60, 'to_value': 45, 'direction': 'decrease',
            'base_protocol_version': 'v1',
            'expected_improvement': {'metric': 'calmar', 'direction': 'increase'},
            'possible_regression': [{'metric': 'turnover', 'direction': 'increase'}],
            'validation_plan': {'window_sessions': 60, 'min_samples': 30,
                                'stop_condition': '超额转负即停'},
            'failure_patterns': [{'pattern': '慢速止损反复触发',
                                  'sample_group': 'semis|hold|MARKET|semis',
                                  'observed': {}}]})

    def write_config(self, version):
        # 账本 namespace 由 llm_decision.engine_v2.account_scope 决定 —— CLI 用它开账本。
        # 缺了它会退回到默认的 'unconfigured'，于是**读不到任何候选且看不出原因**。
        self.config_path.write_text(
            'llm_decision:\n'
            '  engine_v2:\n'
            '    account_scope: DRY-RUN\n'
            f'  protocol_review:\n    protocol_version: {version}\n',
            encoding='utf-8')

    def run_cli(self, argv):
        import io
        import contextlib
        from scripts.live_trading.decision_ledger.protocol_changes import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            main([*argv, '--config', str(self.config_path),
                  '--registry', str(self.registry_path)])
        return buf.getvalue()

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_shows_the_pending_candidate_with_its_evidence(self):
        out = self.run_cli(['list'])
        self.assertIn('待裁决（1）', out)
        self.assertIn(self.cid, out)
        self.assertIn('execution_policy.horizon', out)
        self.assertIn('慢速止损反复触发', out)
        self.assertIn('停止条件', out.replace('stop_condition', '停止条件'))
        self.assertIn('approve', out)          # 给出下一步该敲什么

    def test_approve_then_list_shows_it_as_approved_but_not_carried_out(self):
        out = self.run_cli(['approve', self.cid, '--approver', 'ops',
                            '--target-version', 'v2'])
        self.assertIn('未兑现', out)
        self.assertIn('v2', out)

    def test_carried_out_once_the_config_version_advances(self):
        self.run_cli(['approve', self.cid, '--approver', 'ops',
                      '--target-version', 'v2'])
        self.write_config('v2')
        out = self.run_cli(['list'])
        self.assertNotIn('未兑现', out)
        self.assertIn('已裁决（1）', out)

    def test_reject_requires_a_note(self):
        import argparse
        from scripts.live_trading.decision_ledger.protocol_changes import main
        with self.assertRaises(ValueError) as ctx:
            main(['reject', self.cid, '--approver', 'ops',
                  '--config', str(self.config_path),
                  '--registry', str(self.registry_path)])
        self.assertIn('REJECT_REASON_REQUIRED', str(ctx.exception))

    def test_the_cli_never_writes_config(self):
        """批准是记录，不是执行 —— 配置文件必须一字未动。"""
        before = self.config_path.read_text()
        self.run_cli(['approve', self.cid, '--approver', 'ops',
                      '--target-version', 'v2'])
        self.assertEqual(self.config_path.read_text(), before)


class HeldCapacityRegressionTests(unittest.TestCase):
    def test_existing_positions_consume_slots_and_total_risk(self):
        for limits, expected in ((dict(LIMITS, max_positions=1, max_total_risk_bp=1000), 'MAX_POSITIONS'),
                                 (dict(LIMITS, max_positions=5, max_total_risk_bp=100), 'TOTAL_RISK_BUDGET')):
            with self.subTest(expected=expected):
                result = build_portfolio_templates(
                    candidates=[cand('B', 1, 1, group='new')],
                    positions=[{'security_id': 'A', 'risk_bp': 100, 'risk_group': 'old'}],
                    limits=limits)
                self.assertTrue(result['consult_required'])
                self.assertEqual(result['templates'][0]['allocations'], [])
                self.assertEqual(result['templates'][0]['rejected'][0]['reason'], expected)
                self.assertEqual(result['templates'][0]['total_risk_bp'], 100)

    def test_remaining_capacity_can_still_be_allocated(self):
        result = build_portfolio_templates(
            candidates=[cand('B', 1, 1, group='new', risk=50)],
            positions=[{'security_id': 'A', 'risk_bp': 100, 'risk_group': 'old'}], limits=LIMITS)
        self.assertEqual(result['templates'][0]['total_risk_bp'], 150)
        self.assertEqual([a['security_id'] for a in result['templates'][0]['allocations']], ['B'])

if __name__ == '__main__':
    unittest.main()


class ConsultRequiredTests(unittest.TestCase):
    """`consult_required` 只该在**确有可争的容量**时为真。

    §6.3：没有容量冲突时默认不调用，避免制造无意义决策。两类拒绝**不构成争用**：
    「钱不够」与「已持有」—— 后者尤其要紧：所有模板都会同样拒绝一个已持有的标的，
    换序也改变不了什么，为此发起一次付费评审纯属浪费（这是 2026-09-19 加持仓计入时
    带出的副作用）。
    """

    LIMITS = {'max_positions': 2, 'max_total_risk_bp': 200, 'max_name_risk_bp': 100,
              'cash_available_micro': 10 ** 12}

    def consult(self, positions):
        return build_portfolio_templates(
            candidates=[cand('US.A', 1, 1)], positions=positions,
            limits=self.LIMITS)['consult_required']

    def test_already_held_alone_does_not_trigger_a_review(self):
        self.assertFalse(self.consult([{'security_id': 'US.A', 'risk_bp': 100,
                                        'risk_group': 'semis'}]))

    def test_insufficient_cash_alone_does_not_trigger_a_review(self):
        broke = dict(self.LIMITS, cash_available_micro=0)
        self.assertFalse(build_portfolio_templates(
            candidates=[cand('US.A', 1, 1)], positions=[], limits=broke)['consult_required'])

    def test_a_used_up_position_slot_still_triggers(self):
        """真的没位置了 = 确有候选被挡下 ⇒ 仍要评审（不能把信号一并关掉）。"""
        self.assertTrue(self.consult([{'security_id': 'US.X', 'risk_bp': 0, 'risk_group': 'c'},
                                      {'security_id': 'US.Y', 'risk_bp': 0, 'risk_group': 'c'}]))

    def test_a_used_up_risk_budget_still_triggers(self):
        self.assertTrue(self.consult([{'security_id': 'US.X', 'risk_bp': 150,
                                       'risk_group': 'china'}]))
