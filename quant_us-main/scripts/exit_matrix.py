#!/usr/bin/env python3
"""对冻结入场运行 E1–E12；先检查旧保护线，再更新当日保护线。"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

EXIT_IDS = tuple(f'E{i}' for i in range(1, 13))
FIXED_HOLDS = {'E1': 5, 'E2': 10, 'E3': 20, 'E4': 40}
ATR_MULTS = {'E6': 1.5, 'E7': 2.0, 'E8': 2.5, 'E9': 3.0}


def add_atr(frame, period=14):
    d = frame.sort_values('date').copy()
    prev = d['close'].shift(1)
    tr = pd.concat((d['high']-d['low'], (d['high']-prev).abs(),
                    (d['low']-prev).abs()), axis=1).max(axis=1)
    d['atr14'] = tr.rolling(period).mean()
    return d


def _fill_at_stop(bar, stop):
    if float(bar['open']) <= stop: return float(bar['open']), 'GAP_STOP'
    if float(bar['low']) <= stop: return float(stop), 'STOP'
    return None, None


def simulate_daily(entry, bars, exit_id, cost_pct=.002):
    entry_time = pd.Timestamp(entry['entry_time'])
    entry_time = entry_time.tz_localize('UTC') if entry_time.tzinfo is None else entry_time.tz_convert('UTC')
    d = bars[pd.to_datetime(bars['date'], utc=True) >= entry_time].copy()
    d = d.head(40).reset_index(drop=True)
    if d.empty:
        return {'data_quality': 'missing_future_bars', 'exit_method': exit_id}
    ep = float(entry['entry_price']); raw_stop = entry.get('initial_stop')
    initial = float(raw_stop) if pd.notna(raw_stop) and float(raw_stop) > 0 else ep*.95
    stop = initial; high = ep; structure_stop = initial
    exit_px = exit_time = reason = None; mfe = 0.; mae = 0.
    for i, bar in d.iterrows():
        mfe = max(mfe, float(bar['high']) / ep - 1); mae = min(mae, float(bar['low']) / ep - 1)
        if exit_id not in FIXED_HOLDS:
            exit_px, reason = _fill_at_stop(bar, stop)
            if exit_px is not None: exit_time = bar['date']; break
        if exit_id in FIXED_HOLDS and i + 1 >= FIXED_HOLDS[exit_id]:
            exit_px, exit_time, reason = float(bar['close']), bar['date'], 'TIME_EXIT'; break
        if exit_id == 'E5' and i + 1 >= 20:
            exit_px, exit_time, reason = float(bar['close']), bar['date'], 'TIME_EXIT'; break
        # 今日完成后才更新，下一交易日生效。
        high = max(high, float(bar['high']))
        atr = float(bar.get('atr14', np.nan))
        if exit_id in ATR_MULTS and np.isfinite(atr):
            stop = max(stop, high - ATR_MULTS[exit_id] * atr)
        if exit_id in ('E10', 'E11') and i >= 4:
            pivot_i = i - 2
            lows = d['low']
            if (float(lows.iloc[pivot_i]) < float(lows.iloc[pivot_i-2:pivot_i].min()) and
                    float(lows.iloc[pivot_i]) < float(lows.iloc[pivot_i+1:pivot_i+3].min())):
                structure_stop = max(structure_stop, float(lows.iloc[pivot_i]))
            stop = structure_stop
            if exit_id == 'E11' and np.isfinite(atr): stop = max(stop, high - 2 * atr)
        if i == len(d)-1:
            exit_px, exit_time, reason = float(bar['close']), bar['date'], 'DATA_END'
    gross = exit_px / ep - 1 if exit_px is not None else None
    return {'exit_method': exit_id, 'exit_time': exit_time, 'exit_price': exit_px,
            'exit_reason': reason, 'mfe_pct': mfe, 'mae_pct': mae,
            'gross_pnl_pct': gross, 'cost_pct': cost_pct,
            'net_pnl_pct': gross - cost_pct if gross is not None else None,
            'net_pnl_usd': (gross-cost_pct)*float(entry.get('position_usd', 5000))
            if gross is not None else None, 'data_quality': 'good'}


def simulate_intraday_e12(entry, bars, cost_pct=.002):
    if bars is None or bars.empty:
        return {'exit_method': 'E12', 'data_quality': 'missing_intraday_bars'}
    entry_time=pd.Timestamp(entry['entry_time'])
    entry_time=entry_time.tz_localize('UTC') if entry_time.tzinfo is None else entry_time.tz_convert('UTC')
    d = bars[pd.to_datetime(bars['date'], utc=True) >= entry_time].head(8)
    if d.empty: return {'exit_method': 'E12', 'data_quality': 'missing_intraday_bars'}
    ep=float(entry['entry_price']);raw_stop=entry.get('initial_stop')
    stop=float(raw_stop) if pd.notna(raw_stop) and float(raw_stop)>0 else ep*.95
    mfe = mae = 0.
    for _, bar in d.iterrows():
        mfe=max(mfe,float(bar.high)/ep-1); mae=min(mae,float(bar.low)/ep-1)
        px, reason = _fill_at_stop(bar, stop)
        if px is not None:
            when=bar.date; break
    else:
        bar=d.iloc[-1]; px=float(bar.close); when=bar.date; reason='TIME_EXIT'
    gross=px/ep-1
    return {'exit_method':'E12','exit_time':when,'exit_price':px,'exit_reason':reason,
            'mfe_pct':mfe,'mae_pct':mae,'gross_pnl_pct':gross,'cost_pct':cost_pct,
            'net_pnl_pct':gross-cost_pct,
            'net_pnl_usd':(gross-cost_pct)*float(entry.get('position_usd',5000)),
            'data_quality':'good'}


def run_exit_matrix(entries, daily_bars, intraday_bars=None, costs=(.001,.002,.005,.01)):
    daily = daily_bars.copy(); daily['date'] = pd.to_datetime(daily['date'], utc=True)
    intra = intraday_bars.copy() if intraday_bars is not None else None
    if intra is not None: intra['date'] = pd.to_datetime(intra['date'], utc=True)
    enriched = [add_atr(g).assign(stock=code) for code,g in daily.groupby('stock')]
    daily = pd.concat(enriched, ignore_index=True) if enriched else daily
    rows=[]
    for _, entry in entries.iterrows():
        db=daily[daily.stock==entry.stock]
        ib=intra[intra.stock==entry.stock] if intra is not None else None
        for cost in costs:
            for exit_id in EXIT_IDS:
                result=(simulate_intraday_e12(entry,ib,cost) if exit_id=='E12'
                        else simulate_daily(entry,db,exit_id,cost))
                rows.append(dict(entry.to_dict(), cost_scenario=cost, **result))
    return apply_matrix_portfolio(pd.DataFrame(rows))


def apply_matrix_portfolio(matrix: pd.DataFrame, max_positions: int = 3) -> pd.DataFrame:
    """每个 experiment/exit/cost 使用实际退出时间独立执行三仓约束。"""
    if matrix.empty: return matrix
    out=[]
    for _, group in matrix.groupby(['experiment','exit_method','cost_scenario'], dropna=False):
        g=group.copy();g['_entry']=pd.to_datetime(g['entry_time'],utc=True)
        g['_exit']=pd.to_datetime(g['exit_time'],utc=True,errors='coerce')
        if 'portfolio_rank' not in g:g['portfolio_rank']=999999
        g=g.sort_values(['_entry','portfolio_rank','stock','setup_id'])
        active=[]
        for idx,row in g.iterrows():
            active=[end for end in active if pd.notna(end) and end>row['_entry']]
            item=row.drop(labels=['_entry','_exit']).to_dict()
            if row['data_quality']!='good':
                item['portfolio_accepted']=False;item['portfolio_reject_reason']='DATA_QUALITY'
            elif len(active)>=max_positions:
                item['portfolio_accepted']=False;item['portfolio_reject_reason']='MAX_POSITIONS'
            else:
                item['portfolio_accepted']=True;item['portfolio_reject_reason']=''
                active.append(row['_exit'])
            out.append(item)
    return pd.DataFrame(out)


def main():
    p=argparse.ArgumentParser(description='运行 E1-E12 退出矩阵')
    p.add_argument('--manifest',required=True);p.add_argument('--entries',required=True);p.add_argument('--daily',required=True)
    p.add_argument('--intraday');p.add_argument('--output',required=True)
    args=p.parse_args()
    import json
    from scripts.experiment_manifest import validate_manifest
    manifest=json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    errors=validate_manifest(manifest)
    if errors:raise SystemExit('manifest 无效: '+','.join(errors))
    entries=pd.read_csv(args.entries)
    if set(entries.get('experiment',[]))!=set('ABCD'):
        raise SystemExit('entries 必须包含 A/B/C/D 四组')
    out=run_exit_matrix(entries,pd.read_csv(args.daily),
        pd.read_csv(args.intraday) if args.intraday else None)
    target=Path(args.output)
    if target.exists(): raise FileExistsError(f'禁止覆盖实验产物: {target}')
    target.parent.mkdir(parents=True,exist_ok=True);out.to_csv(target,index=False)
    print(f'wrote {len(out)} rows to {target}');return 0


if __name__=='__main__': raise SystemExit(main())
