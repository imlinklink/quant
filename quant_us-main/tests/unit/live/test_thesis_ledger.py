"""Thesis Ledger（阶段 I2）回归：状态转移、无新证据不改、delta、回放一致、实际退出对比。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.live_trading.decision_ledger.event_store import make_event, stable_id
from scripts.live_trading.decision_ledger.thesis_ledger import (
    ThesisLedger, apply_transition, build_delta, chain_report, cited_evidence_ids,
    compare_actual_exit,
)
from scripts.live_trading.position_registry import PositionRegistry

TRADE = 'trade_x'


def _review(thesis_state, cited=(), status='complete'):
    r = {'status': status, 'thesis_state': thesis_state,
         'facts': [{'text': '依据', 'evidence_ids': list(cited)}],
         'inferences': [], 'counterevidence': []}
    if status != 'complete':
        r = {'status': status, 'error': 'x', 'thesis_state': None}
    return r


def _evidence_id(tag):
    return stable_id('evidence', tag)


class ApplyTransitionContracts(unittest.TestCase):
    def test_first_review_establishes(self):
        st, note = apply_transition(None, 'unchanged', False, False)
        self.assertEqual(st, 'established')

    def test_no_evidence_does_not_change_state(self):
        # strengthened 但无新证据 → 保持原状态
        st, note = apply_transition('unchanged', 'strengthened', False, False)
        self.assertEqual(st, 'unchanged')
        self.assertIn('无新证据', note)
        # invalidated 但无新证据、未触保护线 → 保持
        st2, _ = apply_transition('weakened', 'invalidated', False, False)
        self.assertEqual(st2, 'weakened')

    def test_new_evidence_allows_change(self):
        st, _ = apply_transition('established', 'strengthened', True, False)
        self.assertEqual(st, 'strengthened')
        st2, _ = apply_transition('weakened', 'invalidated', True, False)
        self.assertEqual(st2, 'invalidated')

    def test_near_risk_allows_change(self):
        st, _ = apply_transition('unchanged', 'invalidated', False, True)
        self.assertEqual(st, 'invalidated')

    def test_invalidated_is_terminal(self):
        st, _ = apply_transition('invalidated', 'unchanged', True, False)
        self.assertEqual(st, 'invalidated')  # 不往回改


class DeltaContracts(unittest.TestCase):
    def test_build_delta(self):
        d = build_delta(['a', 'b'], ['b', 'c'])
        self.assertEqual(d['added'], ['c'])
        self.assertEqual(d['removed'], ['a'])

    def test_cited_ids(self):
        review = {
            'facts': [{'evidence_ids': ['a']}],
            'inferences': [{'evidence_ids': ['b', 'a']}],
            'counterevidence': [{'evidence_ids': ['c']}],
        }
        self.assertEqual(cited_evidence_ids(review), {'a', 'b', 'c'})


class ThesisLedgerStoreContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = PositionRegistry(Path(self.tmp.name) / 'state.db', 'DRY-RUN')
        self.ledger = ThesisLedger(self.registry)

    def _rec(self, thesis_state, cited=(), has_ev=False, trigger='session_close'):
        return self.ledger.record_review(
            trade_id=TRADE, code='US.A', plan_id='p1', plan_version=1,
            review_id='r1', review=_review(thesis_state, cited),
            evidence_items=['e1'] if has_ev else [], trigger=trigger)

    def test_record_and_replay(self):
        # 首次建立
        self._rec('unchanged', cited=['a'])
        # 无新证据 → weakened 不采纳，也不写新版本
        self._rec('weakened', cited=['a'], has_ev=False)
        # 有新证据 → strengthened
        self._rec('strengthened', cited=['a', 'b'], has_ev=True)
        # 有新证据 → invalidated
        self._rec('invalidated', cited=['a', 'b', 'c'], has_ev=True, trigger='new_evidence')

        updates = self.ledger.load_updates(TRADE)
        states = [u['state'] for u in updates]
        self.assertEqual(states, ['established', 'strengthened', 'invalidated'])
        self.assertEqual(self.ledger.current(TRADE), 'invalidated')
        # delta：最后一步 added=c
        self.assertEqual(updates[-1]['delta']['added'], ['c'])

    def test_no_change_does_not_write(self):
        self._rec('unchanged', cited=['a'])
        self.assertEqual(len(self.ledger.load_updates(TRADE)), 1)
        # 无新证据、状态不变 → 不产生新版本
        self._rec('unchanged', cited=['a'], has_ev=False)
        self.assertEqual(len(self.ledger.load_updates(TRADE)), 1)

    def test_delta_added_between_reviews(self):
        self._rec('unchanged', cited=['a'], has_ev=True)
        self._rec('strengthened', cited=['a', 'b'], has_ev=True, trigger='new_evidence')
        updates = self.ledger.load_updates(TRADE)
        self.assertEqual(updates[-1]['delta']['added'], ['b'])

    def test_mark_closed(self):
        self._rec('unchanged', cited=['a'])
        self.ledger.mark_closed(trade_id=TRADE, reason='time_exit')
        self.assertEqual(self.ledger.current(TRADE), 'closed')

    def test_version_sequence_and_idempotent(self):
        # 状态连续变化两次 → version 1,2
        self._rec('unchanged', cited=['a'], has_ev=True)         # established v1
        self._rec('invalidated', cited=['a', 'b'], has_ev=True)  # invalidated v2
        updates = self.ledger.load_updates(TRADE)
        self.assertEqual([u['version'] for u in updates], [1, 2])
        # 重复相同 record_review 不再产生新版本
        self._rec('unchanged', cited=['a'], has_ev=True)
        self.assertEqual(len(self.ledger.load_updates(TRADE)), 2)

    def test_compare_actual_exit(self):
        self._rec('unchanged', cited=['a'], has_ev=True)
        self._rec('invalidated', cited=['a', 'b'], has_ev=True, trigger='new_evidence')
        # 造一个 trade_closed 事件（用 events.transaction 拿真实 con）
        ev = make_event(self.registry.namespace, 'trade_closed', 'k',
                        {'status': 'closed'}, trade_id=TRADE)
        with self.ledger.events.transaction() as con:
            from scripts.live_trading.decision_ledger.event_store import insert_event
            insert_event(con, ev)
        report = compare_actual_exit(self.ledger.events.events(), TRADE)
        self.assertEqual(report['invalidated_count'], 1)
        self.assertIsNotNone(report['actual_exit'])
        self.assertIn('invalidated', report['chain'])


if __name__ == '__main__':
    unittest.main()
