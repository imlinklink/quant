#!/usr/bin/env python3
"""从当前配置和Futu基本资料生成第一阶段security_master_pilot.csv。"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from scripts.data.io_utils import write_frame

DEFAULT_LEVERAGED={'US.SOXL','US.SOXS','US.TQQQ','US.SQQQ','US.UPRO','US.SPXS',
                   'US.YINN','US.YANG','US.TNA','US.TZA','US.QLD','US.SSO'}


def normalize_code(value):
    value=str(value).strip().upper()
    return value if value.startswith('US.') else f'US.{value}'


def codes_from_config(config,include=()):
    codes=[]
    for key in ('buy_strategy_v2','dip_buy','trend_breakout','pullback','breakout_retest'):
        codes.extend((config.get(key,{}) or {}).get('watch_list') or [])
    codes.extend(((config.get('pullback',{}) or {}).get('sector_proxies') or {}).values())
    codes.extend(include);codes.append('US.SPY')
    return sorted({normalize_code(c) for c in codes if str(c).strip()})


def _asset_type(row,leveraged):
    code=normalize_code(row.get('code'))
    if code in leveraged:return 'leveraged_etf'
    values=' '.join(str(row.get(k,'')).lower() for k in ('stock_type','stock_child_type','security_type','name'))
    is_etf='etf' in values or 'exchange traded fund' in values
    is_leveraged=is_etf and bool(re.search(r'(^|\W)[+-]?[23]x(\W|$)|ultra(pro)?|daily target',values))
    if is_leveraged:return 'leveraged_etf'
    return 'etf' if is_etf else 'stock'


def reconcile_listing_dates(master:pd.DataFrame,daily:pd.DataFrame,history_start):
    """用首根日线修正试验master；触及下载边界时只标记下界，不伪造上市日。"""
    out=master.copy();out['listing_date']=pd.to_datetime(out.listing_date,errors='coerce')
    first=(daily.assign(date=pd.to_datetime(daily.date)).groupby('stock').date.min())
    boundary=pd.Timestamp(history_start)+pd.Timedelta(days=7)
    for idx,row in out.iterrows():
        observed=first.get(row.code)
        if pd.isna(observed):
            out.at[idx,'listing_date_quality']='no_daily_history';continue
        if observed>boundary and (row.get('listing_date_quality')=='unknown' or
                                  pd.isna(row.listing_date) or row.listing_date<observed):
            out.at[idx,'listing_date']=observed
            out.at[idx,'listing_date_quality']='confirmed_by_first_daily'
        elif observed<=boundary and row.get('listing_date_quality')=='unknown':
            out.at[idx,'listing_date_quality']='listed_on_or_before_history_start'
    out['listing_date']=out.listing_date.dt.strftime('%Y-%m-%d')
    return out


def build_from_basicinfo(frame,codes,*,as_of=None,leveraged=(),allow_missing=False):
    if 'code' not in frame:raise ValueError('Futu basicinfo 缺少 code 字段')
    d=frame.copy();d['code']=d.code.map(normalize_code);wanted=set(map(normalize_code,codes))
    d=d[d.code.isin(wanted)].sort_values('code').drop_duplicates('code',keep='last')
    missing=sorted(wanted-set(d.code))
    if missing and not allow_missing:raise ValueError('Futu未返回证券: '+','.join(missing))
    leveraged={normalize_code(c) for c in set(leveraged)|DEFAULT_LEVERAGED}
    listing_source=(d['listing_date'] if 'listing_date' in d else
                    d.get('list_time',pd.Series(index=d.index,dtype=object)))
    listing=pd.to_datetime(listing_source,errors='coerce')
    result=pd.DataFrame({
        'code':d.code,
        'listing_date':listing.dt.strftime('%Y-%m-%d'),
        'delisting_date':'',
        'asset_type':d.apply(lambda row:_asset_type(row,leveraged),axis=1),
        'name':d.get('name',''),
        'exchange':d.get('exchange_type',d.get('market','')),
        'lot_size':pd.to_numeric(d.get('lot_size',pd.Series(index=d.index,dtype=float)),errors='coerce'),
        'currency':'USD','source':'futu_basicinfo_current',
        'as_of':as_of or date.today().isoformat(),
        'listing_date_quality':listing.map(lambda x:'unknown' if pd.isna(x) or x.year<=1970
                                           else 'reported_unverified'),
    })
    if result.listing_date.isna().any() and not allow_missing:
        bad=','.join(result.loc[result.listing_date.isna(),'code'])
        raise ValueError('Futu未提供上市日期: '+bad)
    if missing:
        extra=pd.DataFrame({'code':missing,'listing_date':'','delisting_date':'',
            'asset_type':'stock','name':'','exchange':'','lot_size':pd.NA,'currency':'USD',
            'source':'missing_from_futu','as_of':as_of or date.today().isoformat(),
            'listing_date_quality':'unknown'})
        result=pd.concat([result,extra],ignore_index=True)
    return result.sort_values('code').reset_index(drop=True)


def fetch_basicinfo(ctx):
    """分别获取普通股和ETF；Futu不接受None作为stock_type。"""
    from futu import Market,SecurityType
    frames=[]
    for kind in (SecurityType.STOCK,SecurityType.ETF):
        ret,data=ctx.get_stock_basicinfo(market=Market.US,stock_type=kind)
        if ret!=0:raise RuntimeError(f'Futu get_stock_basicinfo({kind})失败: {data}')
        if data is not None and len(data):frames.append(data)
    if not frames:raise RuntimeError('Futu返回空证券目录')
    return pd.concat(frames,ignore_index=True)


def main():
    p=argparse.ArgumentParser(description='从Futu生成security_master_pilot.csv')
    p.add_argument('--config',default=str(ROOT/'config.yaml'));p.add_argument('--include',nargs='*',default=[])
    p.add_argument('--leveraged',nargs='*',default=[]);p.add_argument('--output',default='data/security_master_pilot.csv')
    p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=11111)
    p.add_argument('--allow-missing',action='store_true');p.add_argument('--overwrite',action='store_true');args=p.parse_args()
    with open(args.config,encoding='utf-8') as fh:config=yaml.safe_load(fh) or {}
    codes=codes_from_config(config,args.include)
    from futu import OpenQuoteContext
    ctx=OpenQuoteContext(host=args.host,port=args.port)
    try:basic=fetch_basicinfo(ctx)
    finally:ctx.close()
    out=build_from_basicinfo(basic,codes,leveraged=args.leveraged,allow_missing=args.allow_missing)
    write_frame(out,args.output,overwrite=args.overwrite)
    print(f'wrote {len(out)} securities to {args.output}')
    for kind,count in out.groupby('asset_type').size().items():print(f'  {kind}: {count}')
    unknown=out[out.listing_date_quality=='unknown']
    if len(unknown):print('需人工补充上市日期: '+','.join(unknown.code))


if __name__=='__main__':main()
