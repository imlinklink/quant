"""历史引擎 vs 影子引擎奇偶校验（第1条）。"""
import unittest

import pandas as pd

from scripts.portfolio_shadow.schema import to_micro
from scripts.portfolio_shadow.verify_parity import verify_matrix_parity, verify_parity


def bar(o, h, l, c):
    return {'open': o, 'high': h, 'low': l, 'close': c}


class ParityTests(unittest.TestCase):
    def test_t1_settlement_parity(self):
        sessions = ['2026-01-05', '2026-01-06', '2026-01-07']
        bars = {
            '2026-01-05': {'SEC-A': bar(100, 101, 99, 100.0)},
            '2026-01-06': {'SEC-A': bar(100, 101, 99, 100.5)},
            '2026-01-07': {'SEC-A': bar(100.5, 101.5, 99.5, 101.0)},
        }
        entries = [{'security_id': 'SEC-A', 'entry_session': '2026-01-05', 'atr14': 2.0}]
        r = verify_parity(sessions, bars, entries, [], horizon=2, risk_bp=100)
        self.assertEqual(r['diffs'], [], f'两引擎逐日 equity 应一致，实际 {r["diffs"]}')

    def test_dividend_receivable_parity(self):
        sessions = ['2026-01-05', '2026-01-06', '2026-01-07', '2026-01-08']
        bars = {
            '2026-01-05': {'SEC-A': bar(100, 101, 99, 100.0)},
            '2026-01-06': {'SEC-A': bar(100, 101, 99, 100.5)},
            '2026-01-07': {'SEC-A': bar(100.5, 101.5, 99.5, 101.0)},
            '2026-01-08': {'SEC-A': bar(101, 102, 100, 101.5)},
        }
        entries = [{'security_id': 'SEC-A', 'entry_session': '2026-01-05', 'atr14': 2.0}]
        actions = [{'security_id': 'SEC-A', 'action_type': 'cash_dividend',
                    'ex_date': '2026-01-06', 'pay_date': '2026-01-08',
                    'cash_amount_micro': to_micro(1.0)}]
        r = verify_parity(sessions, bars, entries, actions, horizon=3, risk_bp=100)
        self.assertEqual(r['diffs'], [], f'两引擎逐日 equity 应一致，实际 {r["diffs"]}')

    def test_matrix_parity_synthetic(self):
        prices = pd.DataFrame([
            {'security_id': 'SEC-A', 'session': '2026-01-05', 'raw_open': 100.0,
             'raw_high': 101.0, 'raw_low': 99.0, 'raw_close': 100.0},
            {'security_id': 'SEC-A', 'session': '2026-01-06', 'raw_open': 100.0,
             'raw_high': 101.0, 'raw_low': 99.0, 'raw_close': 100.5},
            {'security_id': 'SEC-A', 'session': '2026-01-07', 'raw_open': 100.5,
             'raw_high': 101.5, 'raw_low': 99.5, 'raw_close': 101.0},
        ])
        matrix = pd.DataFrame([{'security_id': 'SEC-A', 'entry_session': '2026-01-05',
                                'entry_price': 100.0, 'initial_stop': 92.0,
                                'exit_session': '2026-01-06', 'exit_price': 100.5,
                                'exit_phase': 'CLOSE', 'exit_reason': 'TIME_EXIT',
                                'portfolio_accepted': True, 'rank': 0, 'entry_id': 'e1'}])
        r = verify_matrix_parity(matrix, prices, pd.DataFrame(), horizon=2, risk_bp=100)
        self.assertEqual(r['diffs'], [], f'矩阵奇偶校验应零分歧，实际 {r["diffs"]}')


if __name__ == '__main__':
    unittest.main()


def test_historical_engine_pays_weekend_receivable_not_just_nav_parity():
    from scripts.medium_term.portfolio_engine import simulate_multi_asset_portfolio
    sessions = pd.to_datetime(['2026-01-05','2026-01-06','2026-01-09','2026-01-12'])
    prices = pd.DataFrame({'security_id':'SEC-A','session':sessions,
                          'raw_open':[100,99,99,99],'raw_close':[100,99,99,99]})
    matrix = pd.DataFrame([dict(security_id='SEC-A',entry_session=sessions[0],entry_price=100,
        initial_stop=92,exit_session=sessions[-1],exit_price=99,exit_phase='CLOSE',
        exit_reason='TIME_EXIT',portfolio_accepted=True)])
    actions = pd.DataFrame([dict(security_id='SEC-A',action_type='cash_dividend',
        ex_date='2026-01-06',pay_date='2026-01-10',cash_amount=1)])
    result = simulate_multi_asset_portfolio(prices,matrix,actions=actions,
        t1_settlement=True,dividend_receivable=True,shadow_precision=True)
    nav = result.equity
    assert nav.iloc[-2].dividend_receivable == 125
    assert nav.iloc[-1].dividend_receivable == 0
    assert nav.iloc[-1]['cash'] - nav.iloc[-2]['cash'] == 125
