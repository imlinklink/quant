#!/usr/bin/env python3
"""将一个已收盘交易日的 Futu 不复权日线保存为不可覆盖的前向证据快照。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.data.download_market_history import fetch_pages
from scripts.data.id_bridge import attach_security_id
from scripts.data.io_utils import sha256_file
from scripts.data.import_corporate_actions_from_futu import build_actions
from scripts.data.normalize_market_history import normalize


def capture(ctx, codes, symbols, session, output_dir, *, observed_at=None, futu_types=None):
    day=pd.Timestamp(session).normalize()
    observed=pd.Timestamp(observed_at or datetime.now(timezone.utc))
    if observed.tzinfo is None:raise ValueError('OBSERVED_AT_TIMEZONE_REQUIRED')
    close=(day.tz_localize('America/New_York')+pd.Timedelta(hours=16)).tz_convert('UTC')
    if observed.tz_convert('UTC')<close:raise ValueError('DAILY_BAR_NOT_CLOSED')
    codes=list(codes)
    if not codes or len(codes)!=len(set(codes)):raise ValueError('CODES_MISSING_OR_DUPLICATE')
    out=Path(output_dir)
    if out.exists():raise FileExistsError(f'快照目录已存在: {out}')
    if futu_types is None:
        from futu import AuType, KLType, Session
        futu_types=(KLType.K_DAY, AuType.NONE, Session.RTH)
    ktype, autype, trading_session=futu_types
    out.mkdir(parents=True)
    raw_dir=out/'raw_response';raw_dir.mkdir()
    raw=[];failures={};hashes={};splits={};dividends={}
    for code in codes:
        try:
            frame,_=fetch_pages(ctx,code,day,day,ktype,autype=autype,
                                session=trading_session,request_interval=0)
            if len(frame)!=1 or not {'code','time_key','open','high','low','close','volume'}.issubset(frame):
                raise ValueError('ONE_COMPLETE_DAILY_BAR_REQUIRED')
            row=frame.iloc[0]
            if str(row.code)!=code or pd.Timestamp(row.time_key).normalize()!=day:
                raise ValueError('BAR_CODE_OR_SESSION_MISMATCH')
            o,h,l,c,v=[float(row[x]) for x in ('open','high','low','close','volume')]
            if not (0<l<=min(o,c)<=max(o,c)<=h and v>=0):
                raise ValueError('INVALID_OHLCV')
            path=raw_dir/(code.replace('.','_')+'.csv')
            frame.to_csv(path,index=False)
            hashes[str(path.relative_to(out))]=sha256_file(path)
            raw.append(frame)
        except Exception as exc:
            failures[code]=f'{type(exc).__name__}:{exc}'
    for code in codes:
        try:
            ret, split_data=ctx.get_corporate_actions_stock_splits(code)
            if ret!=0:raise RuntimeError(f'SPLITS_FETCH_FAILED:{split_data}')
            ret, dividend_data=ctx.get_corporate_actions_dividends(code)
            if ret!=0:raise RuntimeError(f'DIVIDENDS_FETCH_FAILED:{dividend_data}')
            splits[code]=(split_data or {}).get('split_list',[])
            dividends[code]=(dividend_data or {}).get('dividend_list',[])
        except Exception as exc:
            failures[code+'|actions']=f'{type(exc).__name__}:{exc}'
    if not any(k.endswith('|actions') for k in failures):
        path=out/'actions_raw.json'
        path.write_text(json.dumps({'splits':splits,'dividends':dividends},ensure_ascii=False,default=str)+'\n')
        hashes[path.name]=sha256_file(path)
        actions=build_actions(splits,dividends)
        actions['source_observed_at']=observed.tz_convert('UTC').isoformat()
        path=out/'corporate_actions.csv';actions.to_csv(path,index=False)
        hashes[path.name]=sha256_file(path)
    if not failures:
        daily=normalize(pd.concat(raw,ignore_index=True))
        mapped,unmapped,ambiguous=attach_security_id(daily,symbols,symbol_col='stock',date_col='date')
        if len(unmapped) or len(ambiguous) or len(mapped)!=len(codes):
            failures['mapping']=f'unmapped={len(unmapped)},ambiguous={len(ambiguous)}'
        else:
            mapped['observed_at']=observed.tz_convert('UTC').isoformat()
            mapped['price_basis']='raw'
            mapped['price_version']='raw-none-forward-v1'
            path=out/'daily.csv.gz';mapped.to_csv(path,index=False)
            hashes[path.name]=sha256_file(path)
    summary={'status':'complete' if not failures else 'incomplete',
             'session':day.date().isoformat(),'observed_at':observed.tz_convert('UTC').isoformat(),
             'adjustment':'none','expected_codes':codes,'received_codes':len(raw),
             'failures':failures,'sha256':hashes}
    (out/'snapshot.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    return summary


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--codes',required=True,help='含 code 列的固定15只清单')
    p.add_argument('--symbols',required=True)
    p.add_argument('--session',required=True,help='纽约交易日 YYYY-MM-DD')
    p.add_argument('--output-dir',required=True)
    p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=11111)
    args=p.parse_args()
    codes=pd.read_csv(args.codes).code.astype(str).tolist()
    symbols=pd.read_csv(args.symbols)
    from futu import OpenQuoteContext
    ctx=OpenQuoteContext(host=args.host,port=args.port)
    try:result=capture(ctx,codes,symbols,args.session,args.output_dir)
    finally:ctx.close()
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status']=='complete' else 1


if __name__=='__main__':raise SystemExit(main())
