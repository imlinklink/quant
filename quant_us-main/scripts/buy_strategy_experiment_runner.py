#!/usr/bin/env python3
"""生成周线/日线/LLM A-B-C-D 入场实验；全部按 T+1 固定规则成交。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

GROUP_LABELS = {'A':'daily_setup_next_open','B':'weekly_gate_daily_setup_next_open',
                'C':'weekly_gate_daily_confirmation_next_open',
                'D':'weekly_daily_llm_filter_next_open'}
LLM_ACCEPT = {'candidate','support','support_execute'}


def build_abcd_entries(setups: pd.DataFrame, max_gap_atr=.75) -> pd.DataFrame:
    """同一 setup 形成嵌套实验组；依次增加周线门、日线确认和 LLM。"""
    required={'setup_id','stock','setup_time','next_open_time','next_open_price',
              'initial_stop','signal_close','atr14','weekly_gate','daily_confirmed','llm_decision'}
    if not required.issubset(setups):raise ValueError('setups 缺字段: '+','.join(sorted(required-set(setups))))
    s=setups.copy()
    for column in ('setup_time','next_open_time'):s[column]=pd.to_datetime(s[column],utc=True)
    if (s.next_open_time<=s.setup_time).any():raise ValueError('LOOKAHEAD_EXECUTION: next_open_time 必须晚于 setup_time')
    s['weekly_gate']=s.weekly_gate.astype(bool);s['daily_confirmed']=s.daily_confirmed.astype(bool)
    s['llm_decision']=s.llm_decision.astype(str).str.lower()
    executable=(s.next_open_price>s.initial_stop)&(s.next_open_price<=s.signal_close+float(max_gap_atr)*s.atr14)
    masks={'A':executable,'B':executable&s.weekly_gate,
           'C':executable&s.weekly_gate&s.daily_confirmed,
           'D':executable&s.weekly_gate&s.daily_confirmed&s.llm_decision.isin(LLM_ACCEPT)}
    frames=[]
    for group,mask in masks.items():
        item=s[mask].copy();item['experiment']=group;item['entry_method']=GROUP_LABELS[group]
        item['entry_time']=item.next_open_time;item['entry_price']=item.next_open_price;item['entry_id']=item.setup_id
        frames.append(item)
    out=pd.concat(frames,ignore_index=True,sort=False)
    return out.sort_values(['entry_time','experiment','stock','setup_id']).reset_index(drop=True)


def apply_universe(entries: pd.DataFrame, universe: pd.DataFrame):
    if entries.empty:return entries.copy(),entries.copy()
    u=universe.copy()
    if not {'universe_date','code','eligible'}.issubset(u):raise ValueError('universe 需要 universe_date/code/eligible')
    u['universe_date']=pd.to_datetime(u.universe_date).dt.date
    e=entries.copy();e['universe_date']=pd.to_datetime(e.entry_time,utc=True).dt.date
    joined=e.merge(u[['universe_date','code','eligible','quality','reason']],left_on=['universe_date','stock'],right_on=['universe_date','code'],how='left')
    joined['eligible']=joined.eligible.fillna(False).astype(bool)
    joined['portfolio_reject_reason']=joined.reason.fillna('NOT_IN_POINT_IN_TIME_UNIVERSE')
    return joined[joined.eligible].copy(),joined[~joined.eligible].copy()


def write_new(path,frame):
    p=Path(path)
    if p.exists():raise FileExistsError(f'禁止覆盖实验产物: {p}')
    p.parent.mkdir(parents=True,exist_ok=True);frame.to_csv(p,index=False)


def main():
    p=argparse.ArgumentParser(description='生成周线/日线/LLM A-B-C-D 冻结入场集合')
    p.add_argument('--manifest',required=True);p.add_argument('--setups',required=True)
    p.add_argument('--universe',required=True);p.add_argument('--output-dir',required=True)
    p.add_argument('--max-gap-atr',type=float,default=.75);args=p.parse_args()
    from scripts.experiment_manifest import validate_manifest
    manifest=json.loads(Path(args.manifest).read_text(encoding='utf-8'));errors=validate_manifest(manifest)
    if errors:raise SystemExit('manifest 无效: '+','.join(errors))
    accepted,rejected=apply_universe(build_abcd_entries(pd.read_csv(args.setups),args.max_gap_atr),pd.read_csv(args.universe))
    out=Path(args.output_dir);write_new(out/'signals.csv',accepted);write_new(out/'rejected_signals.csv',rejected)
    print(json.dumps({'signals':len(accepted),'rejected':len(rejected)},ensure_ascii=False));return 0


if __name__=='__main__':raise SystemExit(main())
