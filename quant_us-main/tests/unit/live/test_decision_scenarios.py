"""DRY-RUN 集成场景（§20.6）：六个端到端场景的决策逻辑部分。

用假 advisor + 内存 registry，跑通 决策引擎 + 影子桥 + defer 状态机 + outcome + 硬退出，
无券商依赖。真实成交链路（futu）不在本测试范围。
"""
import tempfile
import time
import unittest
from pathlib import Path

from scripts.live_trading.decision_bridge import ShadowBridge
from scripts.live_trading.decision_ledger.event_store import utc
from scripts.live_trading.decision_ledger.evidence_packet import build_evidence_packet
from scripts.live_trading.defer_state import DeferStore
from scripts.live_trading.execution import ExecutionService
from scripts.live_trading.hard_exit_router import HardExitRouter
from scripts.live_trading.position_registry import PositionRegistry

CFG = {'risk_budget': {'per_trade': .0025, 'total': .015,
                       'group_limits': {'semis': .0075},
                       'code_groups': {'US.A': 'semis'},
                       'dry_run_equity': 100000, 'cost_per_share': .05},
       'llm_permissions': {'_default': 'shadow'}}


class _Advisor:
    model = 'test-model'
    last_metadata = {}

    def __init__(self, raw=None):
        self.raw = raw

    def chat(self, prompt, system=None):
        return self.raw


def _sel_raw(eid, decision='candidate'):
    return {'status': 'complete', 'market_view': {'risk_posture': 'normal'},
            'ranked': [{'code': 'US.A', 'standalone_rank': 1, 'portfolio_rank': 1,
                        'decision': decision, 'confidence': 'medium', 'horizon': '1_5d',
                        'setup_type': 'none', 'reason_codes': [],
                        'thesis': [{'text': '财报超预期', 'claim_type': 'inference',
                                    'evidence_ids': [eid]}],
                        'counterevidence': [], 'invalidation_conditions': [],
                        'option_view_effect': 'unavailable'}],
            'abstain_reason_codes': []}


def _entry_raw(action='execute_now', template='p1:standard'):
    return {'status': 'complete', 'action': action, 'template_id': template,
            'confidence': 'high', 'reason_codes': ['OPTIONS_CONFIRM'],
            'facts': [], 'inferences': [], 'counterevidence': [],
            'missing_information': ['资料不足'] if action == 'defer' else [],
            'selected_review_trigger_ids': []}


