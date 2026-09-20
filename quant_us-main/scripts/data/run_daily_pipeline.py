#!/usr/bin/env python3
"""周线/日线买入研究的一键数据流水线；产物按 run-id 不可覆盖。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))

from scripts.data.build_daily_liquidity import build_liquidity
from scripts.data.build_security_master_from_futu import reconcile_listing_dates
from scripts.data.download_market_history import download,failures_for_request,records_for_request
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


def propagate_quality(liquidity, quality):
    """把质量失败按 **(股票, 年份)** 传播到流动性表（设计 §8.4「该股票区间」）。

    只标真正失败的年份，避免单年问题把该股票全历史一起标为失败。
    `failed_years` 为空的失败股票保守地整段标记。
    """
    out = liquidity.copy()
    fail_pairs = set()
    fail_all = set()
    for _, row in quality.loc[quality.quality != 'good'].iterrows():
        years = [y.strip() for y in str(row.get('failed_years') or '').split(';') if y.strip()]
        if years:
            fail_pairs |= {(row['stock'], int(y)) for y in years}
        else:
            fail_all.add(row['stock'])
    if fail_pairs or fail_all:
        years = pd.to_datetime(out['date']).dt.year
        mask = pd.Series([(c, int(y)) in fail_pairs for c, y in zip(out['code'], years)],
                         index=out.index) | out['code'].isin(fail_all)
        out.loc[mask, 'quality'] = 'quality_fail'
    return out


def run_pipeline(*,master_path,start,end,universe_start,universe_end,run_id,
                 raw_root='data/market_history/raw',runs_root='data/market_history/runs',
                 checkpoint='data/market_history/checkpoints/download_state.json',
                 skip_download=False,ctx=None,min_price=5.,min_dollar_volume=5_000_000.,
                 verified_master=False,adjustment='qfq'):
    """`adjustment` 是**价格口径**（qfq 前复权 / none 不复权 / hfq 后复权）。

    它原先在四处写死成 `qfq`、且没有 CLI —— 而价格口径会**实质改变宇宙成员资格**。
    实测（2026-09-20）：前复权序列里 `SEC-US-NVDA 2015-01-05 previous_raw_close = 0.4819`
    （真实约 $19.3，0.4819 × 40 倍拆股 = 19.28），于是 NVDA 被 `PRICE_TOO_LOW` 排除
    **719 个 session**（2015→2017-10）—— 十年最大的赢家被一个假理由挡在样本外。
    没有这个参数，"不复权价基的时点宇宙"就根本做不出来。
    """
    basis = Path(raw_root)/'day'/adjustment
    if not basis.exists():
        have = sorted(p.name for p in (Path(raw_root)/'day').glob('*')) if (Path(raw_root)/'day').exists() else []
        raise ValueError(f'价基目录不存在: {basis}（该 raw_root 下实际有: {have}）')
    run_dir=Path(runs_root)/run_id
    if run_dir.exists() and any(run_dir.iterdir()):raise FileExistsError(f'run-id 已存在，禁止覆盖: {run_dir}')
    run_dir.mkdir(parents=True,exist_ok=False)
    state={'run_id':run_id,'status':'running','created_at':datetime.now(timezone.utc).isoformat(),
           'parameters':{'start':start,'end':end,'universe_start':universe_start,
                         'universe_end':universe_end,'min_price':min_price,
                         'min_dollar_volume':min_dollar_volume,'adjustment':adjustment},'stages':{}}
    _write_json(run_dir/'pipeline.json',state)
    try:
        master=read_frame(master_path)
        required={'code','listing_date','delisting_date','asset_type'}
        if not required.issubset(master):raise ValueError('security master 缺字段: '+','.join(sorted(required-set(master))))
        if 'US.SPY' not in set(master.code.astype(str)):
            raise ValueError('security master 必须包含 US.SPY，用于构建实际交易日历')
        state['stages']['master']={'status':'complete','rows':len(master)};_write_json(run_dir/'pipeline.json',state)

        if not skip_download:
            if ctx is None:raise ValueError('未提供 Futu quote context')
            listing_dates=dict(zip(master.code.astype(str),master.listing_date))
            dl=download(ctx,master.code.astype(str).tolist(),start,end,raw_root,checkpoint,
                        kinds=('day',),listing_dates=listing_dates,autype=adjustment)
            active_failed=failures_for_request(dl,master.code.astype(str).tolist(),('day',),start,end,adjustment)
            if active_failed:raise RuntimeError(f"行情下载失败分区: {len(active_failed)}")
            request_args=(master.code.astype(str).tolist(),('day',),start,end,adjustment)
            completed=records_for_request(dl.get('completed',{}),*request_args)
            unavailable=records_for_request(dl.get('unavailable',{}),*request_args)
            state['stages']['download']={'status':'complete','adjustment':adjustment,
                                         'partitions':len(completed),'unavailable':len(unavailable)}
        else:state['stages']['download']={'status':'skipped'}
        _write_json(run_dir/'pipeline.json',state)

        raw=collect_inputs([basis])
        daily=_filter_daily(normalize(raw,'day'),master,start,end)
        if daily.empty:raise ValueError('标准化后没有日线数据')
        write_frame(daily,run_dir/'daily.csv.gz')
        if verified_master:
            # 已核验上市日：不用首根历史日线覆盖真实上市日（设计 §0 组件表）。
            master.to_csv(run_dir/'security_master.csv',index=False)
            state['stages']['listing_dates']={'status':'preserved_verified'}
        else:
            master=reconcile_listing_dates(master,daily,start)
            master.to_csv(run_dir/'security_master.csv',index=False)
        state['stages']['normalize']={'status':'complete','rows':len(daily)};_write_json(run_dir/'pipeline.json',state)

        spy=daily[daily.stock=='US.SPY']
        if spy.empty:raise ValueError('US.SPY 日线为空，无法生成实际交易日历')
        calendar=sessions(start,end,reference_daily=run_dir/'daily.csv.gz')
        write_frame(calendar,run_dir/'trading_calendar.csv')
        state['stages']['calendar']={'status':'complete','sessions':len(calendar),'source':'US.SPY daily'}
        _write_json(run_dir/'pipeline.json',state)

        quality=validate_daily(daily,master,calendar)
        write_frame(quality,run_dir/'daily_quality.csv')
        liquidity=propagate_quality(build_liquidity(daily),quality)
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
    p.add_argument('--verified-master',action='store_true',
                   help='已核验主数据模式：不覆盖真实上市日，供 v2（security_id 键）正式流程使用')
    p.add_argument('--min-price',type=float,default=5.);p.add_argument('--min-dollar-volume',type=float,default=5_000_000.)
    p.add_argument('--adjustment',default='qfq',
                   help='价格口径 qfq/none/hfq（默认 qfq 保持既有行为）。'
                        '口径会改变宇宙成员资格，见 run_pipeline 的 docstring')
    args=p.parse_args();ctx=None
    try:
        if not args.skip_download:
            from futu import OpenQuoteContext
            ctx=OpenQuoteContext(host=args.host,port=args.port)
        result=run_pipeline(master_path=args.master,start=args.start,end=args.end,
            universe_start=args.universe_start,universe_end=args.universe_end,run_id=args.run_id,
            raw_root=args.raw_root,runs_root=args.runs_root,checkpoint=args.checkpoint,
            skip_download=args.skip_download,ctx=ctx,min_price=args.min_price,
            min_dollar_volume=args.min_dollar_volume,verified_master=args.verified_master,
            adjustment=args.adjustment)
        print(json.dumps({'run_id':args.run_id,'status':result['status'],'stages':result['stages']},ensure_ascii=False,indent=2));return 0
    finally:
        if ctx is not None:ctx.close()


if __name__=='__main__':raise SystemExit(main())
