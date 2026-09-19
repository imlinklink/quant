"""§8 权限等级配置校验 + §12 晋级资格判定。

**判定必须是只读的**：满足门槛只表示"具备晋级资格"，不得切换等级或改变执行路径。
另有两条一致性由测试钉死：角色→权限映射、角色→动作基线 —— 它们与执行侧的两处定义
一旦漂移，"某角色的等级"或"是否越权"就会按另一套集合算，且不会报错。
"""
import unittest
from pathlib import Path

import yaml

from mutifactor.llm.validators.action import EFFECTIVE_BASELINE, SPECIFIC_ACTION_PERMISSION, UMBRELLA
from scripts.live_trading.decision_ledger.promotion_review import ROLE_BASELINE
from scripts.live_trading.llm_permission import (DEFAULT_PROMOTION_THRESHOLDS,
                                                 OPERATOR_ATTESTED, PERMISSIONS,
                                                 ROLE_PERMISSIONS, level_for,
                                                 promotion_report, promotion_verdict,
                                                 validate_permissions)

CONFIG_PATH = Path(__file__).resolve().parents[3] / 'config.yaml'


def config(**overrides):
    section = {'_default': 'shadow'}
    section.update({p: 'shadow' for p in PERMISSIONS})
    section.update(overrides)
    return {'llm_permissions': section}


ALL_TIER1 = {'output_validity': 1.0, 'hard_risk_overreach': 0, 'unreplayable': 0,
             'model_effective_distinguishable': True}
ALL_TIER2 = {'independent_mature_samples': 40, 'excess_return_after_cost': 0.03,
             'mdd_delta': 0.0, 'top_contributor_share': 0.2,
             'out_of_sample_window_locked': True, 'human_approval': True}


class ConsistencyTests(unittest.TestCase):
    """两处定义不得漂移。"""

    def test_role_permissions_match_the_execution_side(self):
        self.assertEqual(ROLE_PERMISSIONS['selection'], (UMBRELLA['selection'],))
        self.assertIn(UMBRELLA['entry'], ROLE_PERMISSIONS['entry'])
        position = {UMBRELLA['position']} | {p for (role, _a), p in
                                             SPECIFIC_ACTION_PERMISSION.items()
                                             if role == 'position'}
        self.assertEqual(set(ROLE_PERMISSIONS['position']), position)

    def test_role_baseline_matches_the_execution_side(self):
        for role, baseline in ROLE_BASELINE.items():
            self.assertEqual(baseline, EFFECTIVE_BASELINE[role])

    def test_every_declared_permission_belongs_to_some_role(self):
        assigned = {p for perms in ROLE_PERMISSIONS.values() for p in perms}
        self.assertEqual(assigned, set(PERMISSIONS),
                         '有权限没有归属角色，它的等级永远不会被晋级判定看到')


class ValidatePermissionsTests(unittest.TestCase):
    def test_real_config_passes(self):
        cfg = yaml.safe_load(CONFIG_PATH.read_text())
        self.assertEqual(validate_permissions(cfg), [])

    def test_missing_section_is_an_error(self):
        errors = validate_permissions({})
        self.assertTrue(any('llm_permissions 缺失' in e for e in errors), errors)

    def test_typo_in_permission_name_is_an_error(self):
        """拼错的权限名会被 `dict.get` 静默忽略 —— 必须报出来，而不是无声失效。"""
        errors = validate_permissions(config(selection_ranks='shadow'))
        self.assertTrue(any('未知键' in e and 'selection_ranks' in e for e in errors), errors)

    def test_typo_in_level_is_an_error(self):
        errors = validate_permissions(config(selection_rank='shdow'))
        self.assertTrue(any('等级非法' in e for e in errors), errors)

    def test_implicit_default_is_an_error(self):
        cfg = config()
        del cfg['llm_permissions']['_default']
        self.assertTrue(any('_default 缺失' in e for e in validate_permissions(cfg)))

    def test_unknown_threshold_key_is_an_error(self):
        errors = validate_permissions(config(promotion={'selection': {'min_sample': 30}}))
        self.assertTrue(any('未知门槛' in e for e in errors), errors)

    def test_unknown_promotion_role_is_an_error(self):
        errors = validate_permissions(config(promotion={'portfolio_v2': {}}))
        self.assertTrue(any('未知键' in e for e in errors), errors)

    def test_all_five_design_roles_are_covered(self):
        """§6 的五个角色都必须能被晋级判定看到；漏一个就等于它永远不参与评级。"""
        self.assertEqual(set(ROLE_PERMISSIONS),
                         {'selection', 'entry', 'position', 'portfolio', 'review'})