class DecisionScenarios(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 'state.db', 'DRY-RUN')
        self.execution = ExecutionService(None, CFG, None, True, self.registry)
        self.router = HardExitRouter(registry=self.registry, execution=self.execution)
        self.defer = DeferStore(self.registry)

    def _packet(self):
        return build_evidence_packet(
            'US.A', quote={'price': 100.0, 'observed_at': time.time() - 60},
            events=[{'summary': '财报超预期', 'source': 'internal:test',
                     'published_at': time.time() - 3600, 'observed_at': time.time() - 3600,
                     'kind': 'fundamental', 'subject_code': 'US.A'}],
            now=time.time() - 60)

    def test_1_candidate_entry_confirmed_fill(self):
        """选股 candidate → 规则信号 → LLM 选 standard 模板 → 成交（影子下规则基线 + 受约束买入）。"""
        pkt = self._packet()
        eid = pkt['events'][0]['evidence_id']
        bridge = ShadowBridge(self.registry, _Advisor(_sel_raw(eid, 'candidate')))
        sel = bridge.run_selection(batch_id='b1', account_scope='DRY-RUN',
                                   discovery_codes=['US.A'], packets=[pkt], as_of=utc())
        self.assertEqual(sel.status, 'validated')
        self.assertEqual(sel.effective_action, 'rule_ranking')  # shadow
        # 信号 → 生成获得 constrained_action 的正式 Entry Decision，再按冻结模板买入。
        from scripts.live_trading.decision_bridge import build_entry_packet
        from scripts.live_trading.decision_engine import DecisionEngine
        entry_cfg = dict(CFG)
        entry_cfg['llm_permissions'] = {
            '_default': 'shadow', 'entry_review': 'constrained_action',
            'plan_template': 'constrained_action', 'position_scale': 'constrained_action'}
        plan = {'plan_id': 'p1', 'stock_code': 'US.A',
                'risk': {'initial_stop': 90.0}, 'entry_constraints': {'price': 100.0}}
        entry_raw = {'status': 'insufficient_information', 'action': 'execute_now',
                     'template_id': 'p1:standard', 'confidence': 'medium',
                     'reason_codes': [], 'facts': [], 'inferences': [],
                     'counterevidence': [], 'missing_information': ['集成测试'],
                     'selected_review_trigger_ids': []}
        entry_packet = build_entry_packet(
            signal={'signal_id': 'sig1'}, plan=plan, evidence=[],
            account_scope='DRY-RUN', subject_id='sig1', as_of=utc(),
            standard_quantity=10, entry_price=100.0, initial_stop=90.0,
            model={'provider': 'test', 'model_id': 'test-model',
                   'temperature': 0.0, 'timeout_seconds': 30})
        entry_decision = DecisionEngine(
            self.registry, advisor=_Advisor(entry_raw), config=entry_cfg).decide_entry(entry_packet)
        constrained_execution = ExecutionService(None, entry_cfg, None, True, self.registry)
        status = constrained_execution.submit_constrained_entry(
            decision_id=entry_decision.decision_id, template_id='p1:standard',
            risk_group='semis')
        self.assertEqual(status, 'filled')
        self.assertEqual(self.registry.get('US.A')['qty'], 10)

    def test_2_watch_defer_then_resolve(self):
        """选股 watch → 入场 defer → trigger 命中 → resolve。"""
        pkt = self._packet()
        eid = pkt['events'][0]['evidence_id']
        bridge = ShadowBridge(self.registry, _Advisor(_sel_raw(eid, 'watch')))
        sel = bridge.run_selection(batch_id='b2', account_scope='DRY-RUN',
                                   discovery_codes=['US.A'], packets=[pkt], as_of=utc())
        self.assertEqual(sel.status, 'validated')
        rec = self.defer.create(signal_id='sig2', decision_id=sel.decision_id,
                                triggers=[{'trigger_id': 't1', 'type': 'price_above',
                                           'params': {'price': 101.0}}],
                                expire_at=time.time() + 3600)
        self.assertEqual(self.defer.evaluate(rec['defer_id'], {'price': 102.0}), ['t1'])
        self.assertEqual(self.defer.trigger(rec['defer_id'], 't1')['status'], 'queued')
        self.assertEqual(self.defer.resolve(rec['defer_id'])['status'], 'resolved')

    def test_3_thesis_weakening_reduce_shadow(self):
        """买入后论文 weakening → 建议 reduce；shadow 下有效动作 = hold。"""
        from scripts.live_trading.decision_bridge import build_position_packet
        from scripts.live_trading.decision_engine import DecisionEngine
        raw = {'status': 'insufficient_information', 'thesis_state': 'WEAKENING',
               'action': 'reduce', 'action_template_id': 't1:reduce:50', 'confidence': 'medium',
               'reason_codes': ['THESIS_WEAKENED'],
               'facts': [], 'inferences': [], 'counterevidence': [],
               'missing_information': ['资料不足']}
        trade = {'trade_id': 't1', 'code': 'US.A', 'direction': 'long',
                 'remaining_qty': 100.0, 'entry_price': 100.0}
        packet = build_position_packet(trade=trade, protection={'active_stop': 90.0},
                                       new_evidence=[], account_scope='DRY-RUN',
                                       subject_id='t1', as_of=utc())
        engine = DecisionEngine(self.registry, advisor=_Advisor(raw), config=CFG)
        result = engine.decide_position(packet)
        self.assertEqual(result.status, 'failed')
        self.assertTrue(any('必须引用至少一条证据' in e
                            for e in result.validation_errors))

    def test_4_bullish_but_hard_stop_exits(self):
        """LLM 看多但硬止损触发 → 立即退出（不依赖 LLM）。"""
        self.registry.open('US.A', 'dip_buy', 100, 100.0, initial_stop=90.0, risk_group='semis')
        r = self.router.submit(trade_id='t4', code='US.A', reason='fixed_stop',
                               market_price=80.0, dry_run=True)
        self.assertEqual(r['status'], 'filled')
        self.assertIsNone(self.registry.get('US.A'))  # 持仓已清

    def test_5_option_missing_unavailable(self):
        """期权数据缺失 → 股票研究继续、期权结论 unavailable。"""
        pkt = self._packet()
        eid = pkt['events'][0]['evidence_id']
        bridge = ShadowBridge(self.registry, _Advisor(_sel_raw(eid)))
        sel = bridge.run_selection(batch_id='b5', account_scope='DRY-RUN',
                                   discovery_codes=['US.A'], packets=[pkt], as_of=utc())
        self.assertEqual(sel.status, 'validated')
        self.assertEqual(sel.validated_output['ranked'][0]['option_view_effect'], 'unavailable')

    def test_6_model_unavailable_blocks_entry_but_not_hard_exit(self):
        """模型不可用 → 新买入停止；已有硬保护继续。"""
        from scripts.live_trading.decision_bridge import build_entry_packet
        from scripts.live_trading.decision_engine import DecisionEngine
        plan = {'plan_id': 'p1', 'stock_code': 'US.A',
                'risk': {'initial_stop': 90.0}, 'entry_constraints': {'price': 100.0}}
        packet = build_entry_packet(signal={'signal_id': 's6'}, plan=plan, evidence=[],
                                    account_scope='DRY-RUN', subject_id='s6', as_of=utc(),
                                    standard_quantity=100, entry_price=100.0, initial_stop=90.0)
        engine = DecisionEngine(self.registry, advisor=_Advisor(None), config=CFG)
        result = engine.decide_entry(packet)
        self.assertEqual(result.status, 'failed')
        self.assertIsNone(result.effective_action)
        # 已有硬保护仍可退出
        self.registry.open('US.B', 'dip_buy', 50, 80.0, initial_stop=72.0, risk_group='semis')
        r = self.router.submit(trade_id='t6', code='US.B', reason='trailing_stop',
                               market_price=70.0, dry_run=True)
        self.assertEqual(r['status'], 'filled')


if __name__ == '__main__':
    unittest.main()
