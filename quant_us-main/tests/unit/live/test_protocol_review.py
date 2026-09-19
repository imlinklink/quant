"""协议复盘排期（§6.5 / §13 P2）：每周一次，无统计基础不调用模型，且永不改配置。"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.live_trading.decision_ledger.event_store import EventStore, stable_id
from scripts.live_trading.decision_ledger.protocol_changes import candidates, pending
from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.protocol_review import ProtocolReviewScheduler

TZ = 'America/New_York'
CONFIG = {'llm_decision': {'protocol_review': {
    'enabled': True, 'weekday': 4, 'time': '18:30', 'protocol_version': 'v1'}}}
# 2026-09-18 是周五（weekday=4）
FRIDAY = datetime(2026, 9, 18, 19, 0, tzinfo=ZoneInfo(TZ))


def proposal_output(packet, variable='execution_policy.horizon'):
    return {'schema_version': 'review-v1', 'packet_id': packet.get('packet_id'),
            'status': 'complete',
            'failure_patterns': [], 'reason_codes': [],
            'proposed_change': {'variable': variable, 'from_value': 60, 'to_value': 45,
                                'direction': 'decrease'},
            'expected_improvement': {'metric': 'calmar', 'direction': 'increase'},
            'possible_regression': [{'metric': 'turnover', 'direction': 'increase'}],
            'validation_plan': {'window_sessions': 60, 'min_samples': 30,
                                'stop_condition': '超额转负即停'},
            'missing_information': []}


def no_change_output(packet):
    return {'schema_version': 'review-v1', 'packet_id': packet.get('packet_id'),
            'status': 'complete', 'failure_patterns': [], 'proposed_change': None,
            'expected_improvement': None, 'possible_regression': [],
            'validation_plan': None, 'reason_codes': [],
            'missing_information': ['样本不足']}


class SchedulerBase(unittest.TestCase):
    WITH_SAMPLES = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = PositionRegistry(Path(self.tmp.name) / 'x.sqlite3', 'DRY-RUN')
        with EventStore(self.registry).transaction():
            pass
        con = sqlite3.connect(str(self.registry.path))
        if self.WITH_SAMPLES:
            con.execute(
                'INSERT OR REPLACE INTO llm_decision_runs '
                '(account_scope, decision_id, role, subject_type, subject_id, as_of, '
                ' status, input_snapshot_id, prompt_version, output_schema_version, '
                ' feature_version, rule_version, permission_version, provider, model_id, '
                ' created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('DRY-RUN', 'decision_a', 'selection', 'research_batch', 'batch',
                 '2026-09-15', 'validated', 'snap', 'v1', 'v1', 'v1', 'v1', 'v1',
                 'configured', 'deepseek-chat', '2026-09-15'))
            con.execute('INSERT OR REPLACE INTO decision_outcomes_v2 VALUES '
                        '(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        ('DRY-RUN', 'decision_a', '1d', 'US.A', '2026-09-16', 0.01, 0.0,
                         0.01, None, -0.01, None, 'good', '{}'))
        con.commit()
        con.close()
        self.scheduler = ProtocolReviewScheduler(self.registry, CONFIG)

    def tearDown(self):
        self.tmp.cleanup()

    def events(self, event_type):
        return [e for e in EventStore(self.registry).events()
                if e.get('event_type') == event_type]


class DueTests(SchedulerBase):
    def test_not_due_outside_the_configured_slot(self):
        saturday = datetime(2026, 9, 19, 19, 0, tzinfo=ZoneInfo(TZ))
        self.assertIsNone(self.scheduler.period_due(saturday))

    def test_not_due_before_the_configured_time(self):
        early = datetime(2026, 9, 18, 9, 0, tzinfo=ZoneInfo(TZ))
        self.assertIsNone(self.scheduler.period_due(early))

    def test_due_on_the_configured_weekday_after_the_time(self):
        self.assertEqual(self.scheduler.period_due(FRIDAY), '2026-W38')

    def test_disabled_means_never_due(self):
        """默认关闭：排期会发起付费调用，不该在无人点头时自动开始。"""
        config = {'llm_decision': {'protocol_review': {'enabled': False}}}
        scheduler = ProtocolReviewScheduler(self.registry, config)
        self.assertIsNone(scheduler.period_due(FRIDAY))

    def test_period_key_changes_week_over_week(self):
        next_week = datetime(2026, 9, 25, 19, 0, tzinfo=ZoneInfo(TZ))
        self.assertNotEqual(self.scheduler.period_key(FRIDAY),
                            self.scheduler.period_key(next_week))


class RunTests(SchedulerBase):
    def test_not_due_run_does_not_touch_the_ledger(self):
        summary = self.scheduler.run(now=datetime(2026, 9, 19, 19, 0,
                                                  tzinfo=ZoneInfo(TZ)))
        self.assertEqual(summary['skipped'], 'NOT_DUE')
        self.assertEqual(self.events('protocol_review_completed'), [])
        self.assertEqual(self.events('daily_job_claimed'), [])

    def test_run_records_completion_and_carries_no_config_change(self):
        summary = self.scheduler.run(now=FRIDAY, call_model=lambda c, p: no_change_output(p))
        self.assertEqual(summary['period'], '2026-W38')
        self.assertIsNone(summary['candidate'])
        self.assertIn('不构成失败', summary['note'])
        self.assertEqual(len(self.events('protocol_review_completed')), 1)
        # 只写事件：配置与权限等级一字未动
        self.assertEqual(self.events('protocol_version_approved'), [])
        self.assertEqual(pending(self.registry), [])

    def test_rerun_in_the_same_period_does_not_call_again(self):
        calls = []

        def counting(_c, p):
            calls.append(1)
            return no_change_output(p)

        self.scheduler.run(now=FRIDAY, call_model=counting)
        again = self.scheduler.run(now=FRIDAY, call_model=counting)
        self.assertEqual(again['skipped'], 'ALREADY_RUN')
        self.assertEqual(len(calls), 1, '同一周期不得再调用付费模型')

    def test_a_proposal_becomes_a_candidate_awaiting_human_approval(self):
        summary = self.scheduler.run(now=FRIDAY,
                                     call_model=lambda c, p: proposal_output(p))
        self.assertEqual(summary['candidate_variable'], 'execution_policy.horizon')
        self.assertIn('须人工批准', summary['note'])
        listed = pending(self.registry)
        self.assertEqual(len(listed), 1)
        self.assertIsNone(listed[0]['decision'])

    def test_invalid_model_output_leaves_no_candidate(self):
        bad = {'packet_id': 'wrong', 'status': 'complete'}
        summary = self.scheduler.run(now=FRIDAY, call_model=lambda c, p: bad)
        self.assertEqual(summary['status'], 'failed')
        self.assertIsNone(summary['candidate'])
        self.assertEqual(candidates(self.registry), [])

    def test_model_output_proposing_a_safety_switch_leaves_no_candidate(self):
        summary = self.scheduler.run(
            now=FRIDAY,
            call_model=lambda c, p: proposal_output(p,
                                                    variable='hard_exit.enabled'))
        self.assertEqual(summary['status'], 'failed')
        self.assertEqual(candidates(self.registry), [])
        self.assertTrue(any('安全相关变量' in e for e in summary['validation_errors']),
                        summary['validation_errors'])

    def test_force_period_is_used_for_manual_catch_up(self):
        summary = self.scheduler.run(now=datetime(2026, 9, 19, 19, 0,
                                                  tzinfo=ZoneInfo(TZ)),
                                     force_period='2026-W38',
                                     call_model=lambda c, p: no_change_output(p))
        self.assertEqual(summary['period'], '2026-W38')


class NoBasisTests(SchedulerBase):
    WITH_SAMPLES = False

    def test_without_settled_samples_the_model_is_not_called(self):
        """调用只会得到"样本不足"，却照样花钱 —— 没有统计基础就不该发起。"""
        called = []

        def counting(_c, p):
            called.append(1)
            return no_change_output(p)

        summary = self.scheduler.run(now=FRIDAY, call_model=counting)
        self.assertEqual(summary['skipped'], 'NO_SETTLED_SAMPLES')
        self.assertEqual(called, [])
        skipped = self.events('protocol_review_skipped')
        self.assertEqual(len(skipped), 1)
        self.assertIn('未被消耗', skipped[0]['payload']['note'])

    def test_a_skipped_week_is_not_consumed_so_later_data_still_runs(self):
        """没有基础时不认领周期：数据到齐后同一周仍可再跑。"""
        self.scheduler.run(now=FRIDAY, call_model=lambda c, p: no_change_output(p))
        self.assertEqual(self.events('daily_job_claimed'), [])
        # 数据到齐后同一周期应能跑起来
        con = sqlite3.connect(str(self.registry.path))
        con.execute(
            'INSERT OR REPLACE INTO llm_decision_runs '
            '(account_scope, decision_id, role, subject_type, subject_id, as_of, status, '
            ' input_snapshot_id, prompt_version, output_schema_version, feature_version, '
            ' rule_version, permission_version, provider, model_id, created_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('DRY-RUN', 'decision_b', 'selection', 'research_batch', 'b', '2026-09-15',
             'validated', 's', 'v1', 'v1', 'v1', 'v1', 'v1', 'configured',
             'deepseek-chat', '2026-09-15'))
        con.execute('INSERT OR REPLACE INTO decision_outcomes_v2 VALUES '
                    '(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('DRY-RUN', 'decision_b', '1d', 'US.B', '2026-09-16', 0.01, 0.0,
                     0.01, None, -0.01, None, 'good', '{}'))
        con.commit()
        con.close()
        summary = self.scheduler.run(now=FRIDAY, call_model=lambda c, p: no_change_output(p))
        self.assertEqual(summary['period'], '2026-W38')
        self.assertEqual(summary['status'], 'validated')


if __name__ == '__main__':
    unittest.main()


class TimerWiringTests(unittest.TestCase):
    """接进服务定时线程：只在到期时跑、同周期不重复、且**不被交易日门挡住**。"""

    def make(self, config, *, integration=False):
        from scripts.live_trading.outcome_scheduler import OutcomeSchedulerThread
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(config)
            if integration:
                cfg['shadow_integration'] = {'enabled': True}
                cfg['buy_strategy_v2'] = {'mode': 'shadow'}
            thread = OutcomeSchedulerThread(cfg, str(Path(tmp) / 'config.yaml'),
                                            stop_event=__import__('threading').Event())
            return thread

    def call_model(self):
        return lambda contract, packet: no_change_output(packet)

    def test_disabled_still_wires_but_never_runs(self):
        """默认关闭时不配置项也必须能启动 —— 定时器不该因为功能没开就报错。"""
        thread = self.make({'llm_decision': {'protocol_review': {'enabled': False}}})
        thread.protocol_reviewer.registry = object()   # 不该被碰到
        self.assertFalse(thread._protocol_review_tick(FRIDAY))

    def test_nothing_happens_outside_the_configured_slot(self):
        thread = self.make(CONFIG)
        saturday = datetime(2026, 9, 19, 19, 0, tzinfo=ZoneInfo(TZ))
        self.assertFalse(thread._protocol_review_tick(saturday))

    def test_the_same_period_is_attempted_only_once_per_process(self):
        """30 秒轮询不该把同一周期反复走一遍。"""
        thread = self.make(CONFIG)
        calls = []

        def fake_run(**kwargs):
            calls.append(kwargs)
            return {'skipped': 'NO_SETTLED_SAMPLES'}

        thread.protocol_reviewer.run = fake_run
        self.assertTrue(thread._protocol_review_tick(FRIDAY))
        self.assertFalse(thread._protocol_review_tick(FRIDAY))
        self.assertEqual(len(calls), 1)

    def test_runs_on_a_non_trading_day_when_integration_is_on(self):
        """周复盘的默认星期是美东周五收盘后（北京周六）—— 那天不是交易 session。

        集成分支原先把整个 tick 挡在 `sessions().empty` 之后；放在门后会让周复盘
        **永远不跑，且不报错**。
        """
        config = {
            'llm_decision': {'engine_v2': {'account_scope': 'DRY-RUN',
                                           'selection': 'shadow'},
                             'protocol_review': {'enabled': True, 'weekday': 5,
                                                 'time': '18:30'}},
        }
        thread = self.make(config, integration=True)
        calls = []
        thread.protocol_reviewer.run = lambda **kw: calls.append(kw) or {'skipped': 'x'}
        saturday = datetime(2026, 9, 19, 19, 0, tzinfo=ZoneInfo(TZ))
        thread._integration_tick(saturday)
        self.assertEqual(len(calls), 1, '非交易日的周复盘被 session 门挡住了')
