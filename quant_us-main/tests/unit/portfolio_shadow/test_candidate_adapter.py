"""candidate_adapter：排程 → READY Opportunity 的确定性测试。"""
import unittest

from scripts.portfolio_shadow.candidate_adapter import adapt_schedule, intents_for_session


class AdapterTests(unittest.TestCase):
    def test_adapt_schedule_produces_ready_opportunities(self):
        schedule = [{'security_id': 'SEC-A', 'source_candidate_id': 'c1',
                     'signal_session': '2026-01-04', 'observed_at': '2026-01-04T21:00:00Z',
                     'planned_execution_session': '2026-01-05', 'rank': 1,
                     'atr14_micro': 2000000, 'input_hash': 'h1'}]
        opps = adapt_schedule('exp1', '1', 'b3', 'H60', schedule)
        self.assertEqual(len(opps), 1)
        self.assertEqual(opps[0].terminal, 'READY')
        self.assertEqual(opps[0].stop_reference['atr14_micro'], 2000000)
        # intents_for_session 只取 READY + 当日执行
        self.assertEqual(len(intents_for_session(opps, '2026-01-05')), 1)
        self.assertEqual(len(intents_for_session(opps, '2026-01-06')), 0)

    def test_opportunity_id_is_deterministic(self):
        schedule = [{'security_id': 'SEC-A', 'source_candidate_id': 'c1',
                     'signal_session': '2026-01-04', 'observed_at': '2026-01-04T21:00:00Z',
                     'planned_execution_session': '2026-01-05', 'rank': 1,
                     'atr14_micro': 2000000, 'input_hash': 'h1'}]
        a = adapt_schedule('exp1', '1', 'b3', 'H60', schedule)[0]
        b = adapt_schedule('exp1', '1', 'b3', 'H60', schedule)[0]
        self.assertEqual(a.opportunity_id(), b.opportunity_id())


if __name__ == '__main__':
    unittest.main()
