"""期权市场视角（P1）回归：纯计算指标 + 摘要 + evidence。"""
import unittest

from scripts.live_trading.option_view import (
    compute_option_view, make_option_evidence, option_view_summary,
)


def _row(otype, strike, oi=0, iv=None, delta=None, prob=None):
    return {'option_type': otype, 'strike_price': strike, 'open_interest': oi,
            'implied_volatility': iv, 'delta': delta, 'prob_of_profit': prob}


class OptionViewComputeContracts(unittest.TestCase):
    def test_atm_iv_and_up_prob(self):
        # spot≈100，ATM call delta 0.6 → up_prob 从 prob_of_profit
        rows = [
            _row('CALL', 95, oi=100, iv=40, delta=0.3, prob=40),
            _row('CALL', 100, oi=200, iv=45, delta=0.6, prob=62),   # ATM call
            _row('PUT', 100, oi=150, iv=46, delta=-0.4, prob=38),
            _row('PUT', 105, oi=80, iv=48, delta=-0.6, prob=30),
        ]
        v = compute_option_view(100, rows)
        self.assertAlmostEqual(v['atm_iv_pct'], 45.5, places=0)  # (45+46)/2
        self.assertEqual(v['pcr_oi'], 0.77)   # (150+80)/(100+200)=230/300
        self.assertEqual(v['max_pain'], 100)  # call200+put150=350 最大
        self.assertEqual(v['up_prob_pct'], 62)

    def test_iv_missing_and_empty(self):
        self.assertTrue(compute_option_view(None, []).get('error'))
        self.assertTrue(compute_option_view(100, []).get('error'))

    def test_up_prob_falls_back_to_delta(self):
        rows = [_row('CALL', 100, oi=10, iv=30, delta=0.55)]
        v = compute_option_view(100, rows)
        self.assertEqual(v['up_prob_pct'], 55.0)

    def test_max_pain_picks_oi_peak(self):
        rows = [
            _row('CALL', 90, oi=500), _row('CALL', 95, oi=1000), _row('PUT', 90, oi=200),
            _row('PUT', 95, oi=800),
        ]
        v = compute_option_view(95, rows)
        self.assertEqual(v['max_pain'], 95)  # (500+200)=700 vs (1000+800)=1800


class OptionViewSummaryContracts(unittest.TestCase):
    def test_summary_mentions_all_metrics(self):
        rows = [
            _row('CALL', 100, oi=200, iv=45, delta=0.6, prob=62),
            _row('PUT', 100, oi=150, iv=46, delta=-0.4, prob=38),
        ]
        v = compute_option_view(100, rows)
        s = option_view_summary(v)
        for token in ('ATM IV', 'Put/Call OI', 'MaxPain', '上涨概率'):
            self.assertIn(token, s)

    def test_make_evidence_kind_option(self):
        rows = [_row('CALL', 100, oi=10, iv=30, delta=0.6)]
        ev = make_option_evidence('US.A', compute_option_view(100, rows),
                                  now='2026-09-08T00:00:00+00:00')
        self.assertEqual(ev['kind'], 'option')
        self.assertTrue(ev['evidence_id'].startswith('evidence_'))
        self.assertIn('internal:option-view', ev['source'])


if __name__ == '__main__':
    unittest.main()
