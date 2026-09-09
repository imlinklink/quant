"""期权市场视角（P1）回归：纯计算指标 + 摘要 + evidence。"""
import unittest

from scripts.live_trading.option_view import (
    assess_data_quality, compute_option_view, compute_term_structure,
    make_option_evidence, option_view_summary, select_expiry_buckets,
)


def _row(otype, strike, oi=0, iv=None, delta=None, prob=None, volume=0,
         expiry='2026-10-16', update_time='2026-09-09 10:00:00'):
    return {'option_type': otype, 'strike_price': strike, 'open_interest': oi,
            'implied_volatility': iv, 'delta': delta, 'prob_of_profit': prob,
            'volume': volume, 'expiry_date': expiry, 'update_time': update_time}


class OptionViewComputeContracts(unittest.TestCase):
    def test_atm_iv_delta_and_full_chain_metrics(self):
        rows = [
            _row('CALL', 95, oi=100, iv=40, delta=0.3, prob=40),
            _row('CALL', 100, oi=200, iv=45, delta=0.6, prob=62),   # ATM call
            _row('PUT', 100, oi=150, iv=46, delta=-0.4, prob=38),
            _row('PUT', 105, oi=80, iv=48, delta=-0.6, prob=30),
        ]
        v = compute_option_view(100, rows, chain_complete=True)
        self.assertAlmostEqual(v['atm_iv_pct'], 45.5, places=0)  # (45+46)/2
        self.assertEqual(v['pcr_oi'], 0.77)   # (150+80)/(100+200)=230/300
        self.assertEqual(v['max_pain'], 100)
        self.assertEqual(v['atm_call_delta'], 0.6)
        self.assertNotIn('up_prob_pct', v)

    def test_iv_missing_and_empty(self):
        self.assertTrue(compute_option_view(None, []).get('error'))
        self.assertTrue(compute_option_view(100, []).get('error'))

    def test_delta_is_not_exposed_as_probability(self):
        rows = [_row('CALL', 100, oi=10, iv=30, delta=0.55)]
        v = compute_option_view(100, rows, chain_complete=True)
        self.assertEqual(v['atm_call_delta'], 0.55)
        self.assertNotIn('up_prob_pct', v)

    def test_true_max_pain_can_differ_from_oi_peak(self):
        rows = [
            _row('CALL', 90, oi=1500), _row('PUT', 90, oi=0),
            _row('CALL', 100, oi=700), _row('PUT', 100, oi=700),
            _row('CALL', 110, oi=0), _row('PUT', 110, oi=1500),
        ]
        v = compute_option_view(100, rows, chain_complete=True)
        self.assertIn(v['oi_peak_strike'], (90, 110))
        self.assertEqual(v['max_pain'], 100)

    def test_sample_does_not_claim_full_chain_metrics(self):
        rows = [_row('CALL', 100, oi=100), _row('PUT', 100, oi=150)]
        v = compute_option_view(100, rows, chain_complete=False)
        self.assertEqual(v['chain_scope'], 'near_atm_sample')
        self.assertEqual(v['sample_pcr_oi'], 1.5)
        self.assertNotIn('pcr_oi', v)
        self.assertNotIn('max_pain', v)

    def test_expiry_bucket_selection(self):
        selected = select_expiry_buckets(
            ['2026-09-18', '2026-10-16', '2026-11-20'], as_of='2026-09-09')
        self.assertEqual(selected['short']['dte'], 9)
        self.assertEqual(selected['mid']['dte'], 37)
        self.assertEqual(selected['long']['dte'], 72)

    def test_quality_gate_uses_coverage_sides_iv_and_age(self):
        rows = [_row('CALL', 100, iv=40), _row('PUT', 100, iv=42)]
        good = assess_data_quality(
            rows, expected_legs=2, observed_at='2026-09-09T14:00:00+00:00',
            now='2026-09-09T14:05:00+00:00')
        self.assertEqual(good['status'], 'usable')
        stale = assess_data_quality(
            rows, expected_legs=4, observed_at='2026-09-09T13:00:00+00:00',
            now='2026-09-09T14:05:00+00:00')
        self.assertEqual(stale['status'], 'degraded')
        self.assertIn('snapshot_stale', stale['reasons'])

    def test_term_structure_and_full_chain_pcr(self):
        def pair(iv, expiry, call_volume, put_volume):
            return [
                _row('CALL', 100, oi=100, iv=iv, delta=.5,
                     volume=call_volume, expiry=expiry),
                _row('PUT', 100, oi=150, iv=iv + 2, delta=-.5,
                     volume=put_volume, expiry=expiry),
            ]
        buckets = {
            'short': pair(60, '2026-09-18', 20, 30),
            'mid': pair(50, '2026-10-16', 40, 20),
            'long': pair(45, '2026-11-20', 10, 5),
        }
        view = compute_term_structure(
            100, buckets, expected_legs={k: 2 for k in buckets},
            observed_at='2026-09-09T14:00:00+00:00',
            now='2026-09-09T14:01:00+00:00')
        self.assertEqual(view['primary_bucket'], 'mid')
        self.assertEqual(view['term_structure']['state'], 'front_elevated')
        self.assertEqual(view['buckets']['mid']['pcr_oi'], 1.5)
        self.assertEqual(view['buckets']['mid']['pcr_volume'], 0.5)
        self.assertEqual(view['quality']['status'], 'usable')


class OptionViewSummaryContracts(unittest.TestCase):
    def test_summary_mentions_all_metrics(self):
        rows = [
            _row('CALL', 100, oi=200, iv=45, delta=0.6, prob=62),
            _row('PUT', 100, oi=150, iv=46, delta=-0.4, prob=38),
        ]
        v = compute_option_view(100, rows, chain_complete=True)
        s = option_view_summary(v)
        for token in ('ATM IV', 'Put/Call OI', 'MaxPain', 'Delta'):
            self.assertIn(token, s)

    def test_make_evidence_kind_option(self):
        rows = [_row('CALL', 100, oi=10, iv=30, delta=0.6)]
        ev = make_option_evidence('US.A', compute_option_view(100, rows),
                                  now='2026-09-08T00:00:00+00:00')
        self.assertEqual(ev['kind'], 'option')
        self.assertTrue(ev['evidence_id'].startswith('evidence_'))
        self.assertIn('internal:option-view', ev['source'])

    def test_unusable_quality_hides_metrics(self):
        summary = option_view_summary({
            'spot': 100, 'atm_iv_pct': 50,
            'quality': {'status': 'unusable', 'reasons': ['one_sided_chain']},
        })
        self.assertIn('不可用', summary)
        self.assertNotIn('ATM IV', summary)


if __name__ == '__main__':
    unittest.main()
