"""持仓评审的结算接线（影子侧）：R 不变、L 按档位相对减仓、可重放、冻结≠成交。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts.portfolio_shadow.cli import cmd_settle_session, manifest_from_dict, replay
from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.position_overlay import subject_key_for
from scripts.portfolio_shadow.position_packet import build_position_packet
from scripts.portfolio_shadow.schema import (Application, Manifest, Opportunity,
                                             Position, to_micro)
from scripts.portfolio_shadow.store import ShadowStore, state_from_dict

SIGNAL, EXEC, NEXT = '2026-01-05', '2026-01-06', '2026-01-07'
CODE = 'SEC-A'


def manifest_dict(**llm_overrides):
    llm = {'overlay': 'fixed_pass', 'position_overlay': 'position_action',
           'evidence_mode': 'strict', 'evidence_window_days': 7,
           'evidence_max_events': 50}
    llm.update(llm_overrides)
    return {
        'experiment_id': 'EXP', 'status': 'FROZEN', 'parent_strategy_id': 'B3',
        'parent_version': '1', 'parent_code_hash': 'abc', 'universe_id': 'u',
        'universe_hash': 'uh', 'account_scopes': ['SHADOW:EXP:R', 'SHADOW:EXP:L'],
        'initial_cash': 100000, 'currency': 'USD',
        'risk_policy': {'single_position_risk_bp': 100, 'max_weight_bp': 2000,
                        'max_positions': 5},
        'execution_policy': {'entry_rule': 'b3', 'exit_policy_id': 'H60', 'horizon': 60},
        'llm_policy': llm, 'calendar_version': 'v1', 'data_hashes': {},
        'evaluation_protocol': {'main_metric': 'L_minus_R_return',
                                'enrollment_window': '3-6 months',
                                'review_date': '2026-12-31',
                                'cost_allocation': 'L_pays_model_cost'},
    }


def bars(open_, close, low=None, high=None):
    return {CODE: {'open': to_micro(open_), 'high': to_micro(high if high is not None else close + 1),
                   'low': to_micro(low if low is not None else open_ - 1),
                   'close': to_micro(close)}}


def prices_frame(session, open_, close, low, high):
    return pd.DataFrame({'session': [pd.Timestamp(session)], 'security_id': [CODE],
                         'raw_open': [open_], 'raw_high': [high], 'raw_low': [low],
                         'raw_close': [close]})


EMPTY_ACTIONS = pd.DataFrame({'security_id': [], 'ex_date': [], 'action_type': []})


class PositionHarness:
    """共享夹具。子类用类属性覆盖 llm_policy（manifest 一旦冻结就不可改，故必须在
    `save_experiment` 之前定好）。"""
    LLM = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / 'out'
        (self.out / 'EXP').mkdir(parents=True)
        self.manifest_path = self.out / 'EXP' / 'manifest.json'
        self.manifest_path.write_text(json.dumps(manifest_dict(**self.LLM)),
                                      encoding='utf-8')
        self.store = ShadowStore(self.out / 'EXP' / 'ledger.sqlite3', 'EXP')
        self.store.save_experiment(manifest_from_dict(json.loads(
            self.manifest_path.read_text())))
        self.manifest = manifest_from_dict(json.loads(self.manifest_path.read_text()))
        self.opp = Opportunity(
            experiment_id='EXP', security_id=CODE, source_candidate_id=CODE,
            parent_version='1', signal_session=SIGNAL,
            observed_at='2026-01-04T00:00:00+00:00', planned_execution_session=EXEC,
            rank=1, entry_rule='b3', stop_reference={'atr14_micro': to_micro(2)},
            exit_policy_id='H60', input_hash='h')
        self.oid = self.opp.opportunity_id()

    def tearDown(self):
        self.tmp.cleanup()

    def enter_both(self):
        """R 与 L 各建一笔 125 股持仓（100 开盘、stop 92）。"""
        states = {}
        for scope in ('SHADOW:EXP:R', 'SHADOW:EXP:L'):
            res = step(new_account_state(scope, to_micro(100000)), session=EXEC,
                       bars=bars(100, 100.5), corporate_actions=[], intents=[self.opp],
                       manifest=self.manifest)
            self.store.save_state(scope, res.state, res.nav, res.events, session=EXEC)
            states[scope] = res.state
        return states

    def freeze_position_review(self, action):
        """冻结一个持仓评审包 + L 侧动作（主体 = R 的持仓）。"""
        key = subject_key_for(self.oid, NEXT)
        r_state = state_from_dict(self.store.latest_state('SHADOW:EXP:R')[1])
        pos = r_state.positions[CODE]
        packet = build_position_packet(
            security_id=CODE, trade={'entry_session': EXEC},
            protection={'active_stop': pos.stop_micro,
                        'initial_stop': pos.initial_stop_micro,
                        'hard_exit_authoritative': True},
            events=[], as_of='2026-01-06T22:00:00+00:00', execution_session=NEXT,
            account_scope='SHADOW:EXP:L', experiment_id='EXP',
            opportunity_id=self.oid, reviewed_session=EXEC, shares=pos.shares,
            entry_price_micro=pos.entry_price_micro, mark_price_micro=to_micro(100.5))
        self.store.put_packet(key, packet)
        self.store.put_application(Application(
            scope='SHADOW:EXP:L', opportunity_id=key, action=action,
            reason_code='THESIS_WEAKENED', decision_id='dec-1',
            as_of='2026-01-06T22:00:00+00:00', decision_frozen=True,
            execution_applied=False))
        return key

    def settle(self, open_=100.0, close=100.5, low=99.0, high=101.0):
        prices = prices_frame(NEXT, open_, close, low, high)
        with patch('scripts.portfolio_shadow.cli._market_data',
                   return_value=(prices, EMPTY_ACTIONS, None, None)):
            return cmd_settle_session(type('A', (), {
                'manifest': str(self.manifest_path), 'output': str(self.out),
                'session': NEXT, 'etf_raw': None})())

    def shares(self, scope):
        row = self.store.latest_state(scope)
        return state_from_dict(row[1]).positions.get(CODE)

class PositionSettleTests(PositionHarness, unittest.TestCase):

    def test_reduce_applies_to_l_only_and_is_tier_relative(self):
        self.enter_both()
        self.assertEqual(self.shares('SHADOW:EXP:R').shares, 125)
        key = self.freeze_position_review('POSITION_REDUCE_25')
        self.settle()
        # R 照常服从规则：数量与持有计数都不受 LLM 动作影响
        self.assertEqual(self.shares('SHADOW:EXP:R').shares, 125)
        # L 按**自身**剩余量的 25% 减仓（模板的绝对数量是按 R 的剩余量算的）
        self.assertEqual(self.shares('SHADOW:EXP:L').shares, 94)
        # 冻结 ≠ 成交：结算后标记为已应用
        self.assertTrue(self.store.application('SHADOW:EXP:L', key)['execution_applied'])

    def test_exit_flattens_l_only(self):
        self.enter_both()
        key = self.freeze_position_review('POSITION_EXIT')
        self.settle()
        self.assertEqual(self.shares('SHADOW:EXP:R').shares, 125)
        self.assertIsNone(self.shares('SHADOW:EXP:L'))
        self.assertTrue(self.store.application('SHADOW:EXP:L', key)['execution_applied'])

    def test_hold_does_not_change_anything(self):
        self.enter_both()
        self.freeze_position_review('POSITION_HOLD')
        before_r = self.shares('SHADOW:EXP:R').shares
        self.settle()
        self.assertEqual(self.shares('SHADOW:EXP:R').shares, before_r)
        self.assertEqual(self.shares('SHADOW:EXP:L').shares, 125)

    def test_gap_stop_wins_over_the_position_action(self):
        self.enter_both()
        key = self.freeze_position_review('POSITION_EXIT')
        # 开盘跳空穿过 stop(92) → 硬退出优先，两账户同价清仓
        self.settle(open_=90.0, low=88.0, high=90.0)
        self.assertIsNone(self.shares('SHADOW:EXP:R'))
        self.assertIsNone(self.shares('SHADOW:EXP:L'))
        # 动作被硬退出抢先，故不得标为已应用
        self.assertFalse(self.store.application('SHADOW:EXP:L', key)['execution_applied'])

    def test_rerun_is_idempotent(self):
        self.enter_both()
        self.freeze_position_review('POSITION_REDUCE_25')
        self.settle()
        l_after_first = self.shares('SHADOW:EXP:L').shares
        self.settle()
        self.assertEqual(self.shares('SHADOW:EXP:L').shares, l_after_first,
                         '同一 session 重跑不得再次减仓')
        self.assertEqual(self.shares('SHADOW:EXP:R').shares, 125)

    def test_replay_reproduces_both_accounts(self):
        self.enter_both()
        self.freeze_position_review('POSITION_REDUCE_25')
        self.settle()
        for scope in ('SHADOW:EXP:R', 'SHADOW:EXP:L'):
            row = self.store.latest_state(scope)
            replayed = replay(scope, to_micro(100000), self.store.events(scope))
            self.assertEqual(replayed.state_hash(), state_from_dict(row[1]).state_hash(),
                             scope)

    def test_missing_application_past_deadline_freezes_explicit_abstain(self):
        self.enter_both()
        key = subject_key_for(self.oid, NEXT)
        r_state = state_from_dict(self.store.latest_state('SHADOW:EXP:R')[1])
        pos = r_state.positions[CODE]
        self.store.put_packet(key, build_position_packet(
            security_id=CODE, trade={'entry_session': EXEC},
            protection={'active_stop': pos.stop_micro,
                        'initial_stop': pos.initial_stop_micro},
            events=[], as_of='2026-01-06T22:00:00+00:00', execution_session=NEXT,
            account_scope='SHADOW:EXP:L', experiment_id='EXP',
            opportunity_id=self.oid, reviewed_session=EXEC, shares=pos.shares,
            entry_price_micro=pos.entry_price_micro, mark_price_micro=to_micro(100.5)))
        self.settle()
        app = self.store.application('SHADOW:EXP:L', key)
        # 绝不静默跳过：没有动作就要留下「为什么没有」
        self.assertIsNotNone(app)
        self.assertEqual(app['reason_code'], 'DECISION_DEADLINE_MISSED')
        self.assertEqual(self.shares('SHADOW:EXP:L').shares, 125)


    def test_market_only_evidence_can_drive_a_reduce_end_to_end(self):
        """端到端：证据只有市场级日报时，reduce 仍然可达、并通过评审真的改变 L 路径。

        这是放宽归属守卫的**全部意义**：此前 `POSITION_NO_COMPANY_EVIDENCE` 要求引用本证券
        证据，而实测证据供给只有 MARKET ⇒ 该动作结构上不可达、L 恒等于 R。
        """
        from scripts.portfolio_shadow.position_overlay import FakePositionModel
        from scripts.portfolio_shadow.position_review import PositionReviewer, PositionSubject
        self.enter_both()
        r_state = state_from_dict(self.store.latest_state('SHADOW:EXP:R')[1])
        pos = r_state.positions[CODE]
        key = subject_key_for(self.oid, NEXT)
        packet = build_position_packet(
            security_id=CODE, trade={'entry_session': EXEC},
            protection={'active_stop': pos.stop_micro,
                        'initial_stop': pos.initial_stop_micro},
            events=[{'evidence_id': 'ev-market-1', 'security_id': 'MARKET',
                     'source': 'daily_market_report', 'source_url': '', 'kind': 'news',
                     'event_type': 'news', 'published_at': '2026-01-06T20:00:00+00:00',
                     'observed_at': '2026-01-06T20:05:00+00:00', 'cluster_id': 'm1',
                     'title': 't', 'summary': '市场级背景', 'excerpt': '市场级背景',
                     'summary_truncated': False, 'content_hash': 'h'}],
            as_of='2026-01-06T22:00:00+00:00', execution_session=NEXT,
            account_scope='SHADOW:EXP:L', experiment_id='EXP',
            opportunity_id=self.oid, reviewed_session=EXEC, shares=pos.shares,
            entry_price_micro=pos.entry_price_micro, mark_price_micro=to_micro(100.5),
            market_context={'observed_at': '2026-01-06T20:00:00+00:00'})
        self.assertEqual(packet['data_quality']['level'], 'OK')
        self.store.put_packet(key, packet)
        template = next(t['template_id'] for t in packet['allowed_actions']
                        if (t.get('constraints') or {}).get('tier') == 0.25)
        reviewer = PositionReviewer(
            self.store, scope='SHADOW:EXP:L',
            model_factory=lambda: FakePositionModel(action='reduce', template_id=template,
                                                    evidence_from_packet=True),
            model_id='fixture')
        subject = PositionSubject(opportunity_id=self.oid, security_id=CODE,
                                  reviewed_session=EXEC, execution_session=NEXT)
        outcome = reviewer.review(subject, packet, '2026-01-07T14:20:00+00:00')
        self.assertTrue(outcome.frozen)
        self.assertEqual(outcome.decision.action, 'POSITION_REDUCE_25')

        self.settle()
        self.assertEqual(self.shares('SHADOW:EXP:R').shares, 125)
        self.assertEqual(self.shares('SHADOW:EXP:L').shares, 94)
        # 放宽的配套：这次判断的依据归属必须可见
        from scripts.portfolio_shadow.report import position_metrics
        metrics = position_metrics(self.store, self.manifest)
        self.assertEqual(metrics['market_only_applications'], 1)
        self.assertEqual(metrics['path_applied'], 1)

    def test_metrics_count_the_applied_path_change(self):
        from scripts.portfolio_shadow.report import position_metrics
        self.enter_both()
        self.freeze_position_review('POSITION_REDUCE_25')
        self.settle()
        m = position_metrics(self.store, self.manifest)
        self.assertEqual(m['eligible'], 1)
        self.assertEqual(m['reviewed'], 1)
        self.assertEqual(m['model_informed'], 1)
        self.assertEqual(m['path_changed'], 1)
        self.assertEqual(m['path_applied'], 1)
        self.assertEqual(m['path_change_rate'], 1.0)

    def test_metrics_report_none_when_nothing_to_review(self):
        # 「没有对象可评」≠「评了但全部选择持有」：分母为 0 一律 None，不返回 0
        from scripts.portfolio_shadow.report import position_metrics
        m = position_metrics(self.store, self.manifest)
        self.assertEqual(m['eligible'], 0)
        self.assertIsNone(m['path_change_rate'])
        self.assertIsNone(m['data_block_rate'])
        self.assertIn('没有可评审的持仓', m['note'])


class OverlayOffTests(PositionHarness, unittest.TestCase):
    """position_overlay 关闭时整个持仓阶段必须是 no-op（既有实验不受影响）。"""
    LLM = {'position_overlay': 'off'}

    def test_overlay_off_ignores_frozen_position_actions(self):
        self.enter_both()
        key = self.freeze_position_review('POSITION_EXIT')
        self.settle()
        self.assertEqual(self.shares('SHADOW:EXP:L').shares, 125)
        self.assertFalse(self.store.application('SHADOW:EXP:L', key)['execution_applied'])


if __name__ == '__main__':
    unittest.main()
