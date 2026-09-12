#!/usr/bin/env python3
"""对冻结入场运行 E1–E11 日线退出；先检查旧保护线，再更新当日保护线。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

EXIT_IDS = tuple(f'E{i}' for i in range(1, 12))
FIXED_HOLDS = {'E1': 5, 'E2': 10, 'E3': 20, 'E4': 40}
ATR_MULTS = {'E6': 1.5, 'E7': 2.0, 'E8': 2.5, 'E9': 3.0}


def add_atr(frame, period=14):
    d = frame.sort_values('date').copy()
    prev = d['close'].shift(1)
    tr = pd.concat((d['high']-d['low'], (d['high']-prev).abs(),
                    (d['low']-prev).abs()), axis=1).max(axis=1)
    d['atr14'] = tr.rolling(period).mean()
    return d


def _fill_at_stop(open_px, low_px, stop):
    if open_px <= stop: return open_px, 'GAP_STOP'
    if low_px <= stop: return stop, 'STOP'
    return None, None


def _prepare_bars(bars):
    """把 date 规整为 UTC datetime64；已规整则原样返回，避免内层重复解析。"""
    dates = bars['date']
    if not pd.api.types.is_datetime64_any_dtype(dates):
        return bars.assign(date=pd.to_datetime(dates, utc=True))
    if dates.dt.tz is None:
        return bars.assign(date=dates.dt.tz_localize('UTC'))
    if str(dates.dt.tz) != 'UTC':
        return bars.assign(date=dates.dt.tz_convert('UTC'))
    return bars


def _entry_day(value):
    """入场时刻所在的交易日（按 UTC 日历日；实盘中 09:30 ET 与日线 00:00 同日）。"""
    ts = pd.Timestamp(value)
    ts = ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')
    return ts.tz_localize(None).normalize()


def _bar_days(dates):
    """日线日期列统一为无时区的自然日，便于与入场交易日比较。"""
    d = dates.dt.tz_localize(None) if dates.dt.tz is not None else dates
    return d.dt.normalize()


def _session_open_times(dates):
    """把日线交易日映射为该日 09:30 ET 的 UTC 时刻，与入场时刻口径一致。"""
    et = _bar_days(dates).dt.tz_localize('America/New_York') + pd.Timedelta(hours=9, minutes=30)
    return et.dt.tz_convert('UTC').to_numpy()


def _session_close_times(dates):
    """日线收盘与盘中触线均在收盘时释放组合仓位（保守口径）。"""
    et = _bar_days(dates).dt.tz_localize('America/New_York') + pd.Timedelta(hours=16)
    return et.dt.tz_convert('UTC').to_numpy()


def _simulate_core(entry, exit_id, opens, highs, lows, closes, open_times, close_times, atrs, n):
    """退出路径：与成本无关；先检查旧保护线，再更新当日保护线。"""
    ep = float(entry['entry_price']); raw_stop = entry.get('initial_stop')
    initial = float(raw_stop) if pd.notna(raw_stop) and float(raw_stop) > 0 else ep*.95
    stop = initial; high = ep; structure_stop = initial
    exit_px = exit_time = reason = None; mfe = 0.; mae = 0.
    for i in range(n):
        bh = highs[i]; bl = lows[i]
        mfe = max(mfe, bh / ep - 1); mae = min(mae, bl / ep - 1)
        if exit_id not in FIXED_HOLDS:
            exit_px, reason = _fill_at_stop(opens[i], bl, stop)
            if exit_px is not None:
                exit_time = open_times[i] if reason == 'GAP_STOP' else close_times[i]
                break
        if exit_id in FIXED_HOLDS and i + 1 >= FIXED_HOLDS[exit_id]:
            exit_px, exit_time, reason = closes[i], close_times[i], 'TIME_EXIT'; break
        if exit_id == 'E5' and i + 1 >= 20:
            exit_px, exit_time, reason = closes[i], close_times[i], 'TIME_EXIT'; break
        # 今日完成后才更新，下一交易日生效。
        if bh > high: high = bh
        atr = atrs[i]
        if exit_id in ATR_MULTS and np.isfinite(atr):
            stop = max(stop, high - ATR_MULTS[exit_id] * atr)
        if exit_id in ('E10', 'E11') and i >= 4:
            pivot_i = i - 2
            if (lows[pivot_i] < lows[pivot_i-2:pivot_i].min() and
                    lows[pivot_i] < lows[pivot_i+1:pivot_i+3].min()):
                structure_stop = max(structure_stop, lows[pivot_i])
            stop = structure_stop
            if exit_id == 'E11' and np.isfinite(atr): stop = max(stop, high - 2 * atr)
        if i == n - 1:
            exit_time, reason = close_times[i], 'DATA_END'
    gross = exit_px / ep - 1 if exit_px is not None else None
    return {'exit_method': exit_id, 'exit_time': exit_time, 'exit_price': exit_px,
            'exit_reason': reason, 'mfe_pct': mfe, 'mae_pct': mae,
            'gross_pnl_pct': gross, 'data_quality': 'right_censored' if reason == 'DATA_END' else 'good',
            'mark_price': closes[n-1] if reason == 'DATA_END' else None}


def _finalize(result, cost_pct, position_usd):
    gross = result.get('gross_pnl_pct')
    result['cost_pct'] = cost_pct
    result['net_pnl_pct'] = gross - cost_pct if gross is not None else None
    result['net_pnl_usd'] = (gross-cost_pct)*position_usd if gross is not None else None
    return result


def simulate_daily(entry, bars, exit_id, cost_pct=.002):
    d = _prepare_bars(bars)
    entry_day = _entry_day(entry['entry_time'])
    d = d[_bar_days(d['date']) >= entry_day].head(40).reset_index(drop=True)
    if d.empty:
        return {'data_quality': 'missing_future_bars', 'exit_method': exit_id}
    result = _simulate_core(
        entry, exit_id,
        d['open'].to_numpy(float), d['high'].to_numpy(float), d['low'].to_numpy(float),
        d['close'].to_numpy(float),
        _session_open_times(d['date']), _session_close_times(d['date']),
        d['atr14'].to_numpy(float) if 'atr14' in d.columns else np.full(len(d), np.nan),
        len(d))
    return _finalize(result, cost_pct, float(entry.get('position_usd', 5000)))


def _stock_arrays(g):
    """按股票预计算 numpy 数组，避免内层循环重复解析时间与索引。"""
    return {'open': g['open'].to_numpy(float), 'high': g['high'].to_numpy(float),
            'low': g['low'].to_numpy(float), 'close': g['close'].to_numpy(float),
            'atr14': g['atr14'].to_numpy(float) if 'atr14' in g.columns else np.full(len(g), np.nan),
            'day': _bar_days(g['date']).astype('int64').to_numpy(),
            'open_times': _session_open_times(g['date']),
            'close_times': _session_close_times(g['date'])}


def run_exit_matrix(entries, daily_bars, costs=(.001,.002,.005,.01)):
    daily = daily_bars.copy(); daily['date'] = pd.to_datetime(daily['date'], utc=True)
    enriched = [add_atr(g).assign(stock=code) for code,g in daily.groupby('stock')]
    daily = pd.concat(enriched, ignore_index=True) if enriched else daily
    prep = {code: _stock_arrays(g.reset_index(drop=True)) for code, g in daily.groupby('stock')}
    missing = {'data_quality': 'missing_future_bars'}
    rows=[]
    for _, entry in entries.iterrows():
        base=entry.to_dict(); position_usd=float(entry.get('position_usd',5000))
        p=prep.get(entry.stock)
        start=end=0
        if p is not None:
            # 含成交当日：从入场交易日开始的 40 个交易日。
            start=int(np.searchsorted(p['day'], _entry_day(entry['entry_time']).value, side='left'))
            end=min(start+40, len(p['day']))
        if p is None or end-start <= 0:
            for exit_id in EXIT_IDS:
                for cost in costs:
                    rows.append(dict(base, cost_scenario=cost, exit_method=exit_id, **missing))
            continue
        sl=slice(start,end)
        opens=p['open'][sl]; highs=p['high'][sl]; lows=p['low'][sl]
        closes=p['close'][sl]; atrs=p['atr14'][sl]
        open_times=p['open_times'][sl]; close_times=p['close_times'][sl]
        n=end-start
        for exit_id in EXIT_IDS:
            # 退出路径与成本无关：只模拟一次，再按成本推导净收益。
            result=_simulate_core(entry, exit_id, opens, highs, lows, closes, open_times, close_times, atrs, n)
            gross=result.get('gross_pnl_pct')
            for cost in costs:
                row=dict(base, cost_scenario=cost, **result)
                row['cost_pct']=cost
                row['net_pnl_pct']=gross-cost if gross is not None else None
                row['net_pnl_usd']=(gross-cost)*position_usd if gross is not None else None
                rows.append(row)
    return apply_matrix_portfolio(pd.DataFrame(rows))


def apply_matrix_portfolio(matrix: pd.DataFrame, max_positions: int = 3) -> pd.DataFrame:
    """每个 experiment/exit/cost 使用实际退出时间独立执行三仓约束。"""
    if matrix.empty: return matrix
    out=[]
    for _, group in matrix.groupby(['experiment','exit_method','cost_scenario'], dropna=False):
        g=group.copy()
        if 'portfolio_rank' not in g: g['portfolio_rank']=999999
        g['_entry']=pd.to_datetime(g['entry_time'],utc=True)
        g['_exit']=pd.to_datetime(g['exit_time'],utc=True,errors='coerce')
        g=g.sort_values(['_entry','portfolio_rank','stock','setup_id']).reset_index(drop=True)
        entry_ns=g['_entry'].astype('int64').to_numpy()
        exit_ns=g['_exit'].astype('int64').to_numpy()  # NaT -> INT64_MIN，永不大于入场时刻
        quality=g['data_quality'].to_numpy() if 'data_quality' in g else np.array(['']*len(g),dtype=object)
        accepted=np.zeros(len(g),dtype=bool)
        reason=np.empty(len(g),dtype=object); reason[:]=''
        active=[]
        for i in range(len(g)):
            now=entry_ns[i]
            active=[end for end in active if end>now]
            if quality[i]!='good':
                reason[i]='DATA_QUALITY'
            elif len(active)>=max_positions:
                reason[i]='MAX_POSITIONS'
            else:
                accepted[i]=True; active.append(exit_ns[i])
        g['portfolio_accepted']=accepted; g['portfolio_reject_reason']=reason
        out.append(g.drop(columns=['_entry','_exit']))
    return pd.concat(out, ignore_index=True)


def main():
    p=argparse.ArgumentParser(description='运行 E1-E11 日线退出矩阵')
    p.add_argument('--manifest',required=True);p.add_argument('--entries',required=True);p.add_argument('--daily',required=True)
    p.add_argument('--output',required=True)
    p.add_argument('--groups',default='ABCD',
                   help='实验分组，A/B/C/D 的有序子集，默认 ABCD（例如 ABC）')
    args=p.parse_args()
    import json
    from scripts.experiment_manifest import (validate_manifest, parse_groups,
                                             check_groups_consistent)
    try:
        groups=parse_groups(args.groups)
    except ValueError as exc:
        raise SystemExit(str(exc))
    manifest=json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    errors=validate_manifest(manifest)
    if errors:raise SystemExit('manifest 无效: '+','.join(errors))
    check_groups_consistent(groups,manifest)
    entries=pd.read_csv(args.entries)
    if 'experiment' not in entries:
        raise SystemExit('entries 缺少 experiment 列')
    entries=entries[entries.experiment.isin(groups)]
    missing=set(groups)-set(entries.experiment)
    if missing:
        raise SystemExit(f'entries 缺少实验分组: {",".join(sorted(missing))}')
    out=run_exit_matrix(entries,pd.read_csv(args.daily))
    target=Path(args.output)
    if target.exists(): raise FileExistsError(f'禁止覆盖实验产物: {target}')
    target.parent.mkdir(parents=True,exist_ok=True);out.to_csv(target,index=False)
    print(f'wrote {len(out)} rows to {target} (groups={",".join(groups)})');return 0


if __name__=='__main__': raise SystemExit(main())
