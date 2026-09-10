import unittest

from scripts.live_trading.setup_state_machine import build_setup_candidate, transition


def snapshot(**overrides):
    features = {'close': 105.0, 'ma20': 100.0, 'ma50': 102.0, 'ma200': 90.0,
                'ma20_slope_5d': .01, 'ma50_slope_20d': .02, 'atr14': 3.0,
                'no_new_low': True, 'one_day_return': .01,
                'volume_ratio_20d': 1.0, 'relative_strength_20d': .03}
    features.update(overrides)
    return {'feature_version': 'daily-setup-v1', 'session': '2026-09-10',
            'quality': {'status': 'pass'}, 'features': features,
            'structure': {'swing_low': 99.0, 'prior_swing_low': 95.0,
                          'higher_low': True, 'reversal_level': 104.0}}


class SetupStateTests(unittest.TestCase):
    def test_falling_cannot_jump_directly_to_confirmed(self):
        state, _ = transition('FALLING', snapshot())
        self.assertEqual(state, 'STABILIZING')

    def test_progresses_through_reversing_and_confirmed(self):
        state, _ = transition('STABILIZING', snapshot())
        self.assertEqual(state, 'REVERSING')
        state, _ = transition('REVERSING', snapshot())
        self.assertEqual(state, 'CONFIRMED')

    def test_candidate_has_bounded_prices(self):
        item = build_setup_candidate('US.X', snapshot(), 'CONFIRMED')
        self.assertIsNotNone(item)
        self.assertLess(item['initial_stop'], item['trigger_price'])
        self.assertGreaterEqual(item['max_chase_price'], item['trigger_price'])

    def test_quality_failure_returns_falling(self):
        s = snapshot(); s['quality'] = {'status': 'fail'}
        self.assertEqual(transition('CONFIRMED', s)[0], 'FALLING')


if __name__ == '__main__':
    unittest.main()
