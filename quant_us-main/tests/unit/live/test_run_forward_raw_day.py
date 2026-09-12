import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts.data.capture_forward_raw_day import capture
from scripts.data.run_forward_raw_day import history_as_of, pending_decisions, settle_pending
from tests.unit.live.test_generate_historical_setups_raw_asof import bars, split


class QuoteContext:
    def __init__(self,row):self.row=row
    def request_history_kline(self,code,**kwargs):return 0,pd.DataFrame([self.row]),None
    def get_corporate_actions_stock_splits(self,code):return 0,{'split_list':[]}
    def get_corporate_actions_dividends(self,code):return 0,{'dividend_list':[]}


class ForwardRawRunnerTests(unittest.TestCase):
    def test_history_rejects_tampered_snapshot(self):
        data=bars();day=data.date.iloc[209].date().isoformat()
        row={'code':'US.A','time_key':day,'open':100,'high':101,'low':99,
             'close':100,'volume':1000}
        symbols=pd.DataFrame([{'security_id':'SEC-A','symbol':'US.A',
                              'valid_from':'2020-01-01','valid_to':''}])
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/day
            capture(QuoteContext(row),['US.A'],symbols,day,folder,
                    observed_at=f'{day}T23:00:00Z',futu_types=('day','none','rth'))
            (folder/'daily.csv.gz').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'SNAPSHOT_HASH_MISMATCH'):
                history_as_of(data.iloc[:209],split(data).iloc[0:0],[folder],day,['US.A'])

    def test_pending_then_replay_uses_observed_time_and_raw_open(self):
        data=bars();day=data.date.iloc[209].date().isoformat()
        actions=split(data).assign(record_hash='same',source_observed_at=f'{day}T21:00:00Z')
        symbols=pd.DataFrame([{'security_id':'SEC-A','symbol':'US.A',
                              'valid_from':'2020-01-01','valid_to':''}])
        master=pd.DataFrame([{'security_id':'SEC-A','asset_type':'stock',
                             'listed_at':'2020-01-01','valid_from':'2020-01-01',
                             'valid_to':'','quality_status':'unverified'}])
        def candidate(code,snapshot,state,config):
            return {'setup_id':'same-id','trigger_price':100.,'initial_stop':95.,
                    'risk_per_share':5.} if snapshot['session']==day else None
        config={'buy_strategy_v2':{'min_daily_bars':200}}
        with patch('scripts.data.generate_historical_setups.build_setup_candidate',side_effect=candidate):
            pending=pending_decisions(data.iloc[:210],actions.iloc[0:0],config,
                                      day,f'{day}T22:00:00Z')
        generated,accepted,rejected=settle_pending(pending,data.iloc[:211],actions,
            config,data.date.iloc[210],master,symbols,actions)
        self.assertEqual(pending.decision_session.iloc[0],day)
        self.assertEqual(generated.setup_time.iloc[0],f'{day}T22:00:00+00:00')
        self.assertEqual(generated.next_open_price.iloc[0],50.)
        self.assertEqual(generated.initial_stop.iloc[0],47.5)
        self.assertEqual(len(accepted)+len(rejected),len(generated)*1)

    def test_unobserved_entry_day_action_blocks(self):
        data=bars();day=data.date.iloc[209].date().isoformat();actions=split(data).assign(record_hash='same',source_observed_at=f'{day}T21:00:00Z')
        pending=pd.DataFrame([{'setup_id':'x','decision_session':day,
            'setup_time':f'{day}T22:00:00+00:00','execution_status':'pending_next_open'}])
        with self.assertRaisesRegex(ValueError,'ENTRY_ACTION_NOT_KNOWN_BEFORE_OPEN'):
            settle_pending(pending,data.iloc[:211],actions,{},data.date.iloc[210],
                pd.DataFrame(),pd.DataFrame(),actions.iloc[0:0])


if __name__=='__main__':unittest.main()
