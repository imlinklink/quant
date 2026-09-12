#!/usr/bin/env python3
"""按已归档的不复权日快照生成 T 日待成交信号，并在 T+1 核对实际开盘。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from scripts.buy_strategy_experiment_runner import apply_universe, build_abcd_entries
from scripts.data.build_daily_liquidity import build_liquidity
from scripts.data.generate_historical_setups import generate
from scripts.data.asof_feature_panel import _factors_by_ex_date
from scripts.data.id_bridge import LIQUIDITY_RENAME, attach_security_id
from scripts.data.io_utils import sha256_file
from scripts.historical_universe import build_point_in_time_universe_v2
from scripts.data.trading_calendar import _rule_sessions


def read_snapshot(path):
    path=Path(path);meta=json.loads((path/'snapshot.json').read_text())
    if meta.get('status')!='complete' or meta.get('adjustment')!='none':
        raise ValueError(f'SNAPSHOT_NOT_RAW_COMPLETE:{path}')
    for relative,digest in meta.get('sha256',{}).items():
        file=path/relative
        if not file.is_file() or sha256_file(file)!=digest:
            raise ValueError(f'SNAPSHOT_HASH_MISMATCH:{file}')
    if not {'daily.csv.gz','corporate_actions.csv','actions_raw.json'}.issubset(meta.get('sha256',{})):
        raise ValueError(f'SNAPSHOT_EVIDENCE_INCOMPLETE:{path}')
    daily=pd.read_csv(path/'daily.csv.gz');actions=pd.read_csv(path/'corporate_actions.csv')
    if daily.empty or not daily.price_basis.eq('raw').all() or not daily.date.astype(str).str.startswith(meta['session']).all():
        raise ValueError(f'SNAPSHOT_DAILY_INVALID:{path}')
    if set(daily.stock)!=set(meta['expected_codes']) or daily.stock.duplicated().any():
        raise ValueError(f'SNAPSHOT_CODES_INVALID:{path}')
    observed=pd.Timestamp(meta['observed_at'])
    if observed.tzinfo is None or daily.observed_at.nunique()!=1 or pd.Timestamp(daily.observed_at.iloc[0])!=observed:
        raise ValueError(f'SNAPSHOT_OBSERVED_AT_INVALID:{path}')
    return daily,actions,meta


def history_as_of(baseline_daily, baseline_actions, snapshot_dirs, day, fixed_codes):
    target=pd.Timestamp(day).normalize();snapshots=[]
    for path in sorted(map(Path,snapshot_dirs)):
        session=pd.Timestamp(path.name).normalize()
        if session<=target:snapshots.append(read_snapshot(path))
    if not snapshots or snapshots[-1][2]['session']!=target.date().isoformat():
        raise ValueError('DECISION_DAY_SNAPSHOT_MISSING')
    expected=set(fixed_codes)
    if any(set(meta['expected_codes'])!=expected for _,_,meta in snapshots):
        raise ValueError('SNAPSHOT_SAMPLE_CHANGED')
    first=pd.Timestamp(snapshots[0][2]['session'])
    old=baseline_daily.copy();old['date']=pd.to_datetime(old.date).dt.normalize()
    old=old[old.date<first]
    if set(old.stock)-expected:raise ValueError('BASELINE_CONTAINS_UNFROZEN_CODES')
    sessions=_rule_sessions(old.date.max()+pd.Timedelta(days=1),first-pd.Timedelta(days=1))
    if len(sessions):raise ValueError('BASELINE_TO_SNAPSHOT_GAP')
    daily=pd.concat([old]+[d for d,_,_ in snapshots],ignore_index=True)
    daily['date']=pd.to_datetime(daily.date).dt.normalize()
    if daily.duplicated(['security_id','date']).any():raise ValueError('DAILY_SESSION_DUPLICATE')
    actions=baseline_actions.copy()
    actions['ex_date']=pd.to_datetime(actions.ex_date).dt.normalize()
    actions=actions[actions.ex_date<first]
    action_frames=[actions]
    for _,frame,meta in snapshots:
        if frame.empty:continue
        frame=frame.copy();frame['ex_date']=pd.to_datetime(frame.ex_date).dt.normalize()
        observed=pd.to_datetime(frame.source_observed_at,utc=True,errors='coerce')
        if observed.isna().any() or observed.gt(pd.Timestamp(meta['observed_at'])).any():
            raise ValueError('ACTION_OBSERVED_AT_INVALID')
        action_frames.append(frame[frame.ex_date<=target])
    actions=pd.concat(action_frames,ignore_index=True)
    key=['security_id','source_record_id']
    if not actions.empty:
        conflicts=actions.groupby(key,dropna=False).record_hash.nunique()
        if conflicts.gt(1).any():raise ValueError('ACTION_HISTORY_CONFLICT')
        actions=actions.drop_duplicates(['record_hash'],keep='first')
    return daily.sort_values(['stock','date']),actions,snapshots[-1][2]


def pending_decisions(daily,actions,config,day,observed_at):
    target=pd.Timestamp(day).normalize()
    raw=generate(daily,config,start=target,end=target,price_basis='raw_asof',
                 actions=actions,include_pending=True)
    if raw.empty:return pd.DataFrame(columns=['setup_id','setup_time','decision_session','execution_status'])
    result=raw[pd.to_datetime(raw.setup_time,utc=True).dt.date==target.date()].copy()
    result['setup_time']=pd.Timestamp(observed_at).tz_convert('UTC').isoformat()
    result['decision_session']=target.date().isoformat()
    return result.reset_index(drop=True)


def settle_pending(pending,daily,actions,config,day,master,symbols,prior_actions):
    target=pd.Timestamp(day).normalize()
    if pending.empty:
        empty=pd.DataFrame()
        return empty,empty,empty
    if pending.decision_session.nunique()!=1:raise ValueError('PENDING_SESSION_INVALID')
    previous=pd.Timestamp(pending.decision_session.iloc[0]).normalize()
    if previous>=target:raise ValueError('PENDING_SESSION_INVALID')
    if list(_rule_sessions(previous+pd.Timedelta(days=1),target))!=[target]:
        raise ValueError('ENTRY_NOT_NEXT_SESSION')
    if not pending.execution_status.eq('pending_next_open').all():raise ValueError('PENDING_STATUS_INVALID')
    today_actions=actions[pd.to_datetime(actions.ex_date).dt.normalize()==target]
    if not prior_actions.empty:
        prior_observed=pd.to_datetime(prior_actions.source_observed_at,utc=True,errors='coerce')
        decision_time=pd.Timestamp(pending.setup_time.iloc[0])
        if prior_observed.isna().any() or prior_observed.gt(decision_time).any():
            raise ValueError('ENTRY_ACTION_OBSERVED_AFTER_DECISION')
    before=set(zip(prior_actions.security_id.astype(str),prior_actions.action_type.astype(str),
                   pd.to_datetime(prior_actions.ex_date).dt.normalize(),prior_actions.record_hash.astype(str)))
    for action in today_actions.itertuples(index=False):
        key=(str(action.security_id),str(action.action_type),target,str(action.record_hash))
        if key not in before:raise ValueError(f'ENTRY_ACTION_NOT_KNOWN_BEFORE_OPEN:{action.security_id}')
    generated=pending.copy();generated['scale_to_next']=1.0
    for code,index in generated.groupby('stock').groups.items():
        bars=daily[daily.stock.eq(code)].rename(columns={'date':'session'})
        sec=str(generated.loc[next(iter(index)),'security_id'])
        sec_actions=actions[actions.security_id.astype(str).eq(sec)]
        factors=_factors_by_ex_date(sec_actions,bars)
        scale=float(np.prod(factors.get(target,[]) or [1.0]))
        generated.loc[index,'scale_to_next']=scale
        for field in ('trigger_price','invalidation_price','initial_stop','max_chase_price',
                      'risk_per_share','signal_close','atr14'):
            if field in generated:generated.loc[index,field]=generated.loc[index,field].astype(float)*scale
        today=bars[pd.to_datetime(bars.session).dt.normalize().eq(target)]
        if len(today)!=1:raise ValueError(f'ENTRY_RAW_OPEN_MISSING:{code}')
        generated.loc[index,'next_open_price']=float(today.open.iloc[0])
    et=target.tz_localize('America/New_York')+pd.Timedelta(hours=9,minutes=30)
    generated['next_open_time']=et.tz_convert('UTC').isoformat()
    generated['execution_status']='observed_next_open'
    entries=build_abcd_entries(generated)
    entries=entries[entries.experiment.isin(['A','B','C'])]
    liq=build_liquidity(daily)
    mapped,unmapped,ambiguous=attach_security_id(liq,symbols,symbol_col='code',date_col='date')
    if len(unmapped) or len(ambiguous):raise ValueError('LIQUIDITY_SECURITY_MAPPING_FAILED')
    mapped=mapped.rename(columns=LIQUIDITY_RENAME)
    universe=build_point_in_time_universe_v2(master,symbols,mapped,[target],
                master_version='futu-survivor39-20260912',price_version='raw-none-forward-v1')
    runner=universe.assign(code=universe.symbol_as_of,
                           quality=np.where(universe.eligible,'good','not_eligible'))
    accepted,rejected=apply_universe(entries,runner)
    return generated,accepted,rejected


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=('decision','settle'),required=True)
    p.add_argument('--day',required=True);p.add_argument('--baseline-daily',required=True)
    p.add_argument('--baseline-actions',required=True);p.add_argument('--snapshots-dir',required=True)
    p.add_argument('--codes',required=True);p.add_argument('--config',required=True)
    p.add_argument('--master',required=True);p.add_argument('--symbols',required=True)
    p.add_argument('--pending',help='settle 模式必填：上一交易日的 pending_setups.csv')
    p.add_argument('--prior-actions',help='settle 模式必填：上一交易日快照中的 corporate_actions.csv')
    p.add_argument('--output-dir',required=True);args=p.parse_args()
    out=Path(args.output_dir)
    if out.exists():raise FileExistsError(f'不可覆盖: {out}')
    codes=pd.read_csv(args.codes).code.astype(str).tolist()
    if len(codes)!=15 or len(set(codes))!=15:raise ValueError('FIXED_15_CODES_REQUIRED')
    daily,actions,meta=history_as_of(pd.read_csv(args.baseline_daily),
        pd.read_csv(args.baseline_actions),Path(args.snapshots_dir).iterdir(),args.day,codes)
    with open(args.config,encoding='utf-8') as fh:config=yaml.safe_load(fh) or {}
    out.mkdir(parents=True)
    if args.mode=='decision':
        result=pending_decisions(daily,actions,config,args.day,meta['observed_at'])
        result.to_csv(out/'pending_setups.csv',index=False)
        counts={'pending':len(result)}
    else:
        if not args.pending or not args.prior_actions:raise ValueError('PREVIOUS_PENDING_AND_ACTIONS_REQUIRED')
        generated,accepted,rejected=settle_pending(pd.read_csv(args.pending),daily,actions,config,
                        args.day,pd.read_csv(args.master),pd.read_csv(args.symbols),
                        pd.read_csv(args.prior_actions))
        for name,frame in (('setups.csv',generated),('entries_abc.csv',accepted),
                           ('entries_rejected.csv',rejected)):
            frame.to_csv(out/name,index=False)
        counts={'setups':len(generated),'accepted':len(accepted),'rejected':len(rejected)}
    (out/'summary.json').write_text(json.dumps({'mode':args.mode,'day':args.day,
        'snapshot_observed_at':meta['observed_at'],'counts':counts},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(counts,ensure_ascii=False))


if __name__=='__main__':main()
