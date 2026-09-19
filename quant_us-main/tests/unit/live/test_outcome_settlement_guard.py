"""结算链路的防重算与显式修订（2026-09-19 生产故障后的修复）。

故障形态：`excess_return_pct` 两次运行差 1.28e-13（基准序列重取带来的浮点末位噪声），
而 `outcome_observed` 的事件键固定 ⇒ `insert_event` 判「同 ID 异内容」⇒
`settlement_conflict, retryable: false` ⇒ **日结算永久失败、永不重试**。

修复的分工是刻意的：
  - **防重算**（主）：已结算的期限不再重算 —— 重跑不得改写已记录的结果；
  - **量化**（辅）：压掉浮点末位噪声。但量化**不保证**"相差小于 1e-6 就落入同一档"，
    落在档边界两侧的数仍不同，那时**冲突保护照旧生效** —— 这是要保住的性质，
    所以下面既有"噪声归一"也有"跨档仍冲突"两条。
  - **显式修订**：确需改历史时走 `revise_outcome`，**不复用原事件键**、留因留人。
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.live_trading.decision_ledger.event_store import EventStore
from scripts.live_trading.decision_ledger.outcome_jobs import (OutcomeSettlement,
                                                              divergences, quantize,
                                                              repair_projection)
from scripts.live_trading.position_registry import PositionRegistry

DID, HORIZON, SUBJECT = 'decision_x', '1d', 'US.A'
# 生产上真实出现的一对值：相对差约 2.9e-11
STORED = 0.004336978873551606
RERUN = 0.004336978873423566


class SettlementBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = PositionRegistry(Path(self.tmp.name) / 'x.sqlite3', 'DRY-RUN')
        with EventStore(self.registry).transaction():
            pass
        self.settlement = OutcomeSettlement(self.registry)

    def tearDown(self):
        self.tmp.cleanup()

    def events(self, event_type='outcome_observed'):
        return [e for e in EventStore(self.registry).events()
                if e.get('event_type') == event_type]

    def projection(self):
        con = sqlite3.connect(str(self.registry.path))
        try:
            return con.execute(
                'SELECT return_pct, excess_return_pct FROM decision_outcomes_v2 '
                'WHERE decision_id=? AND horizon=? AND subject_key=?',
                (DID, HORIZON, SUBJECT)).fetchone()
        finally:
            con.close()

    def outcome(self, excess, ret=0.01):
        return {'horizon': HORIZON, 'return_pct': ret, 'excess_return_pct': excess,
                'data_quality': 'good'}

    def break_projection(self, excess=0.99):
        """模拟 `write_outcome` 在"投影已更新、事件未写"之间崩掉留下的残迹。"""
        con = sqlite3.connect(str(self.registry.path))
        con.execute('UPDATE decision_outcomes_v2 SET excess_return_pct=? '
                    'WHERE decision_id=? AND horizon=? AND subject_key=?',
                    (excess, DID, HORIZON, SUBJECT))
        con.commit()
        con.close()


class RerunIsANoOpTests(SettlementBase):
    def test_rerun_with_the_same_data_writes_no_new_event(self):
        self.assertTrue(self.settlement.write_outcome(DID, self.outcome(STORED),
                                                      subject_key=SUBJECT))
        self.assertFalse(self.settlement.write_outcome(DID, self.outcome(STORED),
                                                       subject_key=SUBJECT))
        self.assertEqual(len(self.events()), 1)

    def test_rerun_with_float_noise_writes_no_new_event(self):
        """生产故障的原形态：同一批数据、末位差 1e-13 —— 不得再触发冲突。"""
        self.settlement.write_outcome(DID, self.outcome(STORED), subject_key=SUBJECT)
        self.assertFalse(self.settlement.write_outcome(DID, self.outcome(RERUN),
                                                       subject_key=SUBJECT))
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.projection(), (0.01, quantize(STORED)))

    def test_materially_changed_rerun_is_also_skipped_not_silently_recorded(self):
        """已结算后数据真实变化：普通重跑**仍然跳过**，不自动记录新结果。"""
        self.settlement.write_outcome(DID, self.outcome(0.05), subject_key=SUBJECT)
        self.assertFalse(self.settlement.write_outcome(DID, self.outcome(-0.9),
                                                       subject_key=SUBJECT))
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]['payload']['excess_return_pct'], quantize(0.05))
        self.assertEqual(self.projection(), (0.01, 0.05))


class QuantizationTests(unittest.TestCase):
    def test_float_noise_is_normalized(self):
        self.assertEqual(quantize(STORED), quantize(RERUN))

    def test_quantization_does_not_merge_values_across_a_bucket_boundary(self):
        """`1e-6` 是量化**精度**，不是"相差小于 1e-6 就算同一个值"。

        落在档边界两侧的两个数仍然不同 —— 所以量化只能当辅助，防重算必须靠跳过规则。
        """
        self.assertNotEqual(quantize(4.9999e-7), quantize(5.0001e-7))

    def test_conflict_protection_survives_quantization(self):
        """量化后仍不同的载荷，继续触发冲突保护（不能因为量化就把冲突吞掉）。"""
        tmp = tempfile.TemporaryDirectory()
        try:
            registry = PositionRegistry(Path(tmp.name) / 'x.sqlite3', 'DRY-RUN')
            events = EventStore(registry)
            with events.transaction():
                pass
            events.record('outcome_observed', [DID, HORIZON, SUBJECT],
                          {'excess_return_pct': quantize(4.9999e-7)})
            with self.assertRaises(ValueError) as ctx:
                events.record('outcome_observed', [DID, HORIZON, SUBJECT],
                              {'excess_return_pct': quantize(5.0001e-7)})
            self.assertIn('冲突', str(ctx.exception))
        finally:
            tmp.cleanup()


class ExplicitRevisionTests(SettlementBase):
    def test_revision_keeps_the_original_and_uses_a_new_key(self):
        self.settlement.write_outcome(DID, self.outcome(0.05), subject_key=SUBJECT)
        original_id = self.events()[0]['event_id']
        self.settlement.revise_outcome(DID, self.outcome(0.07), subject_key=SUBJECT,
                                       reason='数据源修正：复权因子错误', operator='ops')
        rows = self.events()
        self.assertEqual(len(rows), 2, '修订必须新增事件，不能覆盖原键')
        ids = {r['event_id'] for r in rows}
        self.assertIn(original_id, ids)
        self.assertEqual(len(ids), 2)
        newest = [r for r in rows if r['event_id'] != original_id][0]
        self.assertEqual(newest['payload']['excess_return_pct'], 0.07)
        self.assertEqual(newest['payload']['reason'], '数据源修正：复权因子错误')
        self.assertEqual(newest['payload']['operator'], 'ops')
        # 原值原样保留在新事件里，一次查询即可对照
        self.assertEqual(newest['payload']['previous']['excess_return_pct'], 0.05)
        # 投影更新为修订后的值；账本里原始事件仍在
        self.assertEqual(self.projection(), (0.01, 0.07))

    def test_revision_requires_reason_and_operator(self):
        self.settlement.write_outcome(DID, self.outcome(0.05), subject_key=SUBJECT)
        for reason, operator in (('', 'ops'), ('因为', '')):
            with self.subTest(reason=reason, operator=operator):
                with self.assertRaises(ValueError) as ctx:
                    self.settlement.revise_outcome(DID, self.outcome(0.07),
                                                   subject_key=SUBJECT,
                                                   reason=reason, operator=operator)
                self.assertIn('REVISION_REQUIRES_REASON_AND_OPERATOR', str(ctx.exception))

    def test_cannot_revise_something_never_settled(self):
        with self.assertRaises(ValueError) as ctx:
            self.settlement.revise_outcome(DID, self.outcome(0.07), subject_key=SUBJECT,
                                           reason='r', operator='ops')
        self.assertIn('NOT_SETTLED', str(ctx.exception))

    def test_a_second_revision_stacks_without_touching_the_first(self):
        self.settlement.write_outcome(DID, self.outcome(0.05), subject_key=SUBJECT)
        self.settlement.revise_outcome(DID, self.outcome(0.07), subject_key=SUBJECT,
                                       reason='r1', operator='ops')
        self.settlement.revise_outcome(DID, self.outcome(0.09), subject_key=SUBJECT,
                                       reason='r2', operator='ops')
        self.assertEqual(len(self.events()), 3)
        self.assertEqual(self.projection(), (0.01, 0.09))


class DivergenceTests(SettlementBase):
    def test_divergence_is_reported(self):
        # 与生产一致的配对：已结算 STORED，投影被写成噪声值 RERUN（差 1.28e-13）
        self.settlement.write_outcome(DID, self.outcome(STORED), subject_key=SUBJECT)
        self.break_projection(RERUN)
        found = divergences(self.registry)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['kind'], 'VALUE_DIVERGED')
        self.assertLess(found[0]['delta'], 1e-6)

    def test_repair_restores_the_event_value_and_keeps_the_failed_record(self):
        self.settlement.write_outcome(DID, self.outcome(STORED), subject_key=SUBJECT)
        original = self.events()[0]['event_id']
        self.break_projection(RERUN)
        result = repair_projection(self.registry, operator='ops',
                                   reason='事件写失败留下的投影残迹')
        self.assertEqual(result['repaired'], 1)
        self.assertEqual(self.projection(), (0.01, quantize(STORED)))
        # outcome_observed 一字未动；失败记录以独立事件保留
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events()[0]['event_id'], original)
        repaired = self.events('outcome_projection_repaired')
        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0]['payload']['projection_before']
                         ['excess_return_pct'], RERUN)

    def test_repair_refuses_a_large_divergence(self):
        """差异大就不是"写脏了"，另有故事 —— 必须走显式修订，不能借修复之名改账本。"""
        self.settlement.write_outcome(DID, self.outcome(0.05), subject_key=SUBJECT)
        self.break_projection(0.99)
        result = repair_projection(self.registry, operator='ops', reason='r')
        self.assertEqual(result['repaired'], 0)
        self.assertEqual(len(result['refused']), 1)
        self.assertIn('revise_outcome', result['refused'][0]['why'])

    def test_repair_requires_reason_and_operator(self):
        with self.assertRaises(ValueError):
            repair_projection(self.registry, operator='', reason='r')

    def test_missing_event_is_not_treated_as_a_dirty_projection(self):
        """投影 good 但事件缺失 = 结算没写完，应留给正常结算补齐，不在修复里动手。"""
        con = sqlite3.connect(str(self.registry.path))
        con.execute('INSERT OR REPLACE INTO decision_outcomes_v2 VALUES '
                    '(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('DRY-RUN', DID, HORIZON, SUBJECT, '2026-09-19', 0.01, None, 0.02,
                     None, None, None, 'good', '{}'))
        con.commit()
        con.close()
        found = divergences(self.registry)
        self.assertEqual(found[0]['kind'], 'EVENT_MISSING')
        result = repair_projection(self.registry, operator='ops', reason='r')
        self.assertEqual(result['repaired'], 0)
        # 事件缺失 ⇒ 正常结算应能补写
        self.assertTrue(self.settlement.write_outcome(DID, self.outcome(0.02),
                                                      subject_key=SUBJECT))
        self.assertEqual(len(self.events()), 1)


class ReviewRegressionTests(SettlementBase):
    def test_revision_preserves_fields_and_records_full_history(self):
        outcome = dict(self.outcome(.05), benchmark_return_pct=.02, mfe_pct=.1,
                       mae_pct=-.01, realized_r=2, body={'counterfactual_id': 'cf'})
        self.settlement.write_outcome(DID, outcome, subject_key=SUBJECT)
        self.settlement.revise_outcome(DID, {'horizon': HORIZON, 'return_pct': .08},
                                       subject_key=SUBJECT, reason='correction', operator='ops')
        with self.settlement.events.transaction() as con:
            row = con.execute('SELECT return_pct, benchmark_return_pct, mfe_pct, '
                              'mae_pct, realized_r, body FROM decision_outcomes_v2').fetchone()
        self.assertEqual(row[:5], (.08, .02, .1, -.01, 2))
        self.assertEqual(json.loads(row[5]), {'counterfactual_id': 'cf'})
        revised = self.settlement.settled_outcome(DID, HORIZON, SUBJECT)
        self.assertEqual(revised['previous']['return_pct'], .01)
        self.assertEqual(revised['previous']['body'], {'counterfactual_id': 'cf'})
        self.assertEqual(revised['benchmark_return_pct'], .02)

    def test_revision_accepts_explicit_metric_and_body_changes(self):
        self.settlement.write_outcome(DID, self.outcome(.05), subject_key=SUBJECT)
        self.settlement.revise_outcome(
            DID, dict(self.outcome(.06), benchmark_return_pct=.04, mfe_pct=.2,
                      mae_pct=-.1, realized_r=3, body={'source': 'corrected'}),
            subject_key=SUBJECT, reason='correction', operator='ops')
        with self.settlement.events.transaction() as con:
            row = con.execute('SELECT benchmark_return_pct,mfe_pct,mae_pct,realized_r,body '
                              'FROM decision_outcomes_v2').fetchone()
        self.assertEqual(row[:4], (.04, .2, -.1, 3))
        self.assertEqual(json.loads(row[4]), {'source': 'corrected'})

    def test_revision_rolls_back_event_when_projection_update_fails(self):
        self.settlement.write_outcome(DID, self.outcome(.05), subject_key=SUBJECT)
        with self.settlement.events.transaction() as con:
            con.execute("CREATE TRIGGER reject_revision BEFORE UPDATE ON decision_outcomes_v2 "
                        "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.settlement.revise_outcome(DID, self.outcome(.07), subject_key=SUBJECT,
                                           reason='test', operator='ops')
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.projection(), (.01, .05))

    def test_repair_checks_return_and_distinguishes_missing_from_zero(self):
        self.settlement.write_outcome(DID, self.outcome(0), subject_key=SUBJECT)
        for ret, excess in ((.90, 0), (.01, None)):
            with self.subTest(ret=ret, excess=excess):
                with self.settlement.events.transaction() as con:
                    con.execute('UPDATE decision_outcomes_v2 SET return_pct=?, excess_return_pct=?',
                                (ret, excess))
                result = repair_projection(self.registry, operator='ops', reason='test')
                self.assertEqual(result['repaired'], 0)
                self.assertEqual(len(result['refused']), 1)
                self.assertEqual(self.projection(), (ret, excess))

    def test_return_only_float_noise_can_be_repaired(self):
        self.settlement.write_outcome(DID, self.outcome(.05), subject_key=SUBJECT)
        with self.settlement.events.transaction() as con:
            con.execute('UPDATE decision_outcomes_v2 SET return_pct=?', (.01 + 1e-13,))
        self.assertEqual(repair_projection(self.registry, operator='ops', reason='test')['repaired'], 1)
        self.assertEqual(self.projection(), (.01, .05))

if __name__ == '__main__':
    unittest.main()


class RepairAtomicityTests(SettlementBase):
    """投影与修复事件必须**同一事务**提交。

    分两次提交时若崩在中间：投影已修好、修复记录没写；而投影修好之后分歧就消失了，
    下次调用没有东西可修 —— **那条记录永久丢失**。这恰好发生在一个"存在意义就是留下
    审计轨迹"的函数里。
    """

    def test_a_failed_event_write_leaves_the_projection_untouched(self):
        from unittest.mock import patch
        self.settlement.write_outcome(DID, self.outcome(STORED), subject_key=SUBJECT)
        self.break_projection(RERUN)
        before = self.projection()
        with patch('scripts.live_trading.decision_ledger.outcome_jobs.insert_event',
                   side_effect=ValueError('boom')):
            with self.assertRaises(ValueError):
                repair_projection(self.registry, operator='ops', reason='r')
        # 事务整体回滚：投影未被改动、也没有半截的修复记录
        self.assertEqual(self.projection(), before)
        self.assertEqual(self.events('outcome_projection_repaired'), [])
