import tempfile
import unittest
from pathlib import Path

from scripts.live_trading.decision_ledger.outcome_jobs import OutcomeSettlement
from scripts.live_trading.decision_ledger.position_counterfactual import (
    PositionCounterfactualLedger, freeze_payload, simulate,
)
from scripts.live_trading.position_registry import PositionRegistry


def frozen(action='reduce', action_quantity=5.0, stop=90.0, fee_rate=0.0):
    return {
        'counterfactual_id': 'cf1', 'experiment_version': 'position-counterfactual-v1',
        'decision_id': 'd1', 'trade_id': 't1', 'starting_quantity': 10.0,
        'reference_price': 100.0, 'active_stop': stop,
        'hard_exit_authoritative': True, 'fee_rate': fee_rate,
        'l_path': {'action': action, 'quantity': action_quantity},
    }


class PositionCounterfactualTests(unittest.TestCase):
    def test_reduce_executes_at_next_open_and_tracks_missed_upside(self):
        rows = simulate(frozen(), [
            {'open': 101, 'low': 99, 'close': 102},
            {'open': 102, 'low': 101, 'close': 110},
        ], horizons=(1, 2))
        self.assertEqual(rows[0]['l_remaining_quantity'], 5.0)
        self.assertEqual(rows[0]['freed_cash'], 505.0)
        self.assertAlmostEqual(rows[1]['r_return_pct'], 0.10)
        self.assertAlmostEqual(rows[1]['l_return_pct'], 0.055)
        self.assertAlmostEqual(rows[1]['missed_upside_pct'], 0.045)

    def test_hard_stop_precedes_llm_action_and_closes_both_paths(self):
        rows = simulate(frozen(action='exit', action_quantity=10, stop=95), [
            {'open': 92, 'low': 90, 'close': 94},
        ], horizons=(1,))
        self.assertEqual(rows[0]['r_remaining_quantity'], 0.0)
        self.assertEqual(rows[0]['l_remaining_quantity'], 0.0)
        self.assertAlmostEqual(rows[0]['r_return_pct'], -0.08)
        self.assertEqual(rows[0]['delta_return_pct'], 0.0)

    def test_hold_is_control_path(self):
        rows = simulate(frozen(action='hold', action_quantity=0), [
            {'open': 100, 'low': 99, 'close': 103},
        ], horizons=(1,))
        self.assertEqual(rows[0]['r_return_pct'], rows[0]['l_return_pct'])
        self.assertEqual(rows[0]['delta_return_pct'], 0.0)

    def test_freeze_uses_validated_template_and_citation_scope(self):
        packet = {
            'context': {'as_of': '2026-09-19T12:00:00+00:00'},
            'trade': {'trade_id': 't1', 'code': 'US.A', 'remaining_qty': 10,
                      'mark_price': 100, 'entry_price': 80},
            'protection': {'active_stop': 90, 'hard_exit_authoritative': True},
            'allowed_actions': [
                {'template_id': 't1:reduce:50', 'action': 'reduce', 'quantity': 5}],
            'new_evidence': [
                {'evidence_id': 'e1', 'subject_code': 'US.A'},
                {'evidence_id': 'e2', 'subject_code': 'MARKET'}],
        }
        review = {'decision_id': 'd1', 'proposed_action': 'reduce',
                  'action_template_id': 't1:reduce:50',
                  'facts': [{'evidence_ids': ['e1']}], 'inferences': [],
                  'counterevidence': [{'evidence_ids': ['e2']}]}
        payload = freeze_payload(packet, review, review_id='r1', trigger='new_evidence')
        self.assertEqual(payload['l_path']['quantity'], 5.0)
        self.assertEqual(payload['citations']['subject_counts'], {'US.A': 1, 'MARKET': 1})

    def test_ledger_and_outcomes_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = PositionRegistry(Path(tmp) / 'state.db', 'DRY-RUN')
            payload = frozen(action='exit', action_quantity=10)
            payload['as_of'] = payload['frozen_at'] = '2026-09-19T12:00:00+00:00'
            PositionCounterfactualLedger(registry).events.record(
                'position_counterfactual_frozen', 'cf1', payload)
            settlement = OutcomeSettlement(registry)
            count = settlement.settle_position_counterfactual(payload, [
                {'open': 100, 'low': 99, 'close': 101},
                {'open': 101, 'low': 100, 'close': 102},
                {'open': 102, 'low': 101, 'close': 103},
            ])
            self.assertEqual(count, 2)
            with settlement.events.transaction() as con:
                rows = con.execute(
                    'SELECT horizon,return_pct,benchmark_return_pct,excess_return_pct '
                    'FROM decision_outcomes_v2 ORDER BY horizon').fetchall()
            self.assertEqual(len(rows), 2)
            self.assertAlmostEqual(rows[0][1], 0.0)
            self.assertAlmostEqual(rows[0][2], 0.01)


if __name__ == '__main__':
    unittest.main()
