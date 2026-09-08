"""H2 离散计划模板 + 仓位档位 + I3/J3 权限状态机回归。"""
import unittest

from mutifactor.llm.plan_templates import (
    ALLOWED_POSITION_SCALES, PLAN_TEMPLATES, apply_position_scale,
    build_plan_templates, resolve_entry_intent, validate_scale,
)
from mutifactor.llm.trade_review import (
    ALLOWED_POSITION_SCALES as TR_ALLOWED,
    PLAN_TEMPLATES as TR_TEMPLATES,
    validate_review,
)
from scripts.live_trading.llm_permission import (
    PermissionLevel, allowed_scale, eligibility_met, level_for, permits, promote_candidate,
)


class PlanTemplatesContracts(unittest.TestCase):
    def test_templates_and_scales_are_predefined(self):
        self.assertEqual(tuple(PLAN_TEMPLATES), ('standard', 'wait_for_confirmation'))
        self.assertEqual(tuple(ALLOWED_POSITION_SCALES), (0.0, 0.5, 1.0))
        # 与 trade_review schema 常量一致（不漂移）
        self.assertEqual(tuple(TR_TEMPLATES), PLAN_TEMPLATES)
        self.assertEqual(tuple(TR_ALLOWED), ALLOWED_POSITION_SCALES)

    def test_build_plan_templates(self):
        t = build_plan_templates({'initial_stop': 95})
        self.assertIn('standard', t)
        self.assertEqual(t['standard']['allowed_position_scale'], [0.0, 0.5, 1.0])

    def test_validate_scale(self):
        self.assertTrue(validate_scale(0.0))
        self.assertTrue(validate_scale(0.5))
        self.assertTrue(validate_scale(1.0))
        self.assertFalse(validate_scale(1.5))   # 加档不允许
        self.assertFalse(validate_scale(0.3))   # 非预定义档位
        self.assertFalse(validate_scale('x'))

    def test_apply_position_scale(self):
        self.assertEqual(apply_position_scale(100, 1.0), 100)
        self.assertEqual(apply_position_scale(100, 0.5), 50)
        self.assertEqual(apply_position_scale(101, 0.5), 50)  # floor
        self.assertEqual(apply_position_scale(100, 0.0), 0)
        with self.assertRaises(ValueError):
            apply_position_scale(100, 1.5)
        with self.assertRaises(ValueError):
            apply_position_scale(100, 0.3)

    def test_resolve_entry_intent(self):
        t, s = resolve_entry_intent({'plan_template': 'standard', 'position_scale': 0.5})
        self.assertEqual((t, s), ('standard', 0.5))
        t2, s2 = resolve_entry_intent({})
        self.assertEqual((t2, s2), (None, None))
        with self.assertRaises(ValueError):
            resolve_entry_intent({'plan_template': 'bogus'})
        with self.assertRaises(ValueError):
            resolve_entry_intent({'position_scale': 9.9})


class PermissionContracts(unittest.TestCase):
    def test_level_for_default_shadow(self):
        self.assertEqual(level_for('position_scale', {}), 'shadow')
        self.assertEqual(level_for('entry_review', {'llm_permissions': {}}), 'shadow')

    def test_level_for_reads_config(self):
        cfg = {'llm_permissions': {'position_scale': 'constrained_action'}}
        self.assertEqual(level_for('position_scale', cfg), 'constrained_action')

    def test_allowed_scale_shadow_does_not_apply(self):
        # 默认 shadow：模型建议 0.5x 也不应用
        scale, applied = allowed_scale('position_scale', 'shadow', 0.5)
        self.assertEqual(scale, 1.0)
        self.assertFalse(applied)

    def test_allowed_scale_constrained_applies_downscale_only(self):
        scale, applied = allowed_scale('position_scale', 'constrained_action', 0.5)
        self.assertEqual(scale, 0.5)
        self.assertTrue(applied)
        # 加档 1.5 永不应用
        scale2, applied2 = allowed_scale('position_scale', 'constrained_action', 1.5)
        self.assertEqual(scale2, 1.0)
        self.assertFalse(applied2)
        # 1.0 = 现有上限，无变化不 applied
        scale3, applied3 = allowed_scale('position_scale', 'constrained_action', 1.0)
        self.assertFalse(applied3)

    def test_allowed_scale_recommend_not_apply(self):
        scale, applied = allowed_scale('position_scale', 'recommend', 0.5)
        self.assertEqual(scale, 1.0)
        self.assertFalse(applied)

    def test_eligibility_met(self):
        ok, checks = eligibility_met({'independent_samples': 120, 'market_phases': 3,
                                      'data_leak_checked': True},
                                     {'min_samples': 100, 'min_market_phases': 2})
        self.assertTrue(ok)
        ok2, _ = eligibility_met({'independent_samples': 5, 'market_phases': 1,
                                  'data_leak_checked': False},
                                 {'min_samples': 100, 'min_market_phases': 2})
        self.assertFalse(ok2)

    def test_promote_candidate(self):
        cfg = {}
        # 门槛不够 → 不升级
        self.assertFalse(promote_candidate('position_scale', cfg, {'independent_samples': 5}, {}))
        # 满足门槛 → 可升级（判定 true）
        stats = {'independent_samples': 150, 'market_phases': 4, 'data_leak_checked': True}
        self.assertTrue(promote_candidate('position_scale', cfg, stats,
                                          {'min_samples': 100, 'min_market_phases': 2}))
        # disabled 不参与升级
        cfg2 = {'llm_permissions': {'position_scale': 'disabled'}}
        self.assertFalse(promote_candidate('position_scale', cfg2, stats, {}))


class SchemaValidationContracts(unittest.TestCase):
    def _snapshot(self):
        return {'evidence': [{
            'evidence_id': 'ev1', 'summary': '公司财报超预期',
            'source': 'filing', 'published_at': '2026-09-07T00:00:00+00:00',
            'observed_at': '2026-09-08T00:00:00+00:00', 'kind': 'filing',
        }], 'observed_at': '2026-09-08T00:00:00+00:00'}

    def _base(self, **over):
        r = dict(status='complete', recommendation='support_execute', proposed_action='buy',
                 thesis_state='unchanged',
                 facts=[{'text': '公司财报超预期', 'evidence_ids': ['ev1']}],
                 inferences=[], counterevidence=[],
                 missing_information=['缺经营新闻'], plan_change_requested=False,
                 next_review_conditions=[])
        r.update(over)
        return r

    def test_schema_accepts_valid_template_and_scale(self):
        raw = self._base(plan_template='standard', position_scale=0.5)
        out = validate_review(raw, self._snapshot(), 'buy')
        self.assertEqual(out['position_scale'], 0.5)

    def test_schema_rejects_invalid_scale(self):
        raw = self._base(position_scale=1.5)  # 加档
        with self.assertRaises(Exception):
            validate_review(raw, self._snapshot(), 'buy')

    def test_schema_rejects_invalid_template(self):
        raw = self._base(plan_template='bogus')
        with self.assertRaises(Exception):
            validate_review(raw, self._snapshot(), 'buy')

    def test_legacy_review_still_valid(self):
        # 不带新字段的旧 review 仍通过
        out = validate_review(self._base(), self._snapshot(), 'buy')
        self.assertNotIn('position_scale', out)


if __name__ == '__main__':
    unittest.main()
