#!/usr/bin/env python3
"""从冻结日线逐日生成可用于A/B/C/D的历史Setup，不调用模型。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
import yaml

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from scripts.data.io_utils import read_frame,write_frame
from scripts.live_trading.setup_features import FEATURE_VERSION
from scripts.live_trading.setup_state_machine import build_setup_candidate,transition

ET=ZoneInfo('America/New_York')


def _market_time(session,hour,minute=0):
    return pd.Timestamp(session).tz_localize(ET)+pd.Timedelta(hours=hour,minutes=minute)


def _weekly_series(bars):
    """逐日计算当时可见的周线状态；不使用本周未来日线。"""
    out=[];completed=[];week=None;current=None
    for row in bars.itertuples(index=False):
        label=pd.Timestamp(row.date).to_period('W-FRI')
        if label!=week:
            if current is not None:completed.append(current)
            week=label;current={'open':row.open,'high':row.high,'low':row.low,
                                'close':row.close,'prev_close':completed[-1]['close'] if completed else row.close}
        else:
            current['high']=max(current['high'],row.high);current['low']=min(current['low'],row.low);current['close']=row.close
        weeks=completed+[current];cl=np.array([float(x['close']) for x in weeks])
        tr=np.array([max(float(x['high'])-float(x['low']),abs(float(x['high'])-float(x['prev_close'])),
                         abs(float(x['low'])-float(x['prev_close']))) for x in weeks])
        if len(cl)<40:
            out.append({'weekly_gate':False,'weekly_regime':'falling','weekly_ma10':np.nan,
                        'weekly_ma20':np.nan,'weekly_ma40':np.nan,'weekly_atr14':np.nan,
                        'weekly_ma20_slope_4w':np.nan,'weekly_drawdown_52w':np.nan,
                        'weekly_volatility_contracting':False,'weekly_bar_count':len(cl)});continue
        ma10=cl[-10:].mean();ma20=cl[-20:].mean();ma40=cl[-40:].mean()
        prior20=cl[-24:-4].mean();slope=ma20/prior20-1;atr=tr[-14:].mean()
        prior_atr=tr[-18:-4].mean() if len(tr)>=18 else np.nan
        dd=float(cl[-1]/max(float(x['high']) for x in weeks[-52:])-1)
        trend=cl[-1]>ma40 and slope>0
        prev_ma10=cl[-12:-2].mean();recovering=(dd<=-.10 and cl[-1]>ma10 and ma10>=prev_ma10 and
            (not np.isfinite(prior_atr) or atr<=prior_atr*1.10))
        regime='trend' if trend else ('recovering' if recovering else 'falling')
        out.append({'weekly_gate':bool(trend or recovering),'weekly_regime':regime,
            'weekly_ma10':float(ma10),'weekly_ma20':float(ma20),'weekly_ma40':float(ma40),
            'weekly_atr14':float(atr),'weekly_ma20_slope_4w':float(slope),
            'weekly_drawdown_52w':dd,'weekly_volatility_contracting':bool(np.isfinite(prior_atr) and atr<=prior_atr),
            'weekly_bar_count':len(cl)})
    return out


def _snapshots(bars,cfg):
    """O(n)滚动特征和O(1)状态所需结构，替代逐日重算整张DataFrame。"""
    d=bars.reset_index(drop=True).copy();close=d.close.astype(float);high=d.high.astype(float);low=d.low.astype(float)
    ma20=close.rolling(20).mean();ma50=close.rolling(50).mean();ma200=close.rolling(200).mean()
    prev=close.shift(1);tr=pd.concat([high-low,(high-prev).abs(),(low-prev).abs()],axis=1).max(axis=1);atr=tr.rolling(14).mean()
    vol=d.volume.astype(float);volmean=vol.shift(1).rolling(20).mean();reversal=high.shift(1).rolling(5).max()
    weekly=_weekly_series(d);pivots=[];lookback=int(cfg.get('drawdown_window',60));stable=int(cfg.get('stabilization_days',5))
    for i in range(len(d)):
        if i>=4:
            j=i-2
            if low.iloc[j]<low.iloc[j-2:j].min() and low.iloc[j]<low.iloc[j+1:j+3].min():pivots.append(j)
        if i<200:yield None;continue
        recent=low.iloc[max(0,i-lookback+1):i+1].to_numpy();days_since=len(recent)-1-int(np.argmin(recent))
        swing=float(low.iloc[pivots[-1]]) if pivots else None;prior_swing=float(low.iloc[pivots[-2]]) if len(pivots)>1 else None
        w=weekly[i];features={'close':float(close.iloc[i]),'ma20':float(ma20.iloc[i]),'ma50':float(ma50.iloc[i]),
            'ma200':float(ma200.iloc[i]),'ma20_slope_5d':float(ma20.iloc[i]/ma20.iloc[i-5]-1),
            'ma50_slope_20d':float(ma50.iloc[i]/ma50.iloc[i-20]-1),'atr14':float(atr.iloc[i]),
            'drawdown_60d':float(close.iloc[i]/high.iloc[max(0,i-lookback+1):i+1].max()-1),
            'days_since_low':days_since,'no_new_low':days_since>=stable,'relative_strength_20d':None,
            'market_above_ma200':None,'volume_ratio_20d':float(vol.iloc[i]/volmean.iloc[i]),
            'one_day_return':float(close.iloc[i]/close.iloc[i-1]-1),**w}
        structure={'swing_low':swing,'prior_swing_low':prior_swing,
            'higher_low':bool(swing is not None and prior_swing is not None and swing>prior_swing),
            'reversal_level':float(reversal.iloc[i]),'invalidation_price':swing}
        finite=all(np.isfinite(features[k]) for k in ('close','ma20','ma50','ma200','atr14','weekly_ma40'))
        yield {'feature_version':FEATURE_VERSION,'session':str(d.date.iloc[i].date()),
            'bar_count':i+1,'features':features,'structure':structure,
            'quality':{'status':'pass' if finite else 'fail','reasons':[] if finite else ['NON_FINITE_REQUIRED_FEATURE']}}


def generate(daily:pd.DataFrame,config:dict,llm_labels=None,start=None,end=None,progress=False):
    required={'stock','date','open','high','low','close','volume'}
    if not required.issubset(daily):raise ValueError('daily 缺字段: '+','.join(sorted(required-set(daily))))
    d=daily.copy();d['date']=pd.to_datetime(d.date).dt.tz_localize(None).dt.normalize()
    d=d.sort_values(['stock','date'])
    cfg=dict(config.get('buy_strategy_v2',config));baseline_cfg=dict(cfg,require_weekly_gate=False)
    minimum=int(cfg.get('min_daily_bars',250))
    labels={}
    if llm_labels is not None and not llm_labels.empty:
        labels=dict(zip(llm_labels.setup_id.astype(str),llm_labels.llm_decision.astype(str)))
    rows=[]
    for code,bars in d.groupby('stock'):
        if code=='US.SPY':continue
        bars=bars.reset_index(drop=True);state='FALLING';snapshots=_snapshots(bars,baseline_cfg)
        for i,snapshot in enumerate(snapshots):
            if snapshot is None or i<minimum-1 or i>=len(bars)-1:continue
            session=bars.date.iloc[i]
            if end and session>pd.Timestamp(end):break
            as_of=_market_time(session,16).tz_convert('UTC')
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
        if progress:print(f'processed {code}: bars={len(bars)} setups={sum(r["stock"]==code for r in rows)}',flush=True)
    return pd.DataFrame(rows)


def main():
    p=argparse.ArgumentParser(description='从冻结日线生成历史周线/日线Setup')
    p.add_argument('--daily',required=True);p.add_argument('--config',required=True);p.add_argument('--llm-labels')
    p.add_argument('--start');p.add_argument('--end');p.add_argument('--output',required=True);args=p.parse_args()
    with open(args.config,encoding='utf-8') as fh:cfg=yaml.safe_load(fh) or {}
    labels=read_frame(args.llm_labels) if args.llm_labels else None
    out=generate(read_frame(args.daily),cfg,labels,args.start,args.end,progress=True)
    write_frame(out,args.output);print(f'wrote {len(out)} historical setups to {args.output}')


if __name__=='__main__':main()
