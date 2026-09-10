"""action / risk 校验器测试（§13 / §8.2 / §9.5）。"""
import unittest

from mutifactor.llm.validators.action import apply_permission
from mutifactor.llm.validators.risk import (
    validate_entry_templates, validate_position_templates, validate_price_drift,
)

PLAN = {'plan_id': 'plan1', 'stock_code': 'US.AAPL',
        'risk': {'initial_stop': 90.0}, 'entry_constraints': {'price': 100.0}}


class ApplyPermissionTests(unittest.TestCase):
    def test_selection_constrained_action_applies_llm_ranking(self):
        r = apply_permission('selection', 'llm_ranking', 'constrained_action',
                             'selection_rank')
        self.assertEqual(r['effective_action'], 'llm_ranking')

    def test_shadow_baseline(self):
        r = apply_permission('entry', 'execute_now', 'shadow', 'entry_review')
        self.assertEqual(r['effective_action'], 'rule_baseline')
        self.assertEqual(r['shadow_difference'], 'llm_would_execute_now')

    def test_recommend_requires_confirmation(self):
        r = apply_permission('entry', 'execute_now', 'recommend', 'entry_review')
        self.assertEqual(r['effective_action'], 'execute_now')
        self.assertTrue(r['requires_confirmation'])

    def test_constrained_action_within_scope(self):
        r = apply_permission('entry', 'execute_now', 'constrained_action', 'entry_review')
        self.assertEqual(r['effective_action'], 'execute_now')
        self.assertFalse(r['requires_confirmation'])

    def test_constrained_action_out_of_scope(self):
        # plan_template 不覆盖 defer
        r = apply_permission('entry', 'defer', 'constrained_action', 'plan_template')
        self.assertEqual(r['effective_action'], 'rule_baseline')

    def test_auto_exit_thesis_only_invalidated(self):
        r = apply_permission('position', 'exit', 'constrained_action', 'auto_exit_thesis',
                             validated={'thesis_state': 'INVALIDATED'})
        self.assertEqual(r['effective_action'], 'exit')
        r2 = apply_permission('position', 'exit', 'constrained_action', 'auto_exit_thesis',
                              validated={'thesis_state': 'WEAKENING'})
        self.assertEqual(r2['effective_action'], 'hold')

    def test_disabled_treated_as_shadow(self):
        r = apply_permission('position', 'reduce', 'disabled', 'thesis_reduce')
        self.assertEqual(r['effective_action'], 'hold')


class RiskValidationTests(unittest.TestCase):
    def test_entry_templates_ok(self):
        from mutifactor.llm.contracts.entry_v2 import build_entry_templates
        ts = build_entry_templates(plan=PLAN, standard_quantity=100, entry_price=100.0,
                                   initial_stop=90.0, expires_at='2999-01-01T00:00:00+00:00')
        self.assertEqual(validate_entry_templates(ts, PLAN), [])

    def test_entry_template_stop_deviation(self):
        ts = [{'template_id': 't', 'kind': 'standard', 'quantity': 100,
               'entry_price_limit': 100.0, 'initial_stop': 80.0, 'planned_r': 2000.0,
               'expires_at': 'x'}]
        errs = validate_entry_templates(ts, PLAN)
        self.assertTrue(any('止损' in e for e in errs))

    def test_wait_template_must_be_zero(self):
        ts = [{'template_id': 't', 'kind': 'wait_for_confirmation', 'quantity': 10,
               'entry_price_limit': 100.0, 'initial_stop': 90.0, 'planned_r': 0.0,
               'expires_at': 'x'}]
        errs = validate_entry_templates(ts, PLAN)
        self.assertTrue(any('必须为 0' in e for e in errs))

    def test_position_templates_tighten_monotonic(self):
        ts = [{'template_id': 't', 'action': 'tighten_protection', 'quantity': 0.0,
               'new_protection_price': 85.0, 'constraints': {'direction': 'long'}}]
        errs = validate_position_templates(ts, {'remaining_qty': 100.0, 'direction': 'long'},
                                           active_stop=90.0)
        self.assertTrue(any('不得低于当前保护线' in e for e in errs))

    def test_position_templates_reduce_tier(self):
        ts = [{'template_id': 't', 'action': 'reduce', 'quantity': 40.0,
               'constraints': {'tier': 0.4}}]
        errs = validate_position_templates(ts, {'remaining_qty': 100.0, 'direction': 'long'})
        self.assertTrue(any('档位非法' in e for e in errs))

    def test_price_drift(self):
        self.assertTrue(validate_price_drift(104.0, PLAN, max_drift_pct=0.03))
        self.assertFalse(validate_price_drift(101.0, PLAN, max_drift_pct=0.03))


if __name__ == '__main__':
    unittest.main()