class VerdictTests(unittest.TestCase):
    def test_all_checks_pass_means_eligible_but_not_switched(self):
        verdict = promotion_verdict('selection', config(), {**ALL_TIER1})
        self.assertTrue(verdict['eligible'])
        self.assertEqual(verdict['target_level'], 'recommend')
        # 资格 ≠ 切换：措辞必须写明不自动切换
        self.assertIn('不自动切换', verdict['note'])

    def test_missing_metric_fails_closed_with_a_reason(self):
        stats = dict(ALL_TIER1)
        del stats['output_validity']
        verdict = promotion_verdict('selection', config(), stats)
        self.assertFalse(verdict['eligible'])
        check = next(c for c in verdict['checks'] if c['name'] == 'output_validity')
        self.assertIsNone(check['actual'])
        self.assertIn('unavailable', check['reason'])

    def test_reason_is_passed_through_when_supplied(self):
        stats = dict(ALL_TIER1)
        stats['output_validity'] = None
        stats['_unavailable'] = {'output_validity': '该角色尚无已出终态的决策'}
        verdict = promotion_verdict('selection', config(), stats)
        check = next(c for c in verdict['checks'] if c['name'] == 'output_validity')
        self.assertIn('该角色尚无已出终态的决策', check['reason'])

    def test_unmet_lists_every_failing_check(self):
        verdict = promotion_verdict('selection', config(),
                                    {'output_validity': 0.5, 'hard_risk_overreach': 1,
                                     'unreplayable': 2,
                                     'model_effective_distinguishable': False})
        self.assertEqual(sorted(verdict['unmet']),
                         ['hard_risk_overreach', 'model_effective_distinguishable',
                          'output_validity', 'unreplayable'])

    def test_disabled_permission_blocks_promotion(self):
        """disabled 是显式关闭，不得被晋级越过。"""
        verdict = promotion_verdict('selection', config(selection_rank='disabled'),
                                    {**ALL_TIER1})
        self.assertFalse(verdict['eligible'])
        self.assertIsNone(verdict['target_level'])
        self.assertIn('关闭', verdict['note'])

    def test_top_level_has_no_higher_tier(self):
        verdict = promotion_verdict('selection', config(selection_rank='constrained_action'),
                                    {**ALL_TIER2})
        self.assertIsNone(verdict['target_level'])
        self.assertFalse(verdict['eligible'])

    def test_second_tier_uses_the_evidence_gates(self):
        verdict = promotion_verdict('selection', config(selection_rank='recommend'),
                                    {**ALL_TIER1, **ALL_TIER2})
        self.assertTrue(verdict['eligible'])
        self.assertEqual(verdict['target_level'], 'constrained_action')
        names = {c['name'] for c in verdict['checks']}
        self.assertIn('independent_mature_samples', names)

    def test_second_tier_fails_on_thin_samples(self):
        thin = {**ALL_TIER1, **ALL_TIER2, 'independent_mature_samples': 5}
        verdict = promotion_verdict('selection', config(selection_rank='recommend'), thin)
        self.assertFalse(verdict['eligible'])
        self.assertIn('independent_mature_samples', verdict['unmet'])

    def test_role_level_threshold_overrides_the_default(self):
        cfg = config(selection_rank='recommend',
                     promotion={'selection': {'min_independent_samples': 5}})
        verdict = promotion_verdict('selection', cfg,
                                    {**ALL_TIER1, **ALL_TIER2,
                                     'independent_mature_samples': 5})
        self.assertTrue(verdict['eligible'])

    def test_verdict_is_read_only(self):
        cfg = config()
        before = {p: level_for(p, cfg) for p in PERMISSIONS}
        promotion_verdict('selection', cfg, {**ALL_TIER1})
        self.assertEqual({p: level_for(p, cfg) for p in PERMISSIONS}, before)

    def test_report_covers_every_role(self):
        report = promotion_report(config(), {})
        self.assertEqual(set(report['roles']), set(ROLE_PERMISSIONS))
        self.assertEqual(set(report['levels']), set(PERMISSIONS))
        for verdict in report['roles'].values():
            self.assertFalse(verdict['eligible'])


