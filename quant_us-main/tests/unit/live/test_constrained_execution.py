"""constrained_action 自动买入（PR8 / T5）DRY-RUN 测试。"""
import tempfile
import unittest
from pathlib import Path

from scripts.live_trading.execution import ExecutionService
from scripts.live_trading.decision_bridge import build_entry_packet
from scripts.live_trading.decision_engine import DecisionEngine
from scripts.live_trading.decision_ledger.event_store import utc
from scripts.live_trading.position_registry import PositionRegistry

CFG = {'risk_budget': {'per_trade': .0025, 'total': .015,
                       'group_limits': {'semis': .0075},
                       'code_groups': {'US.A': 'semis'},
                       'dry_run_equity': 100000, 'cost_per_share': .05},
       'llm_permissions': {'_default': 'shadow',
                           'entry_review': 'constrained_action',
                           'plan_template': 'constrained_action',
                           'position_scale': 'constrained_action'}}


class _Advisor:
    model = 'test-model'
    last_metadata = {}

    def chat(self, prompt, system=None):
        return {'status': 'insufficient_information', 'action': 'execute_now',
                'template_id': 'p1:standard', 'confidence': 'medium',
                'reason_codes': [], 'facts': [], 'inferences': [],
                'counterevidence': [], 'missing_information': ['测试输入'],
                'selected_review_trigger_ids': []}


class ConstrainedEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 'state.db', 'DRY-RUN')
        self.execution = ExecutionService(None, CFG, None, True, self.registry)

    def _decision(self, subject_id, qty):
        plan = {'plan_id': 'p1', 'stock_code': 'US.A',
                'risk': {'initial_stop': 90.0}, 'entry_constraints': {'price': 100.0}}
        packet = build_entry_packet(
            signal={'signal_id': subject_id}, plan=plan, evidence=[],
            account_scope='DRY-RUN', subject_id=subject_id, as_of=utc(),
            standard_quantity=qty, entry_price=100.0, initial_stop=90.0,
            model={'provider': 'test', 'model_id': 'test-model',
                   'temperature': 0.0, 'timeout_seconds': 30})
        return DecisionEngine(self.registry, advisor=_Advisor(), config=CFG).decide_entry(packet)

    def _submit(self, entry_id, qty):
        decision = self._decision(entry_id, qty)
        return self.execution.submit_constrained_entry(
            decision_id=decision.decision_id, template_id='p1:standard',
            risk_group='semis')

    def test_dry_run_fills_and_creates_position(self):
        status = self._submit('e1', qty=10)
        self.assertEqual(status, 'filled')
        pos = self.registry.get('US.A')
        self.assertIsNotNone(pos)
        self.assertEqual(pos['qty'], 10)

    def test_idempotent_same_entry(self):
        decision = self._decision('e2', 10)
        self.execution.submit_constrained_entry(
            decision_id=decision.decision_id, template_id='p1:standard')
        status = self.execution.submit_constrained_entry(
            decision_id=decision.decision_id, template_id='p1:standard')
        self.assertEqual(status, 'filled')

    def test_quantity_above_risk_cap_rejected(self):
        # 风险上限约 24 股；30 股应被拒绝
        with self.assertRaisesRegex(ValueError, '超过风险上限'):
            self._submit('e3', qty=30)

    def test_duplicate_position_rejected(self):
        self._submit('e4', qty=10)
        with self.assertRaisesRegex(ValueError, '禁止重复买入'):
            self._submit('e5', qty=5)

    def test_caller_cannot_supply_unbound_order_parameters(self):
        decision = self._decision('e6', 10)
        with self.assertRaises(TypeError):
            self.execution.submit_constrained_entry(
                decision_id=decision.decision_id, template_id='p1:standard', qty=999)


if __name__ == '__main__':
    unittest.main()
