"""容量分配：在途提案必须参与容量判定，超容量的按规则序走一条显式分配路径。

背景（2026-09-19 实测）：实盘层此前**没有"分配"这件事** —— `risk_quantity` 与 `submit` 的
`occupied` 都只认 `book['orders']`，而 `pending` 提案**不是订单** ⇒ 并发的待审提案互相看不见、
各自按"容量全空"定仓，第 N 个要到提交时才被拒，**谁赢取决于轮询顺序**。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.live_trading.approval.proposal_store import ProposalStore
from scripts.live_trading.execution import (ExecutionService, committed_slots,
                                            proposal_reservations, reconcile_capacity,
                                            reserved_slots, risk_quantity)
from scripts.live_trading.position_registry import PositionRegistry

CFG = {'risk_budget': {'per_trade': .0025, 'total': .015,
                       'group_limits': {'semis': .0075, 'china': .005},
                       'code_groups': {'US.A': 'semis', 'US.B': 'semis', 'US.C': 'china',
                                       'US.D': 'china'},
                       'dry_run_equity': 100000, 'max_positions': 3}}
REVIEW = {'verdict': 'allow', 'reason': '测试评估已完成'}
FREE = ([], [])          # 空持仓、空订单
SEMIS = CFG['risk_budget']


class Owner:
    def __init__(self, config, store):
        self.config = config
        self.approval_store = store
        self.dry_run = True
        self.pool = None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(ProposalStore, '_record_ledger')
        p.start()
        self.addCleanup(p.stop)
        # **同一个 registry**：提案与账本必须落在同一份库上，否则 `reserved_slots` 读到的
        # book 和提案不是同源。namespace 留 `unconfigured`，好让服务把它 configure 成 DRY-RUN
        # （`ProposalStore(log_dir=...)` 会造一个 namespace='test' 的 registry，那个配不了）。
        self.registry = PositionRegistry(Path(self.tmp.name) / 'state.db')
        self.store = ProposalStore(registry=self.registry)
        self.owner = Owner(CFG, self.store)
        self.service = ExecutionService(None, CFG, self.store, True, self.registry)

    def prop(self, code, *, side='buy', created_at=None, llm=REVIEW, review=False,
             risk_summary=None, trade_plan=None):
        item = self.store.create(stock_code=code, side=side, price=100, quantity=50, llm=llm,
                                 entry_mode='donchian',
                                 trade_plan=trade_plan or {'initial_stop': 95},
                                 risk_summary=risk_summary or {})
        if created_at is not None:
            with self.store._lock:
                self.store._items[item['id']]['created_at'] = created_at
        if review:
            # 直接置位而不是 `begin_review()`：后者对**没有 `plan_id`** 的旧式提案直接返回 None
            # （走不到评审流程），用它会让这条测试假装在测"评审在飞"、实际什么都没设。
            with self.store._lock:
                self.store._items[item['id']]['review_requested_at'] = 1.0
        return self.store.get(item['id'])


class ReservationShapeTests(Base):
    def test_uses_the_proposals_own_risk_summary(self):
        p = self.prop('US.A', risk_summary={'budget_risk': 321.0, 'risk_group': 'semis'})
        r = proposal_reservations([p], code_groups=CFG['risk_budget']['code_groups'])[0]
        self.assertEqual(r['risk'], 321.0)
        self.assertEqual(r['risk_group'], 'semis')
        self.assertEqual((r['qty'], r['price']), (50.0, 100.0))

    def test_falls_back_to_the_stop_based_risk(self):
        """没有 risk_summary 时按 `qty × (price − stop)` 现算，而不是当成 0。"""
        p = self.prop('US.A', risk_summary={}, trade_plan={'initial_stop': 90})
        r = proposal_reservations([p], code_groups=CFG['risk_budget']['code_groups'])[0]
        self.assertEqual(r['risk'], 50 * (100 - 90))
        self.assertEqual(r['risk_group'], 'semis')      # 取自 code_groups

    def test_last_resort_estimates_from_equity_not_zero(self):
        """连止损都没有时按 `equity × per_trade` 估。**按 0 会让预算看起来没被占用** ——
        那正是这个缺陷本身。"""
        p = self.prop('US.A', risk_summary={}, trade_plan={})
        r = proposal_reservations([p], code_groups={}, equity=100000)[0]
        self.assertEqual(r['risk'], 250.0)


class RiskQuantityTests(unittest.TestCase):
    def test_default_is_unchanged(self):
        """`proposals` 默认空 ⇒ 既有调用与既有数字一字不变。"""
        qty, _ = risk_quantity(100, 95, 100000, 100000, *FREE, SEMIS, 'semis', 5000)
        self.assertEqual(qty, 49)

    def test_an_inflight_proposal_consumes_the_group_budget(self):
        base, _ = risk_quantity(100, 95, 100000, 100000, *FREE, SEMIS, 'semis', 5000)
        shrunk, _ = risk_quantity(
            100, 95, 100000, 100000, *FREE, SEMIS, 'semis', 5000,
            proposals=[{'qty': 50, 'price': 100, 'risk': 600, 'risk_group': 'semis'}])
        self.assertEqual(base, 49)
        self.assertEqual(shrunk, 29)          # 组预算 750 − 600 = 150 ⇒ 150/5.05

    def test_a_proposal_in_another_group_does_not_eat_this_groups_budget(self):
        qty, _ = risk_quantity(
            100, 95, 100000, 100000, *FREE, SEMIS, 'semis', 5000,
            proposals=[{'qty': 50, 'price': 100, 'risk': 600, 'risk_group': 'china'}])
        self.assertEqual(qty, 49)             # 只吃总额，本组仍由 per_trade=250 兜住

    def test_an_inflight_proposal_can_exhaust_the_budget_entirely(self):
        with self.assertRaises(ValueError):
            risk_quantity(100, 95, 100000, 100000, *FREE, SEMIS, 'semis', 5000,
                          proposals=[{'qty': 50, 'price': 100, 'risk': 750,
                                      'risk_group': 'semis'}])


class ReservedSlotTests(Base):
    def test_slots_cover_proposals_but_not_sells(self):
        """卖单共用同一个 store，但**释放**容量、不是竞争者。"""
        self.prop('US.A')
        self.prop('US.B')
        self.prop('US.C', side='sell')
        active = self.store.active_buys()        # 事务外取（进事务再取会死锁）
        with self.service.registry.transaction() as book:
            self.assertEqual(reserved_slots(book, active), {'US.A', 'US.B'})

    def test_committed_slots_ignore_proposals(self):
        """`committed_slots` 刻意**不含**提案：人工批准的决定不该被未批准的兄弟挡住。"""
        self.prop('US.A')
        self.assertEqual(committed_slots(self.service, 'US.B'), 0)


class RuleOrderSubmitTests(Base):
    """谁赢由**规则序**决定，不由提交/轮询顺序决定。"""

    def setUp(self):
        super().setUp()
        cfg = {'risk_budget': dict(CFG['risk_budget'], max_positions=1)}
        self.service = ExecutionService(None, cfg, self.store, True, self.registry)

    def _executing(self, code, created_at):
        p = self.prop(code, created_at=created_at)
        self.assertTrue(self.store.approve(p['id']))
        self.assertTrue(self.store.mark(p['id'], 'executing'))
        return p['id']

    def test_the_later_candidate_cannot_win_by_submitting_first(self):
        a = self._executing('US.A', 1)
        b = self._executing('US.B', 2)
        with self.assertRaises(ValueError) as ctx:
            self.service.submit(self.store.get(b), 100)
        self.assertIn('组合数量上限', str(ctx.exception))
        # A 在规则序前面 ⇒ 它拿得到那唯一一个位置。
        self.assertEqual(self.service.submit(self.store.get(a), 100), 'filled')

    def test_a_lone_candidate_is_not_blocked(self):
        """反证：只有一个候选时必须能成交 —— 否则"接上了"与"一直拒绝"分不开。"""
        a = self._executing('US.A', 1)
        self.assertEqual(self.service.submit(self.store.get(a), 100), 'filled')


class ReconcileTests(Base):
    def test_skips_exactly_the_excess_in_rule_order(self):
        ids = [self.prop(c, created_at=i)['id']
               for i, c in enumerate(['US.A', 'US.B', 'US.C', 'US.D'])]
        out = reconcile_capacity(self.owner)
        self.assertEqual([d['code'] for d in out['skipped']], ['US.D'])
        self.assertEqual(out['skipped'][0]['rank'], 4)
        self.assertIn('max_positions=3', out['skipped'][0]['note'])
        self.assertEqual(self.store.get(ids[3])['status'], 'skipped')
        self.assertEqual(self.store.get(ids[3])['note'],
                         '容量未分配：规则序第 4 位，超出 max_positions=3')
        for i in range(3):
            self.assertEqual(self.store.get(ids[i])['status'], 'pending')

    def test_rerun_skips_nothing_more(self):
        for i, c in enumerate(['US.A', 'US.B', 'US.C', 'US.D']):
            self.prop(c, created_at=i)
        reconcile_capacity(self.owner)
        self.assertEqual(reconcile_capacity(self.owner)['skipped'], [])

    def test_sells_are_never_skipped(self):
        for i, c in enumerate(['US.A', 'US.B', 'US.C']):
            self.prop(c, created_at=i)
        sell = self.prop('US.D', side='sell', created_at=9)
        self.assertEqual(reconcile_capacity(self.owner)['skipped'], [])
        self.assertEqual(self.store.get(sell['id'])['status'], 'pending')

    def test_a_proposal_under_review_is_not_pulled_out(self):
        """评审已在飞的超容量提案这一轮不动 —— 否则等于从评审底下把提案抽走。"""
        for i, c in enumerate(['US.A', 'US.B', 'US.C']):
            self.prop(c, created_at=i)
        late = self.prop('US.D', created_at=3, llm=None, review=True)
        out = reconcile_capacity(self.owner)
        self.assertEqual(out['skipped'], [])
        self.assertEqual(self.store.get(late['id'])['status'], 'pending')

    def test_a_proposal_held_elsewhere_does_not_take_a_proposal_slot(self):
        """已有持仓/订单的代码已占了一格，不再从提案名额里再占一次。"""
        with self.service.registry.transaction() as book:
            book['positions']['US.A'] = {'qty': 5, 'entry_price': 100, 'initial_risk': 250,
                                         'risk_group': 'semis'}
        ids = [self.prop(c, created_at=i)['id'] for i, c in enumerate(['US.A', 'US.B', 'US.C'])]
        self.assertEqual(reconcile_capacity(self.owner)['skipped'], [])
        for pid in ids:
            self.assertEqual(self.store.get(pid)['status'], 'pending')


class StateMachineTests(Base):
    def test_pending_can_be_skipped_for_capacity(self):
        """`skipped` 与 `expired` 是两回事：前者是"容量未分配"，混用会让界面撒谎。"""
        p = self.prop('US.A')
        self.assertTrue(self.store.mark(p['id'], 'skipped', note='容量未分配'))
        self.assertEqual(self.store.get(p['id'])['status'], 'skipped')

    def test_illegal_transitions_are_still_refused(self):
        p = self.prop('US.A')
        self.assertFalse(self.store.mark(p['id'], 'executing'))
        self.assertEqual(self.store.get(p['id'])['status'], 'pending')