class DefaultsTests(unittest.TestCase):
    def test_defaults_are_the_design_values(self):
        """缺配置时用**设计值**而不是"无门槛"。"""
        self.assertEqual(DEFAULT_PROMOTION_THRESHOLDS['min_independent_samples'], 30)
        self.assertEqual(DEFAULT_PROMOTION_THRESHOLDS['min_output_validity'], 0.95)

    def test_operator_attested_items_default_to_required_and_unmet(self):
        """人工确认项无法从账本推出：门槛默认**要求**，而指标缺失即未达标。

        `OPERATOR_ATTESTED` 是**指标名**，`DEFAULT_PROMOTION_THRESHOLDS` 的 `require_*`
        是**门槛名** —— 两个命名空间，测试必须分别断言，否则就是把它们混为一谈。
        """
        self.assertEqual(OPERATOR_ATTESTED,
                         ('out_of_sample_window_locked', 'human_approval'))
        self.assertIs(DEFAULT_PROMOTION_THRESHOLDS['require_human_approval'], True)
        self.assertIs(DEFAULT_PROMOTION_THRESHOLDS['require_out_of_sample_window'], True)
        # 指标缺失 → 未达标（而不是"没声明就算通过"）
        verdict = promotion_verdict('selection', config(selection_rank='recommend'),
                                    {**ALL_TIER1, **ALL_TIER2,
                                     'human_approval': None,
                                     'out_of_sample_window_locked': None})
        self.assertFalse(verdict['eligible'])
        self.assertIn('human_approval', verdict['unmet'])


class IndependentSampleRegressionTests(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.con = sqlite3.connect(':memory:')
        self.addCleanup(self.con.close)
        self.con.execute('CREATE TABLE decision_outcomes_v2(decision_id,subject_key,label_as_of,data_quality)')
        self.con.execute('CREATE TABLE decision_snapshots(id,version,body)')
        self.snapshots = {}

    def add(self, did, *, as_of='2026-09-14T20:00:00Z', cluster='event',
            role='selection', trade_id=None, dates=('2026-09-15', '2026-09-17', '2026-09-21')):
        import json
        packet = {'context': {'role': role, 'as_of': as_of, 'account_scope': 'test'},
                  'stocks': [{'code': 'A', 'primary_event_cluster': cluster}],
                  'trade': {'trade_id': trade_id}}
        self.snapshots[did] = did
        self.con.execute('INSERT INTO decision_snapshots VALUES(?,1,?)', (did, json.dumps(packet)))
        for date in dates:
            self.con.execute('INSERT INTO decision_outcomes_v2 VALUES(?,?,?,?)', (did, 'A', date, 'good'))

    def count(self):
        from scripts.live_trading.decision_ledger.promotion_review import _settled_groups
        return _settled_groups(self.con, self.snapshots)

    def test_horizons_and_repeated_reviews_in_same_week_count_once(self):
        self.add('a')
        self.add('b', as_of='2026-09-18T20:00:00Z')
        self.assertEqual(self.count(), (1, 0))

    def test_distinct_weeks_or_events_count_separately(self):
        self.add('a')
        self.add('b', as_of='2026-09-21T20:00:00Z')
        self.add('c', cluster='another-event')
        self.assertEqual(self.count(), (3, 0))

    def test_missing_cluster_or_timestamp_is_unavailable(self):
        self.add('a', cluster=None)
        self.add('b', as_of='invalid')
        self.assertEqual(self.count(), (None, 2))

    def test_position_counts_the_whole_trade_across_weeks(self):
        self.add('a', role='position', trade_id='trade-1')
        self.add('b', role='position', trade_id='trade-1', as_of='2026-09-21T20:00:00Z')
        self.add('c', role='position', trade_id='trade-2')
        self.assertEqual(self.count(), (2, 0))

if __name__ == '__main__':
    unittest.main()
