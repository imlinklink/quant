#!/usr/bin/env python3
"""周线/日线买入研究的一键数据流水线；产物按 run-id 不可覆盖。"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from scripts.data.build_daily_liquidity import build_liquidity
from scripts.data.download_market_history import download
from scripts.data.io_utils import read_frame,sha256_file,write_frame
from scripts.data.normalize_market_history import collect_inputs,normalize
from scripts.data.trading_calendar import sessions
from scripts.data.validate_market_history import validate_daily
from scripts.historical_universe import build_point_in_time_universe


def _write_json(path,payload):
    path=Path(path);tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    tmp.replace(path)


def _filter_daily(daily,master,start,end):
    codes=set(master.code.astype(str));d=daily[daily.stock.astype(str).isin(codes)].copy()
    dates=pd.to_datetime(d.date);return d[(dates>=pd.Timestamp(start))&(dates<=pd.Timestamp(end))].reset_index(drop=True)


def run_pipeline(*,master_path,start,end,universe_start,universe_end,run_id,
                 raw_root='data/market_history/raw',runs_root='data/market_history/runs',
                 checkpoint='data/market_history/checkpoints/download_state.json',
                 skip_download=False,ctx=None,min_price=5.,min_dollar_volume=5_000_000.):
    run_dir=Path(runs_root)/run_id
    if run_dir.exists() and any(run_dir.iterdir()):raise FileExistsError(f'run-id 已存在，禁止覆盖: {run_dir}')
    run_dir.mkdir(parents=True,exist_ok=False)
    state={'run_id':run_id,'status':'running','created_at':datetime.now(timezone.utc).isoformat(),
           'parameters':{'start':start,'end':end,'universe_start':universe_start,
                         'universe_end':universe_end,'min_price':min_price,
                         'min_dollar_volume':min_dollar_volume},'stages':{}}
    _write_json(run_dir/'pipeline.json',state)
    try:
        master=read_frame(master_path)
        required={'code','listing_date','delisting_date','asset_type'}
        if not required.issubset(master):raise ValueError('security master 缺字段: '+','.join(sorted(required-set(master))))
        if 'US.SPY' not in set(master.code.astype(str)):
            raise ValueError('security master 必须包含 US.SPY，用于构建实际交易日历')
        shutil.copy2(master_path,run_dir/'security_master.csv')
        state['stages']['master']={'status':'complete','rows':len(master)};_write_json(run_dir/'pipeline.json',state)

        if not skip_download:
            if ctx is None:raise ValueError('未提供 Futu quote context')
            dl=download(ctx,master.code.astype(str).tolist(),start,end,raw_root,checkpoint,kinds=('day',))
            if dl.get('failed'):raise RuntimeError(f"行情下载失败分区: {len(dl['failed'])}")
            state['stages']['download']={'status':'complete','partitions':len(dl['completed'])}
        else:state['stages']['download']={'status':'skipped'}
        _write_json(run_dir/'pipeline.json',state)

        raw=collect_inputs([Path(raw_root)/'day'])
        daily=_filter_daily(normalize(raw,'day'),master,start,end)
        if daily.empty:raise ValueError('标准化后没有日线数据')
        write_frame(daily,run_dir/'daily.csv.gz')
        state['stages']['normalize']={'status':'complete','rows':len(daily)};_write_json(run_dir/'pipeline.json',state)

        spy=daily[daily.stock=='US.SPY']
        if spy.empty:raise ValueError('US.SPY 日线为空，无法生成实际交易日历')
        calendar=sessions(start,end,reference_daily=run_dir/'daily.csv.gz')
        write_frame(calendar,run_dir/'trading_calendar.csv')
        state['stages']['calendar']={'status':'complete','sessions':len(calendar),'source':'US.SPY daily'}
        _write_json(run_dir/'pipeline.json',state)

        quality=validate_daily(daily,master,calendar)
        write_frame(quality,run_dir/'daily_quality.csv')
        liquidity=build_liquidity(daily)
        failed=set(quality.loc[quality.quality!='good','stock'])
        liquidity.loc[liquidity.code.isin(failed),'quality']='quality_fail'
        write_frame(liquidity,run_dir/'daily_liquidity.csv.gz')
        state['stages']['quality']={'status':'complete','good':int((quality.quality=='good').sum()),
                                    'failed':int((quality.quality!='good').sum())}
        _write_json(run_dir/'pipeline.json',state)

        target_sessions=calendar[(pd.to_datetime(calendar.session_date)>=pd.Timestamp(universe_start))&
                                 (pd.to_datetime(calendar.session_date)<=pd.Timestamp(universe_end))]
        universe=build_point_in_time_universe(master,liquidity,pd.to_datetime(target_sessions.session_date),
                                               min_price,min_dollar_volume)
        write_frame(universe,run_dir/'universe.csv')
        state['stages']['universe']={'status':'complete','rows':len(universe),
                                     'eligible':int(universe.eligible.sum())}
        files=['security_master.csv','daily.csv.gz','trading_calendar.csv','daily_quality.csv',
               'daily_liquidity.csv.gz','universe.csv']
        state['files']={name:{'size':(run_dir/name).stat().st_size,'sha256':sha256_file(run_dir/name)} for name in files}
        state['status']='complete';state['completed_at']=datetime.now(timezone.utc).isoformat()
        _write_json(run_dir/'pipeline.json',state);return state
    except Exception as exc:
        state['status']='failed';state['error']=f'{type(exc).__name__}: {exc}'
        state['failed_at']=datetime.now(timezone.utc).isoformat();_write_json(run_dir/'pipeline.json',state);raise


def main():
    p=argparse.ArgumentParser(description='运行日线研究数据流水线')
    p.add_argument('--master',required=True);p.add_argument('--start',default='2015-01-01');p.add_argument('--end',required=True)
    p.add_argument('--universe-start',default='2016-01-01');p.add_argument('--universe-end',required=True)
    p.add_argument('--run-id',default=datetime.now().strftime('%Y%m%dT%H%M%S'))
    p.add_argument('--raw-root',default='data/market_history/raw');p.add_argument('--runs-root',default='data/market_history/runs')
    p.add_argument('--checkpoint',default='data/market_history/checkpoints/download_state.json')
    p.add_argument('--skip-download',action='store_true');p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=11111)
    p.add_argument('--min-price',type=float,default=5.);p.add_argument('--min-dollar-volume',type=float,default=5_000_000.)
    args=p.parse_args();ctx=None
    try:
        if not args.skip_download:
            from futu import OpenQuoteContext
            ctx=OpenQuoteContext(host=args.host,port=args.port)
        result=run_pipeline(master_path=args.master,start=args.start,end=args.end,
            universe_start=args.universe_start,universe_end=args.universe_end,run_id=args.run_id,
            raw_root=args.raw_root,runs_root=args.runs_root,checkpoint=args.checkpoint,
            skip_download=args.skip_download,ctx=ctx,min_price=args.min_price,
            min_dollar_volume=args.min_dollar_volume)
        print(json.dumps({'run_id':args.run_id,'status':result['status'],'stages':result['stages']},ensure_ascii=False,indent=2));return 0
    finally:
        if ctx is not None:ctx.close()


if __name__=='__main__':raise SystemExit(main())
