"""Portfolio 的调用方（§6.3）：限额导出、候选构造、以及**开启后行为不变**的证明。

最强的那条断言是 `test_enabling_changes_nothing_under_shadow`：Portfolio 的权限等级是
`shadow` ⇒ 引擎的 `effective_action` 是父策略 `keep_rule_allocation` ⇒ 用于筛选 L 的
分配就是规则分配本身。开启与不开启必须得到**同一个 state_hash**。这是"默认不变"的
证据，不是声明。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts.portfolio_shadow.cli import cmd_settle_session, manifest_from_dict
from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.portfolio_review import (filter_intents,
                                                       limits_from_manifest,
                                                       portfolio_ranks, subject_key,
                                                       UNAVAILABLE_LIMIT_REASONS)
from scripts.portfolio_shadow.schema import (Application, Manifest, Opportunity,
                                             to_micro)
from scripts.portfolio_shadow.store import ShadowStore, state_from_dict
from tests.unit.portfolio_shadow.test_position_cli import (EMPTY_ACTIONS, bars,
                                                           manifest_dict,
                                                           prices_frame)

SIGNAL, EXEC, NEXT = '2026-01-05', '2026-01-06', '2026-01-07'
CODE = 'SEC-A'


class Harness(unittest.TestCase):
    LLM = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / 'out'
        (self.out / 'EXP').mkdir(parents=True)
        self.manifest_path = self.out / 'EXP' / 'manifest.json'
        self.manifest_path.write_text(json.dumps(manifest_dict(**self.LLM)),
                                      encoding='utf-8')
        self.store = ShadowStore(self.out / 'EXP' / 'ledger.sqlite3', 'EXP')
        self.manifest = manifest_from_dict(json.loads(self.manifest_path.read_text()))
        self.store.save_experiment(self.manifest)
        self.opp = Opportunity(
            experiment_id='EXP', security_id=CODE, source_candidate_id=CODE,
            parent_version='1', signal_session=SIGNAL,
            observed_at='2026-01-04T00:00:00+00:00', planned_execution_session=NEXT,
            rank=1, entry_rule='b3', stop_reference={'atr14_micro': to_micro(2)},
            exit_policy_id='H60', input_hash='h')

    def tearDown(self):
        self.tmp.cleanup()

    def l_state(self):
        row = self.store.latest_state('SHADOW:EXP:L')
        return state_from_dict(row[1]) if row else None

    def seed_l_account(self):
        """L 账户先建一笔持仓，让后续 session 有状态可比。"""
        res = step(new_account_state('SHADOW:EXP:L', to_micro(100000)), session=EXEC,
                   bars=bars(100, 100.5), corporate_actions=[],
                   intents=[self.opp], manifest=self.manifest)
        self.store.save_state('SHADOW:EXP:L', res.state, res.nav, res.events, session=EXEC)
        return res.state

    def settle(self):
        prices = prices_frame(NEXT, 100.0, 100.5, 99.0, 101.0)
        with patch('scripts.portfolio_shadow.cli._market_data',
                   return_value=(prices, EMPTY_ACTIONS, None, None)):
            cmd_settle_session(type('A', (), {
                'manifest': str(self.manifest_path), 'output': str(self.out),
                'session': NEXT, 'etf_raw': None})())


class LimitsTests(Harness):
    def test_limits_come_from_the_frozen_manifest(self):
        state = new_account_state('SHADOW:EXP:L', to_micro(100000))
        limits, unavailable = limits_from_manifest(self.manifest, state=state)
        self.assertEqual(limits['max_positions'],
                         self.manifest.risk_policy['max_positions'])
        self.assertEqual(limits['max_name_risk_bp'],
                         self.manifest.risk_policy['single_position_risk_bp'])
        # 总风险预算**未声明**，这里是推导值 —— 报告里必须标明
        self.assertEqual(limits['max_total_risk_bp'],
                         self.manifest.risk_policy['max_positions']
                         * self.manifest.risk_policy['single_position_risk_bp'])

    def test_group_cap_is_reported_unavailable_not_silently_zero(self):
        """§6.3 要求风险组上限，而 manifest 没声明 —— 必须可见。

        若按 0 处理，任何有风险组的候选都会被拒，变成**静默空仓**。
        """
        state = new_account_state('SHADOW:EXP:L', to_micro(100000))
        limits, unavailable = limits_from_manifest(self.manifest, state=state)
        self.assertNotIn('max_group_risk_bp', limits)
        self.assertIn('max_group_risk_bp', unavailable)
        self.assertIn('未声明', UNAVAILABLE_LIMIT_REASONS['max_group_risk_bp'])


class CandidateTests(Harness):
    def test_candidates_come_from_due_opportunities(self):
        from scripts.portfolio_shadow.portfolio_review import candidates_for
        self.store.put_opportunity(self.opp)
        state = new_account_state('SHADOW:EXP:L', to_micro(100000))
        candidates = candidates_for(self.store, self.manifest,
                                    execution_session=NEXT,
                                    bars=bars(100, 100.5), state=state)
        self.assertEqual([c['security_id'] for c in candidates], [CODE])
        self.assertGreater(candidates[0]['estimated_cost_micro'], 0)

    def test_portfolio_rank_comes_from_the_frozen_counterfactual(self):
        """模型排序**读冻结记录**，不重算：重算会得到与当时不同的值。"""
        self.store.put_opportunity(self.opp)
        from scripts.live_trading.decision_ledger.event_store import (insert_event,
                                                                     make_event)
        with self.store.transaction() as con:
            insert_event(con, make_event(
                'SHADOW:EXP', 'selection_counterfactual_frozen', 'cf1',
                {'as_of': EXEC, 'ranked': [{'code': CODE, 'portfolio_rank': 3}]}))
        self.assertEqual(portfolio_ranks(self.store, EXEC), {CODE: 3})
        # 换一个 session 就取不到 —— 不跨期复用
        self.assertEqual(portfolio_ranks(self.store, '2099-01-01'), {})


class NoOpUnderShadowTests(Harness):
    """Portfolio 开启后行为**必须**不变 —— 这是最能证伪"默认不变"的断言。"""

    def run_case(self, llm_overrides, *, freeze=True):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            (out / 'EXP').mkdir(parents=True)
            manifest_path = out / 'EXP' / 'manifest.json'
            manifest_path.write_text(json.dumps(manifest_dict(**llm_overrides)),
                                     encoding='utf-8')
            store = ShadowStore(out / 'EXP' / 'ledger.sqlite3', 'EXP')
            manifest = manifest_from_dict(json.loads(manifest_path.read_text()))
            store.save_experiment(manifest)
            opp = Opportunity(
                experiment_id='EXP', security_id=CODE, source_candidate_id=CODE,
                parent_version='1', signal_session=SIGNAL,
                observed_at='2026-01-04T00:00:00+00:00',
                planned_execution_session=EXEC, rank=1, entry_rule='b3',
                stop_reference={'atr14_micro': to_micro(2)}, exit_policy_id='H60',
                input_hash='h')
            store.put_opportunity(opp)
            res = step(new_account_state('SHADOW:EXP:L', to_micro(100000)), session=EXEC,
                       bars=bars(100, 100.5), corporate_actions=[], intents=[],
                       manifest=manifest)
            store.save_state('SHADOW:EXP:L', res.state, res.nav, res.events, session=EXEC)
            if freeze:
                # 冻结一个 Portfolio 包与一条"采用规则分配"的动作（shadow 下的 effective）
                from scripts.portfolio_shadow.portfolio_review import prepare
                state = state_from_dict(store.latest_state('SHADOW:EXP:L')[1])
                prepare(store, manifest, session=EXEC, execution_session=NEXT,
                        bars=bars(100, 100.5), state=state)
                store.put_application(Application(
                    scope='SHADOW:EXP:L', opportunity_id=subject_key(NEXT),
                    action='keep_rule_allocation', reason_code='PORTFOLIO_ALLOCATION',
                    decision_id='d1', as_of='2026-01-06T22:00:00+00:00',
                    decision_frozen=True, execution_applied=False))
            prices = prices_frame(NEXT, 100.0, 100.5, 99.0, 101.0)
            with patch('scripts.portfolio_shadow.cli._market_data',
                       return_value=(prices, EMPTY_ACTIONS, None, None)):
                cmd_settle_session(type('A', (), {
                    'manifest': str(manifest_path), 'output': str(out),
                    'session': NEXT, 'etf_raw': None})())
            return state_from_dict(store.latest_state('SHADOW:EXP:L')[1]).state_hash()

    def test_enabling_changes_nothing_under_shadow(self):
        off = self.run_case({})
        on = self.run_case({'portfolio_review': 'portfolio_action'})
        self.assertEqual(off, on,
                         'Portfolio 权限为 shadow 时，开启与不开启必须得到同一状态')

    def test_a_frozen_allocation_actually_filters(self):
        """反证：若冻结的分配**真的**不含某候选，它就会被剔除 —— 说明筛选接上了。

        没有这条，上面的"开启后不变"可能只是"筛选根本没接上"。
        这里自建包：`put_packet` 是**首次写入即冻结**，所以不能靠覆盖 `prepare` 的产物。
        """
        from scripts.live_trading.decision_bridge import build_portfolio_packet
        key = subject_key(NEXT)
        packet = build_portfolio_packet(
            candidates=[{'security_id': CODE, 'rank': 1, 'risk_bp': 100,
                         'risk_group': '', 'estimated_cost_micro': 1000}],
            positions=[], limits={'max_positions': 5, 'max_total_risk_bp': 500,
                                  'max_name_risk_bp': 100,
                                  'cash_available_micro': 100000},
            new_evidence=[], account_scope='SHADOW:EXP:L', subject_id='pf',
            as_of='2026-01-06T22:00:00+00:00')
        packet['templates'] = [{'template_id': 'hold_cash_buffer',
                                'action': 'hold_cash_buffer', 'allocations': [],
                                'total_risk_bp': 0, 'cash_used_micro': 0,
                                'cash_left_micro': 100000, 'group_risk_bp': {},
                                'rejected': []}]
        self.store.put_packet(key, packet)
        self.store.put_application(Application(
            scope='SHADOW:EXP:L', opportunity_id=key, action='hold_cash_buffer',
            reason_code='X', decision_id='d2', as_of='2026-01-06T22:00:00+00:00',
            decision_frozen=True, execution_applied=False))
        self.store.put_opportunity(self.opp)
        kept, dropped = filter_intents(self.store, 'SHADOW:EXP:L', NEXT, [self.opp])
        self.assertEqual(kept, [])
        self.assertEqual([o.security_id for o in dropped], [CODE])

    def test_without_a_frozen_allocation_intents_pass_through(self):
        """没有冻结分配（未启用/无冲突/窗口错过）时原样放行 —— 那不该表现成"模型少买"。"""
        opp = Opportunity(
            experiment_id='EXP', security_id=CODE, source_candidate_id=CODE,
            parent_version='1', signal_session=SIGNAL,
            observed_at='2026-01-04T00:00:00+00:00', planned_execution_session=NEXT,
            rank=1, entry_rule='b3', stop_reference={'atr14_micro': to_micro(2)},
            exit_policy_id='H60', input_hash='h')
        kept, dropped = filter_intents(self.store, 'SHADOW:EXP:L', NEXT, [opp])
        self.assertEqual(kept, [opp])
        self.assertEqual(dropped, [])


if __name__ == '__main__':
    unittest.main()


class PositionExposureRegressionTests(unittest.TestCase):
    def test_held_risk_reaches_the_template_builder(self):
        from scripts.portfolio_shadow.schema import Position
        from scripts.portfolio_shadow.portfolio_review import position_exposure
        from mutifactor.llm.contracts.portfolio_v1 import build_portfolio_templates
        state = new_account_state('SHADOW:EXP:L', to_micro(10000))
        state.cash_available = to_micro(9000)
        state.positions['A'] = Position('A', 10, to_micro(100), EXEC,
                                        to_micro(90), to_micro(90), 'H60', 'opp')
        exposure = position_exposure(state, {'A': {'close': to_micro(100)}})
        self.assertEqual(exposure[0]['risk_bp'], 100)
        result = build_portfolio_templates(
            candidates=[{'security_id': 'B', 'rank': 1, 'risk_bp': 100,
                         'estimated_cost_micro': 1000}], positions=exposure,
            limits={'max_positions': 5, 'max_total_risk_bp': 150,
                    'max_name_risk_bp': 100, 'cash_available_micro': state.cash_available})
        self.assertEqual(result['templates'][0]['allocations'], [])
        self.assertEqual(result['templates'][0]['rejected'][0]['reason'], 'TOTAL_RISK_BUDGET')

    def test_fractional_basis_point_risk_rounds_up(self):
        from scripts.portfolio_shadow.schema import Position
        from scripts.portfolio_shadow.portfolio_review import position_exposure
        state = new_account_state('SHADOW:EXP:L', to_micro(10000))
        state.positions['A'] = Position('A', 1, to_micro(100), EXEC,
                                        to_micro(99.999999), to_micro(99.999999), 'H60', 'opp')
        self.assertEqual(position_exposure(state, {'A': {'close': to_micro(100)}})[0]['risk_bp'], 1)
