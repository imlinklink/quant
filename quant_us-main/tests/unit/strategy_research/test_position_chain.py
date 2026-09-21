"""L1（规划 §6）的执行链：技术包 → 门 → 模型 → 动作，用**合成输入**验证。

先验证链，再谈收益（§6.3）。这里钉死的是让这个角色**能存在**的那几环：

1. 有技术包时，**零新闻**也能把门开成 `OK`（否则模型永不被调用、L 恒等于 R）；
2. 技术事实**可被逐字引用**且归属为本持仓证券 —— 契约对 reduce/exit 要求至少引用一条证据，
   没有公司事件源时这是唯一能引用的东西；
3. 不给技术包时，行为与引入前逐字段相同（门仍按新闻判）；
4. CLI 的技术输入**按 session 截断**（未来 bar 进不来）。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts.portfolio_shadow.cli import (_account_facts_for_review, _technical_inputs,
                                         freeze_position_reviews, manifest_from_dict)
from scripts.portfolio_shadow.llm_overlay import gate
from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.position_overlay import (POSITION_EXIT, FakePositionModel,
                                                      POSITION_ABSTAIN,
                                                      decide_position_overlay)
from scripts.portfolio_shadow.position_overlay import subject_key_for
from scripts.portfolio_shadow.schema import Manifest, Opportunity, to_micro
from scripts.portfolio_shadow.store import ShadowStore, state_from_dict

CODE = 'SEC-A'
SESSION, EXEC = '2026-01-05', '2026-01-06'


def price_history(n=300, *, close=100.0, atr=2.0, future_bars=0):
    """`end=SESSION` ⇒ 末行恰好是评审日。`future_bars>0` 时额外追加**评审日之后**的行，
    用来验证「未来行情进不来」—— 这是前视的第一道闸。"""
    idx = pd.date_range(end=SESSION, periods=n, freq='B').normalize()
    frame = pd.DataFrame({'security_id': CODE, 'session': idx, 'raw_open': [close] * n,
                          'raw_high': [close * 1.01] * n, 'raw_low': [close * 0.99] * n,
                          'raw_close': [close] * n, 'volume': [1_000_000] * n,
                          'asof_atr': [atr] * n, 'scale_to_next': [1.0] * n})
    if future_bars:
        future = pd.date_range(start=pd.Timestamp(SESSION) + pd.Timedelta(days=1),
                               periods=future_bars, freq='B').normalize()
        spike = close * 3.0
        frame = pd.concat([frame, pd.DataFrame({
            'security_id': CODE, 'session': future, 'raw_open': [spike] * future_bars,
            'raw_high': [spike * 1.01] * future_bars, 'raw_low': [spike * 0.99] * future_bars,
            'raw_close': [spike] * future_bars, 'volume': [1_000_000] * future_bars,
            'asof_atr': [atr] * future_bars, 'scale_to_next': [1.0] * future_bars})],
            ignore_index=True)
    return frame


def manifest_dict(**llm_overrides):
    llm = {'overlay': 'fixed_pass', 'position_overlay': 'position_action',
           'evidence_mode': 'strict', 'evidence_window_days': 7, 'evidence_max_events': 50}
    llm.update(llm_overrides)
    return {'experiment_id': 'EXP', 'status': 'FROZEN', 'parent_strategy_id': 'B3',
            'parent_version': '1', 'parent_code_hash': 'abc', 'universe_id': 'u',
            'universe_hash': 'uh', 'account_scopes': ['SHADOW:EXP:R', 'SHADOW:EXP:L'],
            'initial_cash': 100000, 'currency': 'USD',
            'risk_policy': {'single_position_risk_bp': 100, 'max_weight_bp': 2000,
                            'max_positions': 5},
            'execution_policy': {'entry_rule': 'b3', 'exit_policy_id': 'H60',
                                 'horizon': 60},
            'llm_policy': llm, 'calendar_version': 'v1', 'data_hashes': {},
            'evaluation_protocol': {'main_metric': 'L_minus_R_return',
                                    'enrollment_window': '3-6 months',
                                    'review_date': '2026-12-31',
                                    'cost_allocation': 'L_pays_model_cost'}}


class ChainHarness(unittest.TestCase):
    LLM = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / 'out'
        (self.out / 'EXP').mkdir(parents=True)
        self.path = self.out / 'EXP' / 'manifest.json'
        self.path.write_text(json.dumps(manifest_dict(**self.LLM)), encoding='utf-8')
        self.m = manifest_from_dict(json.loads(self.path.read_text()))
        self.store = ShadowStore(self.out / 'EXP' / 'ledger.sqlite3', 'EXP')
        self.store.save_experiment(self.m)
        self.opp = Opportunity(
            experiment_id='EXP', security_id=CODE, source_candidate_id=CODE,
            parent_version='1', signal_session='2026-01-04',
            observed_at='2026-01-04T00:00:00+00:00', planned_execution_session=SESSION,
            rank=1, entry_rule='b3', stop_reference={'atr14_micro': to_micro(2)},
            exit_policy_id='H60', input_hash='h')
        self.oid = self.opp.opportunity_id()
        for scope in ('SHADOW:EXP:R', 'SHADOW:EXP:L'):
            res = step(new_account_state(scope, to_micro(100000)), session=SESSION,
                       bars={CODE: {'open': to_micro(100.0), 'high': to_micro(101.0),
                                    'low': to_micro(99.0), 'close': to_micro(100.5)}},
                       corporate_actions=[], intents=[self.opp], manifest=self.m)
            self.store.save_state(scope, res.state, res.nav, res.events, session=SESSION)

    def tearDown(self):
        self.tmp.cleanup()

    def freeze(self, *, technical_inputs, as_of='2026-01-05T22:00:00+00:00'):
        return freeze_position_reviews(
            self.store, self.m, session=SESSION, execution_session=EXEC,
            closes={CODE: to_micro(100.5)}, source=None, market_cutoff=None, as_of=as_of,
            evidence_mode='strict', knowledge_cutoff=None,
            technical_inputs=technical_inputs)

    def technical(self):
        return _technical_inputs(self.store, self.m, prices=price_history(),
                                 actions=pd.DataFrame(), session=SESSION,
                                 r_scope='SHADOW:EXP:R')


class TechnicalChainTests(ChainHarness):
    LLM = {'technical_packet': True, 'open_actions': ['hold', 'exit']}

    def test_zero_news_still_opens_the_gate(self):
        frozen, blocked = self.freeze(technical_inputs=self.technical())
        self.assertEqual(len(frozen), 1)
        self.assertEqual(blocked, 0)
        packet = frozen[0][1]
        self.assertEqual(packet['data_quality']['level'], 'OK')
        self.assertEqual(packet['data_quality']['news_events'], 0)
        self.assertGreater(packet['data_quality']['technical_facts'], 0)
        self.assertIsNone(gate(packet), '门不该短路 —— 否则模型永远不会被调用')

    def test_the_frozen_packet_offers_only_the_open_actions(self):
        frozen, _ = self.freeze(technical_inputs=self.technical())
        packet = frozen[0][1]
        self.assertEqual(packet['allowed_action_set'], ['exit', 'hold'])
        self.assertEqual({t['action'] for t in packet['allowed_actions']},
                         {'hold', 'exit'})

    def test_a_model_exit_on_technical_evidence_becomes_a_path_changing_action(self):
        frozen, _ = self.freeze(technical_inputs=self.technical())
        packet = frozen[0][1]
        item = packet['new_evidence'][0]
        self.assertEqual(item['subject_code'], CODE, '技术事实必须归属本持仓证券')
        self.assertIn(item['summary'], [e['summary'] for e in packet['new_evidence']])

        class ExitModel(FakePositionModel):
            def call(self, packet, deadline):
                out = super().call(packet, deadline)
                out['output']['action'] = 'exit'
                out['output']['thesis_state'] = 'INVALIDATED'
                out['output']['reason_codes'] = ['THESIS_INVALIDATED']
                out['output']['action_template_id'] = None
                return out

        model = ExitModel(evidence_from_packet=True, reason_code='THESIS_INVALIDATED')
        decision = decide_position_overlay(packet, model, '2026-01-06T13:20:00+00:00')
        self.assertEqual(decision.action, POSITION_EXIT)
        self.assertNotEqual(decision.action, POSITION_ABSTAIN)

    def test_an_action_outside_the_open_set_degrades_to_abstain(self):
        frozen, _ = self.freeze(technical_inputs=self.technical())
        packet = frozen[0][1]

        class NotOpenModel(FakePositionModel):
            def call(self, packet, deadline):
                out = super().call(packet, deadline)
                out['output']['action'] = 'post_exit_review'
                return out

        model = NotOpenModel(evidence_from_packet=True, reason_code='THESIS_WEAKENED')
        decision = decide_position_overlay(packet, model, '2026-01-06T13:20:00+00:00')
        self.assertEqual(decision.action, POSITION_ABSTAIN)
        self.assertEqual(decision.reason_code, 'INVALID_OUTPUT')

    def test_technical_inputs_never_include_future_bars(self):
        """未来 bar 进不来 —— 前视的第一道闸。**两道闸都要在**：

        ① CLI 切片按 session 截断；② `build_facts` 内部再按 session 过滤一次。
        只留一道时本测试会失败（两道都单独验过：去掉任一道，下面的相等断言就不成立）。
        """
        clean = _technical_inputs(self.store, self.m, prices=price_history(),
                                  actions=pd.DataFrame(), session=SESSION,
                                  r_scope='SHADOW:EXP:R')
        dirty = _technical_inputs(self.store, self.m,
                                  prices=price_history(future_bars=5),
                                  actions=pd.DataFrame(), session=SESSION,
                                  r_scope='SHADOW:EXP:R')
        # ① CLI 切掉了未来行
        self.assertLessEqual(dirty['history'][CODE].session.max(), pd.Timestamp(SESSION))
        # ② 即便未来行漏进 history，事实也必须一模一样
        from scripts.strategy_research.technical_packet import build_facts
        pos = state_from_dict(self.store.latest_state('SHADOW:EXP:R')[1]).positions[CODE]
        def compute(history):
            f = build_facts(security_id=CODE, history=history, atr14_micro=to_micro(2.0),
                            position=pos, account_facts=dirty['account_facts'],
                            session=SESSION, mark_price_micro=to_micro(100.5),
                            actions=pd.DataFrame())
            return {k: v.as_dict() for k, v in f.items()}
        self.assertEqual(compute(price_history()),
                         compute(price_history(future_bars=5)),
                         '未来 bar 改变了技术事实 ⇒ 前视')
        self.assertEqual(clean['account_facts']['risk_state'], 'NORMAL')
        self.assertIsNotNone(clean['account_facts']['cash_share'])
        self.assertEqual(clean['atr14'][CODE], to_micro(2.0))

    def test_replay_is_idempotent_for_the_same_session(self):
        """同一 session 重复冻结：包是「首次写入即冻结」，第二次必须原样复用。"""
        first, _ = self.freeze(technical_inputs=self.technical())
        second, _ = self.freeze(technical_inputs=self.technical())
        self.assertEqual(first[0][1]['packet_id'], second[0][1]['packet_id'])


class LegacyChainTests(ChainHarness):
    """不给技术包时，行为与引入前逐字段相同。"""

    def test_without_technical_inputs_the_gate_still_needs_news(self):
        frozen, _ = self.freeze(technical_inputs=None)
        packet = frozen[0][1]
        self.assertEqual(packet['data_quality']['level'], 'LLM_INSUFFICIENT')
        self.assertNotIn('technical', packet)
        self.assertNotIn('allowed_action_set', packet)
        self.assertEqual(gate(packet)[1], 'INSUFFICIENT_EVIDENCE')

    def test_a_packet_without_the_declaration_keeps_every_action_open(self):
        frozen, _ = self.freeze(technical_inputs=None)
        packet = frozen[0][1]
        self.assertGreater(len(packet['allowed_actions']), 2,
                           '未声明动作集时不得收窄可选动作')


class TimeoutAndBudgetTests(ChainHarness):
    """启动前必须存在且**真的生效**的两道闸（规划 §4.1 用户裁定）。

    两者此前都不存在：`timeout_seconds` 只被赋值、从未被使用（一次挂死的调用能让日作业
    无限等待），调用预算则完全没有。
    """

    LLM = {'technical_packet': True, 'open_actions': ['hold', 'exit']}

    def _reviewer(self, *, budget=None, timeout=60, model=None):
        from scripts.portfolio_shadow.position_overlay import FakePositionModel
        from scripts.portfolio_shadow.position_review import PositionReviewer, PositionSubject
        reviewer = PositionReviewer(
            self.store, scope='SHADOW:EXP:L',
            model_factory=(lambda: model or FakePositionModel(
                action='hold', cost_micro=0, evidence_from_packet=True)),
            model_id='fixture', timeout_seconds=timeout, model_budget_micro=budget)
        frozen, _blocked = self.freeze(technical_inputs=self.technical())
        packet = frozen[0][1]          # frozen 是 [(key, packet)]，不是 (frozen, blocked)
        subject = PositionSubject(opportunity_id=self.oid, security_id=CODE,
                                  reviewed_session=SESSION, execution_session=EXEC)
        return reviewer, subject, packet

    def test_timeout_is_enforced_not_just_configured(self):
        import time as _time

        class HangingModel:
            def __init__(self):
                self.calls = 0

            def call(self, packet, deadline):
                self.calls += 1
                _time.sleep(2.0)          # 远超超时
                return {'status': 'OK', 'output': None}

        hanging = HangingModel()
        reviewer, subject, packet = self._reviewer(timeout=0.05, model=hanging)
        started = _time.time()
        outcome = reviewer.review(subject, packet, '2026-01-06T13:20:00+00:00')
        elapsed = _time.time() - started
        self.assertLess(elapsed, 1.0, '超时没有被收口：调用把主线程拖住了')
        self.assertEqual(outcome.decision.action, POSITION_ABSTAIN)
        self.assertEqual(outcome.decision.reason_code, 'TIMED_OUT')
        self.assertEqual(hanging.calls, 1)

    def test_timeout_is_capped_by_the_remaining_decision_window(self):
        from datetime import datetime, timedelta, timezone
        reviewer, _subject, _packet = self._reviewer(timeout=60)
        now = datetime(2026, 1, 6, 13, 19, 55, tzinfo=timezone.utc)
        reviewer.now = lambda: now                     # 距截止只剩 5 秒
        deadline = (now + timedelta(seconds=5)).isoformat()
        self.assertAlmostEqual(reviewer._effective_timeout(deadline), 5.0, places=3)

    def test_budget_exhaustion_abstains_without_calling_the_model(self):
        from dataclasses import replace as _replace
        from scripts.portfolio_shadow.position_overlay import FakePositionModel
        # 账户已计入的模型成本 ≥ 预算
        seq, body = self.store.latest_state('SHADOW:EXP:L')
        state = state_from_dict(body)
        bumped = _replace(state, sequence=seq + 1, model_cost=9_000_000)
        self.store.save_state('SHADOW:EXP:L', bumped,
                              {'session': SESSION, 'equity': 1, 'cash_available': 1,
                               'gross_exposure': 0, 'fees': 0, 'valuation_status': 'OK',
                               'revision': 2}, [], session=SESSION)
        calls = []

        class CountingModel(FakePositionModel):
            def call(self, packet, deadline):
                calls.append(1)
                return super().call(packet, deadline)

        reviewer, subject, packet = self._reviewer(
            budget=5_000_000, model=CountingModel(evidence_from_packet=True))
        outcome = reviewer.review(subject, packet, '2026-01-06T13:20:00+00:00')
        self.assertEqual(calls, [], '预算用尽时不得发起调用')
        # 动作词汇用角色中立的 'ABSTAIN'（与编排器里其它程序侧分支一致 ——
        # 租约在飞、崩溃遗留走的也是它）；语义与可报告性由**原因码**承载。
        self.assertNotIn(outcome.decision.action, ('POSITION_EXIT',))
        self.assertTrue(outcome.decision.action.endswith('ABSTAIN'))
        self.assertEqual(outcome.decision.reason_code, 'MODEL_BUDGET_EXHAUSTED')
        from scripts.portfolio_shadow.llm_overlay import NO_CALL_REASONS, is_program_abstain
        self.assertTrue(is_program_abstain('MODEL_BUDGET_EXHAUSTED'))
        self.assertIn('MODEL_BUDGET_EXHAUSTED', NO_CALL_REASONS)
        self.assertEqual(outcome.decision.model_cost, 0)
        # 必须落 Application，否则结算会把「没评审过」当成缺口
        self.assertIsNotNone(self.store.application('SHADOW:EXP:L', subject.key()))

    def test_within_budget_the_model_is_called(self):
        from scripts.portfolio_shadow.position_overlay import FakePositionModel
        calls = []

        class CountingModel(FakePositionModel):
            def call(self, packet, deadline):
                calls.append(1)
                return super().call(packet, deadline)

        reviewer, subject, packet = self._reviewer(
            budget=5_000_000, model=CountingModel(evidence_from_packet=True))
        reviewer.review(subject, packet, '2026-01-06T13:20:00+00:00')
        self.assertEqual(len(calls), 1)

    def test_no_budget_configured_keeps_the_old_behaviour(self):
        reviewer, subject, packet = self._reviewer(budget=None)
        outcome = reviewer.review(subject, packet, '2026-01-06T13:20:00+00:00')
        self.assertNotEqual(outcome.decision.reason_code, 'MODEL_BUDGET_EXHAUSTED')


class ManifestBudgetTests(unittest.TestCase):
    def test_real_model_requires_a_declared_budget(self):
        from scripts.portfolio_shadow.schema import Manifest
        base = manifest_dict(use_real_model=True, knowledge_cutoff='unknown',
                             model_budget_micro=5_000_000)
        self.assertEqual(manifest_from_dict(base).validate(), [])
        without = {k: v for k, v in base['llm_policy'].items()
                   if k != 'model_budget_micro'}
        errors = manifest_from_dict({**base, 'llm_policy': without}).validate()
        self.assertTrue(any('model_budget_micro' in e for e in errors), errors)


class AccountFactTests(unittest.TestCase):
    def test_drawdown_and_cash_come_from_the_latest_settlement(self):
        state = new_account_state('SHADOW:x:R', to_micro(100000))
        state.high_water = to_micro(120000)
        facts = _account_facts_for_review(state, {'equity': to_micro(100000),
                                                  'full_cost_equity': to_micro(108000),
                                                  'cash_available': to_micro(40000),
                                                  'gross_exposure': to_micro(60000)})
        self.assertAlmostEqual(facts['account_drawdown'], 1 - 108000 / 120000, places=9)
        self.assertAlmostEqual(facts['cash_share'], 0.4, places=9)
        self.assertAlmostEqual(facts['gross_exposure'], 0.6, places=9)
        self.assertEqual(facts['risk_state'], 'NORMAL')

    def test_missing_nav_yields_no_fabricated_numbers(self):
        state = new_account_state('SHADOW:x:R', to_micro(100000))
        facts = _account_facts_for_review(state, None)
        self.assertNotIn('cash_share', facts)
        self.assertNotIn('account_drawdown', facts)


if __name__ == '__main__':
    unittest.main()
