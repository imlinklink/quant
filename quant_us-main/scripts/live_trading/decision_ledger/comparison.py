"""Offline entry-selection experiment: common-clock A/B, human-clock C, actual D.

All simulated layers use one cash/risk constrained executor and identical exits.
Defer/reduce actions are observation-only until an explicit policy is validated.
No execution service or model calls are made by this module.
"""
import argparse
import json
import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from scripts.run_strategy_validation import run_portfolio
from scripts.live_trading.decision_ledger.event_store import digest, utc
from scripts.live_trading.decision_ledger.funnel_report import build_funnel


def replay(events, bars, risk_config, experiment):
    for field in ('experiment_id','account_scope','decision_delay_seconds','entry_ttl_seconds','exit_approval_delay_bars','defer_policy'):
        if field not in experiment:
            raise ValueError('实验缺少预先固定的参数: '+field)
    if experiment['defer_policy'] != 'observe' or experiment['decision_delay_seconds'] < 0 or experiment['entry_ttl_seconds'] <= 0:
        raise ValueError('首版暂缓/减仓只单列观察；时钟/有效期必须有效')
    # Deduplicate exports before interpreting any decisions.
    build_funnel(events)
    events = sorted({e['event_id']:e for e in events if e.get('schema_version')==1 and
                     e.get('account_scope')==experiment['account_scope']}.values(),
                    key=lambda e:(e['observed_at'],e['event_id']))
    bars=bars.copy()
    if bars.empty:
        raise ValueError('缺少可重放的OHLC行情')
    # Naive timestamps are not silently interpreted as UTC.
    bars['date']=[utc(v) for v in bars['date']]
    bars['date']=pd.to_datetime(bars.date,utc=True)
    if bars.duplicated(['code','date']).any():
        raise ValueError('行情存在重复K线')
    for row in bars.to_dict('records'):
        if not all(math.isfinite(float(row[c])) and float(row[c])>0 for c in ('open','high','low','close')) or not (
            row['low'] <= min(row['open'],row['close']) <= max(row['open'],row['close']) <= row['high']):
            raise ValueError('OHLC行情无效')
    end = bars.date.max()+pd.Timedelta(minutes=15)
    events = [e for e in events if pd.Timestamp(e['observed_at']) <= end]
    candidates = {e['signal_id']:e for e in events if e['event_type']=='rule_candidate' and e['payload']['timeframe']!='event'}
    layers={'A':[], 'B':[], 'C':[]}
    groups=[]; gaps=[]; forward=[]
    for sid, event in sorted(candidates.items()):
        p=event['payload']; details=p['details']; related=[e for e in events if e.get('signal_id')==sid]
        if not all(details.get(k) is not None for k in ('price','initial_stop')) or not p.get('risk_group'):
            gaps.append({'signal_id':sid,'reason':'missing_frozen_rule_price_stop_or_group'})
            continue
        observed=pd.Timestamp(event['observed_at'])
        signal_time=pd.Timestamp(p['signal_bar_end'])
        clock=max(observed,signal_time)+pd.Timedelta(seconds=experiment['decision_delay_seconds'])
        if clock>end:
            groups.append({'signal_id':sid,'llm':'pending_clock','human':'unhandled'})
            continue
        row=dict(code=p['stock_code'],strategy=p['strategy'],signal_id=sid,signal_time=signal_time,
                 approved_at=clock,price=float(details['price']),initial_stop=float(details['initial_stop']),
                 risk_group=p['risk_group'],expires_at=clock+pd.Timedelta(seconds=experiment['entry_ttl_seconds']))
        for k in ('target','min_rr','breakout_level','signal_atr','max_chase_atr','failure_sessions','time_exit_bars'):
            if details.get(k) is not None:row[k]=details[k]
        layers['A'].append(row)
        reviews=[e for e in related if e['event_type'] in ('llm_completed','llm_failed') and pd.Timestamp(e['observed_at'])<=clock]
        r=reviews[-1]['payload'] if reviews else None
        verdict='missing_or_pending'
        if r:
            valid=r.get('status')=='complete' and r.get('expires_at',0)>=clock.timestamp() and not r.get('plan_change_requested')
            verdict=r.get('recommendation') if valid else r.get('status','failed')
            if valid and r.get('recommendation')=='support_execute' and r.get('proposed_action')=='buy':
                layers['B'].append(dict(row))
        humans=[e for e in related if e['event_type']=='human_decision']
        approved=next((e for e in humans if e['payload'].get('status')=='approved'),None)
        human='unhandled'
        if approved:
            human='approved'
            approval_clock=pd.Timestamp(approved['observed_at'])
            # Revisions change the experiment's frozen exit; keep them in a separate group.
            if approved.get('plan_version')!=1:
                gaps.append({'signal_id':sid,'reason':'revised_plan_not_comparable_in_entry_experiment'})
            else:
                layers['C'].append(dict(row,approved_at=approval_clock,
                    expires_at=approval_clock+pd.Timedelta(seconds=experiment['entry_ttl_seconds'])))
        elif humans:
            human=humans[-1]['payload'].get('status','unhandled')
        groups.append({'signal_id':sid,'llm':verdict,'human':human})
        # Independent signal diagnostics, never summed into portfolio return.
        future=bars[(bars.code==p['stock_code'])&(bars.date>clock)].sort_values('date')
        if len(future):
            price=float(future.iloc[0]['open'])
            sessions=future.date.dt.tz_convert('America/New_York').dt.date
            dates=sorted(sessions.unique())
            for horizon in (1,3,5):
                if len(dates)<horizon:continue
                window=future[sessions<=dates[horizon-1]]
                last=window.iloc[-1]
                # Require a full regular session through 16:00 ET; incomplete horizons stay missing.
                if (last['date']+pd.Timedelta(minutes=15)).tz_convert('America/New_York').hour<16:
                    continue
                forward.append(dict(signal_id=sid,horizon_sessions=horizon,
                    return_pct=float(last['close'])/price-1,
                    mfe=float(window.high.max())/price-1,mae=float(window.low.min())/price-1,
                    simulated_r=(float(last['close'])-price-2*float(risk_config.get('cost_per_share',.05)))/(price-float(details['initial_stop']))
                    if price>float(details['initial_stop']) else None))
    columns=['code','strategy','signal_id','signal_time','approved_at','price','initial_stop','risk_group','expires_at']
    results={}
    for layer,rows in layers.items():
        signals=pd.DataFrame(rows) if rows else pd.DataFrame(columns=columns)
        results[layer]=run_portfolio(bars,signals,risk_config,experiment['exit_approval_delay_bars'])
        results[layer]['included_signals']=len(rows)
    funnel=build_funnel(events)
    comparable_trades={e.get('trade_id') for e in events if e['event_type']=='fill_received' and
                       e.get('signal_id') in candidates and e['payload'].get('side')=='buy'} - {None}
    actual=build_funnel([e for e in events if e.get('signal_id') in candidates or e.get('trade_id') in comparable_trades])
    results['D']=actual['actual']
    for layer in ('B','C'):
        results[layer]['final_equity_less_llm_cost'] = (results[layer]['final_equity']-funnel['llm_cost']['known_usd']
            if funnel['llm_cost']['unknown_calls']==0 else None)
    return dict(experiment=experiment,config_hash=digest({'risk':risk_config,'experiment':experiment}),
                events_hash=digest(events),bars_hash=digest(bars.assign(date=bars.date.astype(str)).to_dict('records')),
                layers=results,cross_groups=groups,data_gaps=gaps,forward_diagnostics=forward,
                llm_cost=funnel['llm_cost'],
                assumptions=['仅检验入场选择；各模拟层冻结相同退出规则，退出审批延迟由实验参数指定',
                    'A/B使用固定公共决策时钟；C使用历史人工确认时间；暂缓和减仓单列观察',
                    '下一根可用开盘报价按限价一次尝试，不模拟排队；成本按每股预算，期末按收盘估值',
                    'D仅真实成交账本金额；缺费用/历史风险显示不可计算，不把C/D差额宣称为纯执行因果损耗',
                    '无足够样本与锁定样本外区间时，暂无法判断LLM是否提高收益'])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--events',required=True);p.add_argument('--bars',required=True)
    p.add_argument('--experiment',required=True);p.add_argument('--risk-config',required=True)
    p.add_argument('--output',required=True)
    args=p.parse_args()
    events=[json.loads(line) for line in Path(args.events).read_text().splitlines() if line.strip()]
    result=replay(events,pd.read_csv(args.bars),json.loads(Path(args.risk_config).read_text()),
                  json.loads(Path(args.experiment).read_text()))
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


if __name__=='__main__':
    main()
