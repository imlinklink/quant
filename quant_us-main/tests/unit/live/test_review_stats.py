"""Review 统计构造器（§6.5 的输入侧）：只统计已结算、不静默丢弃、必须披露不能说明什么。"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.live_trading.decision_ledger.event_store import EventStore, stable_id
from scripts.live_trading.decision_ledger.review_stats import (build_review_packet, _caveats,
                                                              horizon_availability,
                                                              pick_horizon, role_stats,
                                                              sample_groups)
from scripts.live_trading.position_registry import PositionRegistry

HORIZON = '1d'


class StatsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = PositionRegistry(Path(self.tmp.name) / 'x.sqlite3', 'DRY-RUN')
        # 投影表由事务内的 migrate 建立，构造 EventStore 本身不建表
        with EventStore(self.registry, ).transaction():
            pass
        self.con = sqlite3.connect(str(self.registry.path))

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def add_run(self, decision_id, role='selection', snapshot_id='snap1'):
        self.con.execute(
            'INSERT OR REPLACE INTO llm_decision_runs '
            '(account_scope, decision_id, role, subject_type, subject_id, as_of, status, '
            ' input_snapshot_id, prompt_version, output_schema_version, feature_version, '
            ' rule_version, permission_version, provider, model_id, created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('DRY-RUN', decision_id, role, 'research_batch', 'batch', '2026-09-15',
             'validated', snapshot_id, 'v1', 'v1', 'v1', 'v1', 'v1', 'configured',
             'deepseek-chat', '2026-09-15'))

    def add_effective(self, decision_id, action, level='shadow', model_action='llm_ranking'):
        event = {'event_id': stable_id('event', 'DRY-RUN', 'decision_effective_action',
                                       decision_id),
                 'event_type': 'decision_effective_action', 'account_scope': 'DRY-RUN',
                 'observed_at': '2026-09-15T00:00:00+00:00',
                 'payload_hash': 'h', 'payload': {'model_action': model_action,
                                                  'effective_action': action,
                                                  'permission_level': level},
                 'decision_id': decision_id}
        self.con.execute('INSERT OR REPLACE INTO decision_events VALUES (?,?,?,?,?,?)',
                         (event['event_id'], 'DRY-RUN', event['event_type'],
                          event['observed_at'], 'h', json.dumps(event)))

    def add_snapshot(self, snapshot_id, events=None, risk_group='semis'):
        body = json.dumps({'packet': {'events': events or [],
                                      'identity': {'risk_group': risk_group}}})
        self.con.execute('INSERT OR REPLACE INTO decision_snapshots VALUES (?,?,?,?,?,?)',
                         ('DRY-RUN', 'selection_input', snapshot_id, 1, 'h', body))

    def add_outcome(self, decision_id, subject, excess, *, quality='good',
                    horizon=HORIZON, mae=-0.02):
        self.con.execute(
            'INSERT OR REPLACE INTO decision_outcomes_v2 VALUES '
            '(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('DRY-RUN', decision_id, horizon, subject, '2026-09-16T00:00:00+00:00',
             0.0, 0.0, excess, None, mae, None, quality, '{}'))

    def commit(self):
        self.con.commit()
        return self.registry


class HorizonTests(StatsBase):
    def test_availability_counts_joined_and_unjoined(self):
        self.add_run('decision_a')
        self.add_outcome('decision_a', 'US.A', 0.01)
        self.add_outcome('research_batch_x', 'US.B', 0.01)      # 关联不上
        # 主键是 (scope, decision_id, horizon, subject_key)：必须换 subject，否则会被替换掉
        self.add_outcome('decision_a', 'US.C', None, quality='pending_future_bars')
        self.commit()
        rows = horizon_availability(self.registry)
        self.assertEqual(rows, [{'horizon': HORIZON, 'joined': 1, 'unjoined': 1}])

    def test_pick_horizon_prefers_the_most_joinable(self):
        self.assertEqual(pick_horizon([{'horizon': '1d', 'joined': 3, 'unjoined': 0},
                                       {'horizon': '3d', 'joined': 9, 'unjoined': 0}]),
                         '3d')

    def test_pick_horizon_is_none_when_nothing_joins(self):
        self.assertIsNone(pick_horizon([{'horizon': '1d', 'joined': 0, 'unjoined': 5}]))

    def test_packet_records_every_horizons_availability(self):
        self.add_run('decision_a')
        self.add_outcome('decision_a', 'US.A', 0.01)
        self.add_outcome('decision_a', 'US.A', 0.02, horizon='5d')
        self.commit()
        packet = build_review_packet(self.registry, protocol_version='v1',
                                     account_scope='DRY-RUN', subject_id='r1',
                                     as_of='2026-09-19T00:00:00+00:00')
        window = packet['window']
        self.assertEqual(window['horizon'], HORIZON)
        self.assertEqual({a['horizon'] for a in window['horizon_availability']},
                         {'1d', '5d'})


class SamplingTests(StatsBase):
    def test_pending_rows_are_not_counted_as_zero_return(self):
        """把未成熟读成 0 收益是这类统计最常见的错误。"""
        self.add_run('decision_a')
        self.add_outcome('decision_a', 'US.A', 0.05)
        # 换 subject：主键含 subject_key，同 key 会被替换而不是新增
        self.add_outcome('decision_a', 'US.PENDING', None, quality='pending_future_bars')
        self.commit()
        groups = sample_groups(self.registry, horizon=HORIZON)
        self.assertEqual(groups['settled_rows'], 1)
        self.assertEqual(groups['unjoined_outcomes'], 0)

    def test_unjoined_rows_are_reported_not_dropped(self):
        self.add_run('decision_a')
        self.add_outcome('decision_a', 'US.A', 0.05)
        self.add_outcome('research_batch_x', 'US.B', 0.05)
        self.commit()
        groups = sample_groups(self.registry, horizon=HORIZON)
        self.assertEqual(groups['settled_rows'], 2)
        self.assertEqual(groups['unjoined_outcomes'], 1)

    def test_groups_split_by_action_and_carry_outcome_summary(self):
        self.add_run('decision_a')
        self.add_run('decision_b')
        self.add_effective('decision_a', 'rule_ranking')
        self.add_effective('decision_b', 'llm_ranking')
        self.add_snapshot('snap1')
        for i, (decision, excess) in enumerate((('decision_a', 0.01),
                                                ('decision_a', 0.03),
                                                ('decision_b', -0.02))):
            self.add_outcome(decision, f'US.{i}', excess)
        self.commit()
        groups = {g['action']: g for g in
                  sample_groups(self.registry, horizon=HORIZON)['groups']}
        self.assertEqual(groups['rule_ranking']['n'], 2)
        self.assertAlmostEqual(groups['rule_ranking']['mean_excess_pct'], 0.02)
        self.assertEqual(groups['llm_ranking']['n'], 1)

    def test_role_stats_reports_single_contributor_dependence(self):
        """§12 要的"不依赖单一证券"：全部超额来自一只标的时占比应为 1。"""
        self.add_run('decision_a')
        self.add_run('decision_b')
        self.add_outcome('decision_a', 'US.A', 0.02)
        self.add_outcome('decision_b', 'US.A', 0.03)
        self.commit()
        stats = role_stats(self.registry, horizon=HORIZON)['roles']['selection']
        self.assertEqual(stats['n'], 2)
        self.assertEqual(stats['distinct_subjects'], 1)
        self.assertAlmostEqual(stats['top_contributor_share'], 1.0)

    def test_top_contributor_share_uses_absolute_contributions(self):
        """用绝对值之和做分母：正负相消会让占比失真。"""
        self.add_run('decision_a')
        self.add_run('decision_b')
        self.add_outcome('decision_a', 'US.A', 0.10)
        self.add_outcome('decision_b', 'US.B', -0.02)
        self.commit()
        stats = role_stats(self.registry, horizon=HORIZON)['roles']['selection']
        self.assertAlmostEqual(stats['top_contributor_share'], 0.10 / 0.12)


class CaveatTests(StatsBase):
    def test_always_discloses_that_these_are_not_l_minus_r(self):
        """最容易被误读的一条：研究篮子相对基准 ≠ L/R 影子路径之差。"""
        self.add_run('decision_a')
        self.add_outcome('decision_a', 'US.A', 0.01)
        self.commit()
        packet = build_review_packet(self.registry, protocol_version='v1',
                                     account_scope='DRY-RUN', subject_id='r1',
                                     as_of='2026-09-19T00:00:00+00:00')
        text = ' '.join(packet['window']['caveats'])
        self.assertIn('L/R 影子路径之差', text)

    def test_flags_a_degenerate_evidence_source_stratum(self):
        caveats = _caveats([{'evidence_source': 'NONE', 'action': 'x', 'n': 1}], 0)
        self.assertTrue(any('不携带信息' in c for c in caveats), caveats)

    def test_flags_failed_decisions_separately_from_neutral_actions(self):
        caveats = _caveats([{'evidence_source': 'MARKET', 'action': '', 'n': 7}], 0)
        self.assertTrue(any('校验失败' in c for c in caveats), caveats)

    def test_does_not_claim_a_degenerate_stratum_when_sources_vary(self):
        caveats = _caveats([{'evidence_source': 'MARKET', 'action': 'x', 'n': 1},
                            {'evidence_source': 'futu', 'action': 'x', 'n': 1}], 0)
        self.assertFalse(any('不携带信息' in c for c in caveats), caveats)

    def test_market_state_is_declared_unavailable(self):
        self.add_run('decision_a')
        self.add_outcome('decision_a', 'US.A', 0.01)
        self.commit()
        packet = build_review_packet(self.registry, protocol_version='v1',
                                     account_scope='DRY-RUN', subject_id='r1',
                                     as_of='2026-09-19T00:00:00+00:00')
        self.assertIn('market_state', packet['window']['unavailable_dimensions'])


class PacketContractTests(StatsBase):
    def test_packet_is_accepted_by_the_review_validator(self):
        """组出来的包必须能被 Review 的校验器接受 —— 否则角色跑不起来。"""
        from mutifactor.llm.contracts.review_v1 import validate_review_v1
        self.add_run('decision_a')
        self.add_effective('decision_a', 'rule_ranking')
        self.add_snapshot('snap1')
        self.add_outcome('decision_a', 'US.A', 0.01)
        self.commit()
        packet = build_review_packet(self.registry, protocol_version='v1',
                                     account_scope='DRY-RUN', subject_id='r1',
                                     as_of='2026-09-19T00:00:00+00:00')
        output = {'schema_version': 'review-v1', 'packet_id': packet['packet_id'],
                  'status': 'complete', 'failure_patterns': [], 'proposed_change': None,
                  'expected_improvement': None, 'possible_regression': [],
                  'validation_plan': None, 'reason_codes': [],
                  'missing_information': ['样本量不足以提出改动']}
        self.assertEqual(validate_review_v1(output, packet), [])
        self.assertEqual(packet['context']['role'], 'review')
        self.assertEqual(packet['context']['subject_type'], 'evaluation_window')

    def test_packet_id_is_content_addressed(self):
        self.add_run('decision_a')
        self.add_outcome('decision_a', 'US.A', 0.01)
        self.commit()
        first = build_review_packet(self.registry, protocol_version='v1',
                                    account_scope='DRY-RUN', subject_id='r1',
                                    as_of='2026-09-19T00:00:00+00:00')
        second = build_review_packet(self.registry, protocol_version='v2',
                                     account_scope='DRY-RUN', subject_id='r1',
                                     as_of='2026-09-19T00:00:00+00:00')
        self.assertNotEqual(first['packet_id'], second['packet_id'])

    def test_packet_carries_the_allowed_direction_per_variable(self):
        """每个变量的**允许方向**必须进包。

        校验器强制方向（`single_position_risk_bp` 只允许 `decrease`），而系统提示原先只说
        "取自白名单" —— 模型提一个方向非法的合理改动（"单笔风险太低，调高"）会让**整条决策
        失败**。与 entry/position 的原因码闭集是同一形态：**校验器强制的，提示词没说**。
        派生自同一常量，所以这里同时钉死"不另抄一份"。
        """
        from mutifactor.llm.contracts.review_v1 import CHANGEABLE_VARIABLES
        packet = build_review_packet(self.registry, protocol_version='v1',
                                     account_scope='DRY-RUN', subject_id='r-dir',
                                     as_of='2026-09-19T00:00:00+00:00')
        directions = packet['changeable_variable_directions']
        self.assertEqual(set(directions), set(CHANGEABLE_VARIABLES))
        for variable, allowed in CHANGEABLE_VARIABLES.items():
            self.assertEqual(list(directions[variable]), list(allowed))


if __name__ == '__main__':
    unittest.main()
