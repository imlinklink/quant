import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.data.capture_forward_raw_day import capture


class QuoteContext:
    def __init__(self, rows):self.rows=rows;self.calls=[]

    def request_history_kline(self, code, **kwargs):
        self.calls.append((code,kwargs))
        return 0,pd.DataFrame(self.rows.get(code,[])),None

    def get_corporate_actions_stock_splits(self, code):return 0,{'split_list':[]}

    def get_corporate_actions_dividends(self, code):return 0,{'dividend_list':[]}


class ForwardCaptureTests(unittest.TestCase):
    def setUp(self):
        self.symbols=pd.DataFrame([{'security_id':'SEC-X','symbol':'US.X',
            'valid_from':'2020-01-01','valid_to':''}])
        self.row={'code':'US.X','time_key':'2026-09-11 00:00:00',
                  'open':100,'high':102,'low':99,'close':101,'volume':1000}

    def test_complete_snapshot_is_raw_and_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'run';ctx=QuoteContext({'US.X':[self.row]})
            report=capture(ctx,['US.X'],self.symbols,'2026-09-11',path,
                           observed_at='2026-09-11T22:00:00Z',futu_types=('day','none','rth'))
            self.assertEqual(report['status'],'complete')
            self.assertEqual(ctx.calls[0][1]['autype'],'none')
            daily=pd.read_csv(path/'daily.csv.gz')
            self.assertEqual(daily.price_basis.iloc[0],'raw')
            self.assertEqual(daily.security_id.iloc[0],'SEC-X')
            self.assertIn('daily.csv.gz',report['sha256'])
            self.assertIn('corporate_actions.csv',report['sha256'])
            with self.assertRaises(FileExistsError):
                capture(ctx,['US.X'],self.symbols,'2026-09-11',path,
                        observed_at='2026-09-11T22:00:00Z',futu_types=('day','none','rth'))

    def test_unclosed_and_wrong_session_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx=QuoteContext({'US.X':[self.row]})
            with self.assertRaisesRegex(ValueError,'DAILY_BAR_NOT_CLOSED'):
                capture(ctx,['US.X'],self.symbols,'2026-09-11',Path(tmp)/'early',
                        observed_at='2026-09-11T19:00:00Z',futu_types=('day','none','rth'))
            row=dict(self.row,time_key='2026-09-10 00:00:00')
            result=capture(QuoteContext({'US.X':[row]}),['US.X'],self.symbols,
                           '2026-09-11',Path(tmp)/'wrong',observed_at='2026-09-11T22:00:00Z',
                           futu_types=('day','none','rth'))
            self.assertEqual(result['status'],'incomplete')
            self.assertNotIn('daily.csv.gz',result['sha256'])


if __name__=='__main__':unittest.main()
