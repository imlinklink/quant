"""实盘层 Portfolio 调用方（设计 §6.3）。

三条要钉死的东西：
1. **候选口径**：在途**买入**提案（卖单共用同一个 store，必须排除）；
2. **限额映射**：`risk_budget` → §6.3 口径，组上限是**按组**的字典；
3. **接通后实盘行为一字不变** —— 咨询不改任何提案/订单，生效动作恒为父策略。
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.live_trading.portfolio_allocation import (active_buy_proposals,
                                                       candidates_from_proposals,
                                                       consult, limits_from_config,
                                                       occupied_slots)

CONFIG = {'risk_budget': {
    'max_positions': 3, 'per_trade': 0.0025, 'total': 0.015, 'dry_run_equity': 100000,
    'group_limits': {'semis': 0.0075, 'china': 0.005},
    'code_groups': {'US.A': 'semis', 'US.B': 'semis', 'US.C': 'china', 'US.D': 'china'}}}


class FakeStore:
    def __init__(self, items):
        self._items = items

    def get_all(self):
        return [dict(i) for i in self._items]


def prop(pid, code, *, side='buy', status='pending', qty=10, price=100.0, created_at=0,
         evidence=()):
    return {'id': pid, 'stock_code': code, 'side': side, 'status': status,
            'quantity': qty, 'price': price, 'created_at': created_at,
            'plan_id': 'pl1', 'expires_at': 9e9, 'evidence_items': list(evidence)}


def news(summary, code):
    """提案携带的真实证据形状（`workflow.news_evidence` 的产物）。"""
    from mutifactor.llm.trade_review import evidence
    item = evidence(summary, 'https://example.invalid/n', '2026-09-18T12:00:00+00:00',
                    '2026-09-18T11:00:00+00:00', cluster_id='c1', kind='news')
    item['subject_code'] = code
    return item


def portfolio_output(packet, template='keep_rule_allocation'):
    """夹具输出。选 `keep_rule_allocation` 以外的模板时**必须引用包内证据** ——
    校验器要求改变分配的动作至少有依据（§7.2），否则整条判 failed。"""
    cited = ([{'text': e['summary'], 'claim_type': 'fact', 'evidence_ids': [e['evidence_id']]}
              for e in packet.get('new_evidence') or []][:1]
             if template != 'keep_rule_allocation' else [])
    return {'schema_version': 'portfolio-v1', 'packet_id': packet['packet_id'],
            'status': 'complete', 'chosen_template_id': template, 'reason_codes': [],
            'facts': cited, 'inferences': [], 'counterevidence': [],
            'missing_information': ['夹具']}


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from scripts.live_trading.position_registry import PositionRegistry
        self.path = Path(self.tmp.name) / 'live.sqlite3'
        self.registry = PositionRegistry(self.path, 'DRY-RUN')

    def seed_book(self, positions=None, orders=None):
        with self.registry.transaction() as book:
            book['positions'].update(positions or {})
            book['orders'].update(orders or {})

    def events(self, event_type):
        con = sqlite3.connect(str(self.path))
        try:
            return con.execute('SELECT COUNT(*) FROM decision_events WHERE event_type=?',
                               (event_type,)).fetchone()[0]
        finally:
            con.close()


class CandidateTests(unittest.TestCase):
    def test_sell_proposals_are_not_candidates(self):
        """卖单与买单**共用同一个 store**，而卖单释放容量、不是竞争者。"""
        store = FakeStore([prop('p1', 'US.A'), prop('p2', 'US.B', side='sell')])
        self.assertEqual([p['id'] for p in active_buy_proposals(store)], ['p1'])

    def test_terminal_proposals_are_not_candidates(self):
        store = FakeStore([prop('p1', 'US.A'), prop('p2', 'US.B', status='executed')])
        self.assertEqual([p['id'] for p in active_buy_proposals(store)], ['p1'])

    def test_candidates_rank_by_created_at(self):
        store = FakeStore([prop('p2', 'US.B', created_at=200),
                           prop('p1', 'US.A', created_at=100)])
        cands = candidates_from_proposals(
            active_buy_proposals(store),
            code_groups=CONFIG['risk_budget']['code_groups'], per_trade_bp=25)
        self.assertEqual([(c['security_id'], c['rank']) for c in cands],
                         [('US.A', 1), ('US.B', 2)])
        self.assertEqual(cands[0]['risk_group'], 'semis')
        self.assertEqual(cands[0]['estimated_cost_micro'], 10 * 100.0 * 1_000_000)


class LimitTests(unittest.TestCase):
    def test_limits_map_config_to_bp(self):
        limits, unavailable = limits_from_config(CONFIG, cash=50000)
        self.assertEqual(limits['max_positions'], 3)
        self.assertEqual(limits['max_name_risk_bp'], 25)        # per_trade 0.0025
        self.assertEqual(limits['max_total_risk_bp'], 150)      # total 0.015
        self.assertEqual(limits['cash_available_micro'], 50_000_000_000)
        # **按组**，不是标量 —— 实盘层各组上限不同
        self.assertEqual(limits['max_group_risk_bp'], {'semis': 75, 'china': 50})
        self.assertEqual(unavailable, {})

    def test_absent_group_limits_are_disclosed_not_treated_as_zero(self):
        """未声明组上限要**如实披露**。按 0 处理会让任何有风险组的候选被静默拒掉。"""
        cfg = {'risk_budget': {k: v for k, v in CONFIG['risk_budget'].items()
                               if k != 'group_limits'}}
        limits, unavailable = limits_from_config(cfg, cash=1000)
        self.assertNotIn('max_group_risk_bp', limits)
        self.assertIn('max_group_risk_bp', unavailable)


class OccupiedSlotTests(unittest.TestCase):
    def test_holdings_and_inflight_buys_each_take_a_slot(self):
        book = {'positions': {'US.A': {'qty': 5, 'initial_risk': 40, 'risk_group': 'semis'}},
                'orders': {'o1': {'code': 'US.B', 'side': 'buy', 'status': 'submitted'},
                           'o2': {'code': 'US.C', 'side': 'sell', 'status': 'submitted'},
                           'o3': {'code': 'US.D', 'side': 'buy', 'status': 'filled'}}}
        slots, estimated = occupied_slots(book, code_groups={}, per_trade_bp=25)
        codes = [s['security_id'] for s in slots]
        self.assertEqual(codes, ['US.A', 'US.B'])      # 卖单与已成交单都不占格
        self.assertEqual(slots[0]['risk_bp'], 40)
        self.assertEqual(estimated, 1)                 # US.B 没有 initial_risk ⇒ 估计
        self.assertTrue(slots[1].get('risk_estimated'))

    def test_an_inflight_buy_never_double_counts_a_held_code(self):
        book = {'positions': {'US.A': {'qty': 5, 'initial_risk': 40}},
                'orders': {'o1': {'code': 'US.A', 'side': 'buy', 'status': 'submitted'}}}
        slots, _ = occupied_slots(book, code_groups={}, per_trade_bp=25)
        self.assertEqual([s['security_id'] for s in slots], ['US.A'])


class ConsultTests(Harness):
    def _consult(self, proposals, *, config=None, call_model=None):
        calls = []

        def model(contract, packet):
            calls.append(packet['packet_id'])
            return (call_model or portfolio_output)(packet)

        summary = consult(self.registry, FakeStore(proposals), config or CONFIG,
                          equity=100000, cash=100000, advisor=object(),
                          call_model=model, as_of='2026-09-19T20:00:00+00:00',
                          session='2026-09-19')
        return summary, calls

    def test_no_capacity_conflict_does_not_call_the_model_or_claim(self):
        """§6.3：没有容量冲突不调用 —— 且**不消耗当天认领**（冲突晚些出现还能再评）。"""
        summary, calls = self._consult([prop('p1', 'US.A')])
        self.assertFalse(summary['consult_required'])
        self.assertFalse(summary['called'])
        self.assertEqual(summary['skipped'], 'NO_CAPACITY_CONFLICT')
        self.assertEqual(calls, [])
        self.assertEqual(self.events('daily_job_claimed'), 0)

    def test_capacity_conflict_calls_the_model_and_records_a_run(self):
        # max_positions=3 ⇒ 4 个候选必然有一个被 MAX_POSITIONS 挡下
        summary, calls = self._consult([prop(f'p{i}', c) for i, c in
                                        enumerate(['US.A', 'US.B', 'US.C', 'US.D'])])
        self.assertTrue(summary['consult_required'])
        self.assertTrue(summary['called'])
        self.assertEqual(summary['status'], 'validated')
        self.assertEqual(len(calls), 1)
        con = sqlite3.connect(str(self.path))
        try:
            rows = con.execute("SELECT role, status FROM llm_decision_runs "
                               "WHERE role='portfolio'").fetchall()
        finally:
            con.close()
        self.assertEqual(rows, [('portfolio', 'validated')])

    def test_shadow_permission_means_the_rule_allocation_always_wins(self):
        """接通后实盘行为一字不变：生效动作恒为父策略 `keep_rule_allocation`。"""
        proposals = [prop(f'p{i}', c, evidence=[news(f'{c} 有新闻', c)])
                     for i, c in enumerate(['US.A', 'US.B', 'US.C', 'US.D'])]
        summary, _ = self._consult(
            proposals, call_model=lambda packet: portfolio_output(packet, 'hold_cash_buffer'))
        self.assertEqual(summary['status'], 'validated', summary.get('validation_errors'))
        self.assertEqual(summary['model_action'], 'hold_cash_buffer')      # 模型确实选了别的
        self.assertEqual(summary['effective_action'], 'keep_rule_allocation')
        self.assertEqual(summary['permission_level'], 'shadow')

    def test_a_change_without_evidence_fails_validation(self):
        """包内无证据时，改变分配的模板**必然**判 failed —— 这正是为什么要把提案携带的
        证据并进包：否则 portfolio 的 `output_validity` 是 0 而不是 1。"""
        summary, _ = self._consult(
            [prop(f'p{i}', c) for i, c in enumerate(['US.A', 'US.B', 'US.C', 'US.D'])],
            call_model=lambda packet: portfolio_output(packet, 'hold_cash_buffer'))
        self.assertEqual(summary['status'], 'failed')
        self.assertTrue(any('比较依据' in e for e in summary['validation_errors']))

    def test_evidence_from_proposals_reaches_the_packet(self):
        proposals = [prop('p1', 'US.A', evidence=[news('A 的消息', 'US.A')]),
                     prop('p2', 'US.B', evidence=[news('B 的消息', 'US.B')])]
        summary, calls = self._consult(proposals)
        self.assertEqual(summary['evidence'], 2)

    def test_consulting_mutates_nothing(self):
        """咨询**不改任何提案状态、不下单** —— 这是"接通后行为不变"的实质证据。"""
        proposals = [prop(f'p{i}', c) for i, c in enumerate(['US.A', 'US.B', 'US.C', 'US.D'])]
        before = [dict(p) for p in proposals]
        store = FakeStore(proposals)
        consult(self.registry, store, CONFIG, equity=100000, cash=100000,
                advisor=object(), call_model=lambda c, p: portfolio_output(p),
                as_of='2026-09-19T20:00:00+00:00', session='2026-09-19')
        self.assertEqual(store.get_all(), before)
        with self.registry.transaction() as book:
            self.assertEqual(book['positions'], {})
            self.assertEqual(book['orders'], {})

    def test_only_one_paid_consultation_per_day(self):
        """同账户每天最多一次付费调用：第二次同日的冲突**不再调模型**。"""
        proposals = [prop(f'p{i}', c) for i, c in enumerate(['US.A', 'US.B', 'US.C', 'US.D'])]
        first, calls1 = self._consult(proposals)
        second, calls2 = self._consult(proposals)
        self.assertTrue(first['called'])
        self.assertEqual(calls1 and len(calls1), 1)
        self.assertFalse(second['called'])
        self.assertEqual(second['skipped'], 'ALREADY_CONSULTED_TODAY')
        self.assertEqual(calls2, [])
