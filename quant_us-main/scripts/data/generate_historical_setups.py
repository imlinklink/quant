#!/usr/bin/env python3
"""从冻结日线逐日生成可用于A/B/C/D的历史Setup，不调用模型。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from scripts.data.io_utils import read_frame,write_frame
from scripts.live_trading.setup_features import compute_setup_features
from scripts.live_trading.setup_state_machine import build_setup_candidate,transition

ET=ZoneInfo('America/New_York')


def _market_time(session,hour,minute=0):
    return pd.Timestamp(session).tz_localize(ET)+pd.Timedelta(hours=hour,minutes=minute)


def generate(daily:pd.DataFrame,config:dict,llm_labels=None,start=None,end=None):
    required={'stock','date','open','high','low','close','volume'}
    if not required.issubset(daily):raise ValueError('daily 缺字段: '+','.join(sorted(required-set(daily))))
    d=daily.copy();d['date']=pd.to_datetime(d.date).dt.tz_localize(None).dt.normalize()
    d=d.sort_values(['stock','date']);market=d[d.stock=='US.SPY'].copy()
    cfg=dict(config.get('buy_strategy_v2',config));baseline_cfg=dict(cfg,require_weekly_gate=False)
    minimum=int(cfg.get('min_daily_bars',250));labels={}
    if llm_labels is not None and not llm_labels.empty:
        labels=dict(zip(llm_labels.setup_id.astype(str),llm_labels.llm_decision.astype(str)))
    rows=[]
    for code,bars in d.groupby('stock'):
        if code=='US.SPY':continue
        bars=bars.reset_index(drop=True);state='FALLING'
        for i in range(minimum-1,len(bars)-1):
            session=bars.date.iloc[i]
            if end and session>pd.Timestamp(end):break
            as_of=_market_time(session,16).tz_convert('UTC')
            market_prefix=market[market.date<=session]
            snapshot=compute_setup_features(bars.iloc[:i+1],None,market_prefix,as_of,baseline_cfg)
            state,_=transition(state,snapshot,baseline_cfg)
            if start and session<pd.Timestamp(start):continue
            candidate=build_setup_candidate(code,snapshot,state,config=baseline_cfg)
            if not candidate:continue
            nxt=bars.iloc[i+1];setup_id=candidate['setup_id']
            rows.append(dict(candidate,stock=code,
                setup_time=as_of.isoformat(),next_open_time=_market_time(nxt.date,9,30).tz_convert('UTC').isoformat(),
                next_open_price=float(nxt.open),signal_close=float(bars.close.iloc[i]),
                atr14=float(snapshot['features']['atr14']),
                weekly_gate=bool(snapshot['features']['weekly_gate']),
                daily_confirmed=state=='CONFIRMED',llm_decision=labels.get(setup_id,'missing')))
    return pd.DataFrame(rows)


def main():
    p=argparse.ArgumentParser(description='从冻结日线生成历史周线/日线Setup')
    p.add_argument('--daily',required=True);p.add_argument('--config',required=True);p.add_argument('--llm-labels')
    p.add_argument('--start');p.add_argument('--end');p.add_argument('--output',required=True);args=p.parse_args()
    with open(args.config,encoding='utf-8') as fh:cfg=yaml.safe_load(fh) or {}
    labels=read_frame(args.llm_labels) if args.llm_labels else None
    out=generate(read_frame(args.daily),cfg,labels,args.start,args.end)
    write_frame(out,args.output);print(f'wrote {len(out)} historical setups to {args.output}')


if __name__=='__main__':main()
