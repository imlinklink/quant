"""Registered H-A opportunity experiment; no portfolio or live writes."""
import argparse
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
import sqlite3
import json

import numpy as np
import pandas as pd
from scripts.portfolio_shadow.cli import manifest_from_dict
from scripts.portfolio_shadow.paper_engine import new_account_state, step
from scripts.portfolio_shadow.schema import Opportunity, to_micro
from .manifest import read, file_hash, write_json, code_hashes
from .inputs import load
from .experiments import shadow_actions


def block_interval(values, dates, calendar, length, seed=20260921, statistic='mean'):
    values = np.asarray(values, float)
    ids = [int(calendar.searchsorted(pd.Timestamp(d))) // length for d in dates]
    nblocks = int(calendar.searchsorted(pd.Timestamp(max(dates)))) // length + 1
    groups = [np.flatnonzero(np.asarray(ids) == i) for i in range(nblocks)]
    rng = np.random.default_rng(seed)
    estimates = []
    for _ in range(2000):
        ix = np.concatenate([groups[i] for i in rng.integers(0, nblocks, nblocks)])
        if len(ix):
            estimates.append(float(np.median(values[ix]) if statistic == 'median' else np.mean(values[ix])))
    return np.quantile(estimates, [.025,.975]).tolist() if estimates else [None,None]


def distribution(rows):
    if not rows:
        return {'count':0,'net_r_sum':0,'stop_rate':None,'below_minus_2r_rate':None,'worst5':None}
    values=np.array([r['net_r'] for r in rows])
    return {'count':len(rows),'net_r_sum':float(values.sum()),'net_r_mean':float(values.mean()),
        'net_r_quantiles':np.quantile(values,[0,.05,.5,.95,1]).tolist(),
        'mae_r_quantiles':np.quantile([r['mae_r'] for r in rows],[0,.5,.95,1]).tolist(),
        'mae_pct_quantiles':np.quantile([r['mae_pct'] for r in rows],[0,.5,.95,1]).tolist(),
        'stop_rate':sum(r['reason'] in ('STOP','GAP_STOP') for r in rows)/len(rows),
        'gap_stops':sum(r['reason']=='GAP_STOP' for r in rows),
        'below_minus_1r':int(sum(values < -1)), 'below_minus_2r':int(sum(values < -2)),
        'below_minus_2r_rate':float(np.mean(values < -2)), 'worst5':float(np.sort(values)[:5].sum())}


def concentration(rows, field):
    grouped=defaultdict(float)
    for row in rows: grouped[row['security_id']]+=row[field]
    ordered=sorted(grouped.items(),key=lambda kv:kv[1],reverse=True)
    total=sum(grouped.values())
    return {'by_security':dict(ordered),'top1_share':ordered[0][1]/total if ordered and total>0 else None,
        'leave_one_out':total-ordered[0][1] if ordered else None,
        'top2_share':sum(v for _,v in ordered[:2])/total if ordered and total>0 else None}


def candidate_span(calendar, rounds):
    """候选轮次的跨度（session 数）。

    用它而不是整段参考日历：后者在研究窗口里有 2932 个 session，"≥60 交易日"的门槛恒真、
    形同虚设 —— 门槛必须是**被评价的那一段**才有意义。
    """
    first,last=pd.Timestamp(min(rounds)),pd.Timestamp(max(rounds))
    return int(calendar.searchsorted(last)-calendar.searchsorted(first))+1


def reconcile_fills(archived_fills, a_index, round_by_execution, cutoff):
    """归档成交 vs 重建的 A 入场：**双向**核对，返回分类计数。

    对得上的逐字段比（入场价与止损）；对不上的**必须**由预登记的候选轮次截止解释
    （轮次 > cutoff ⇒ 本来就不在本次总体里）。轮次在截止内却对不上 = 重建漏了一笔真成交
    ⇒ 立即失败。单向核对（"对得上才比"）会让这种缺口静默通过，而它正是本控制要防的事。
    """
    out={'archived':len(archived_fills),'matched':0,'excused_by_cutoff':0,'excused':[]}
    for sid,day,price,shares,stop in archived_fills:
        r=a_index.get((sid,day))
        if r is None:
            cid=round_by_execution.get((sid,day))
            if cid is None:raise ValueError(f'BASELINE_FILL_UNKNOWN_ROUND:{sid}:{day}')
            if cid<=cutoff:
                raise ValueError(f'BASELINE_FILL_NOT_RECONSTRUCTED:{sid}:{day}:{cid}')
            out['excused_by_cutoff']+=1;out['excused'].append([sid,day,cid]);continue
        if (price,stop)!=(r['entry_price_micro'],r['stop_micro']):
            raise ValueError(f'BASELINE_ENTRY_OR_STOP_MISMATCH:{sid}:{day}')
        out['matched']+=1
    return out


def verdict(metrics):
    g=metrics['gates']
    if not g['sample']: return 'INSUFFICIENT_SAMPLE'
    if metrics['delta_mean']<=0: return 'NO_IMPROVEMENT'
    if not all(g[k] for k in ('stop_rate','tail','worst5','mae')): return 'RISK_REJECTED'
    if not g['concentration']: return 'CONCENTRATED'
    if not g['experimental_sample']: return 'INSUFFICIENT_SAMPLE'
    if not g['experimental_concentration']: return 'CONCENTRATED'
    if not g['interval'] or not g['stress']: return 'INCONCLUSIVE'
    return 'SUPPORTED'


def simulate(sid, cid, signal, prices, calendar, acts, manifest, fee):
    signal=pd.Timestamp(signal)
    start=int(calendar.searchsorted(signal))+1
    dates=calendar[start:start+60]
    if len(dates)!=60: raise ValueError('INCOMPLETE_COMMON_HORIZON')
    stock=prices[sid]
    if any(d not in stock.index for d in dates): raise ValueError('MISSING_REFERENCE_SESSION')
    atr=stock.loc[signal,'asof_atr']*stock.loc[signal,'scale_to_next']
    if not np.isfinite(atr) or atr<=0: raise ValueError('ATR_UNAVAILABLE')
    scope='SHADOW:ha:R'
    m=replace(manifest,experiment_id='ha',account_scopes=(scope,),initial_cash=to_micro(100000))
    o=Opportunity(experiment_id='ha',security_id=sid,source_candidate_id=cid,parent_version=m.parent_version,
        signal_session=str(signal.date()),observed_at=str(signal.date())+'T23:00:00Z',
        planned_execution_session=str(dates[0].date()),rank=1,entry_rule='b3',
        stop_reference={'atr14_micro':to_micro(atr)},exit_policy_id='H60',input_hash='')
    state=new_account_state(scope,m.initial_cash)
    qty=0; dividends=0; mae=0; buy=None
    for day in dates:
        date=str(day.date()); bar={k:to_micro(stock.loc[day,'raw_'+k]) for k in ('open','high','low','close')}
        result=step(state,session=date,bars={sid:bar},corporate_actions=acts.get((sid,date),[]),
                    intents=[o] if day==dates[0] else [],manifest=m,fee_bp=fee)
        sell=None
        for e in result.events:
            if e['type']=='fill' and e['side']=='BUY': buy=e;qty=e['shares']
            elif e['type']=='split': qty=qty*e['ratio'] if e['kind']=='split' else qty//e['ratio']
            elif e['type']=='dividend_record': dividends+=e['total_micro']
            elif e['type']=='fill' and e['side']=='SELL': sell=e
        if buy is None: raise ValueError('OPPORTUNITY_ENTRY_BLOCKED')
        initial=buy['shares']*buy['price_micro'];risk=buy['shares']*(buy['price_micro']-buy['stop_micro'])
        low=sell['price_micro'] if sell and sell['reason'] in ('STOP','GAP_STOP') else bar['low']
        mae=max(mae,initial-qty*low-dividends)
        state=result.state
        if sell:
            pnl=result.nav['equity']-m.initial_cash
            return {'entry_session':str(dates[0].date()),'signal_session':str(signal.date()),
                'entry_price_micro':buy['price_micro'],'shares':buy['shares'],'stop_micro':buy['stop_micro'],
                'initial_risk_micro':risk,'net_pnl_micro':pnl,'net_r':pnl/risk,'net_return':pnl/initial,
                'mae_r':mae/risk,'mae_pct':mae/initial,'holding_sessions':int(calendar.searchsorted(day))-start+1,
                'reason':sell['reason'],'exit_session':date}
    raise ValueError('H60_NOT_CLOSED')


def execute(root, output):
    root,output=Path(root).resolve(),Path(output).resolve()
    if output.exists(): raise ValueError('OUTPUT_EXISTS')
    doc=Path(__file__).resolve().parents[2]/'docs/preregistrations'
    reg=read(doc/'ENTRY-DAILY-CONFIRM-20260921.json')
    amendment=read(doc/'ENTRY-DAILY-CONFIRM-20260921.engineering-amendment.json')
    if file_hash(doc/'ENTRY-DAILY-CONFIRM-20260921.json')!=amendment['original_registration_sha256']:raise ValueError('REGISTRATION_CHANGED')
    data=read(root/'study_manifest.json');checks=read(root/'checks.json')
    if data['manifest_hash']!=amendment['baseline_migration']['new_manifest_hash']:raise ValueError('BASELINE_CHANGED')
    if checks['baseline_parity']['status']!='VERIFIED' or checks['uncovered_by_parity']['dividend_receivable_outstanding']['stuck_within_window']:raise ValueError('BASELINE_BLOCKED')
    if file_hash(root/'comparison.json')!=read(root/'complete.json')['comparison_hash']:raise ValueError('RESULT_CHANGED')
    for entries in data['input_index'].values():
        for item in entries:
            if file_hash(root/item['path'])!=item['sha256']:raise ValueError('INPUT_CHANGED')
    # A new analysis module does not rewrite the archived baseline's code snapshot.
    current=code_hashes()
    if any(current.get(k)!=v for k,v in data['code_hashes'].items()):raise ValueError('BASELINE_IMPLEMENTATION_CHANGED')
    prices,_,calendar,_,actions=load(data,root)
    calendar=calendar[(calendar>=data['research_window']['start']) & (calendar<=data['research_window']['end'])]
    stocks={str(s):g.set_index('session') for s,g in prices.groupby('security_id')}
    m=manifest_from_dict(read(root/'variants/baseline/manifest.json'))
    converted,_=shadow_actions(actions,universe=set(stocks),session_range=(str(calendar[0].date()),str(calendar[-1].date())))
    acts=defaultdict(list)
    for a in converted:acts[(a['security_id'],a['ex_date'])].append(a)
    cutoff=reg['maturity']['common_candidate_cutoff']
    observations=read(root/'funnel_observations.json');signals=defaultdict(dict)
    for r in observations:
        if r['candidate_round']>cutoff:continue
        cid=r['candidate_id'];s=signals[cid];s.update(security_id=r['security_id'],candidate_round=r['candidate_round'])
        if r['stage']=='weekly' and r['result']=='pass':s.setdefault('B',r['session'])
        if r.get('candidate_state')=='READY':s['A']=r['session']
    # Reconcile archived account fills independently of unit-risk opportunities.
    # **双向**控制：对得上的逐字段比；对不上的一律要求由**预登记的候选轮次截止**解释。
    # 只做单向（"对得上才比"）会让"重建漏掉若干笔真成交"静默通过 —— 那正是这个控制
    # 要防的事。成交按 (security_id, 执行日) 经 `shadow:opportunity` 映射回候选轮次。
    def archived(directory):
        ledger=directory/'variants/baseline/ledger.sqlite3'
        with sqlite3.connect('file:'+str(ledger)+'?mode=ro',uri=True) as con:
            steps=[json.loads(b)['payload'] for (b,) in con.execute("SELECT body FROM decision_events WHERE event_type='shadow:step'")]
            opps=[json.loads(b)['payload'] for (b,) in con.execute("SELECT body FROM decision_events WHERE event_type='shadow:opportunity'")]
        fills=sorted((e['security_id'],e['session'],e['price_micro'],e['shares'],e['stop_micro'])
                     for e in steps if e.get('type')=='fill' and e.get('side')=='BUY')
        rounds={}
        for o in opps:
            cid=str(o.get('source_candidate_id') or '')
            rounds[(o['security_id'],o['planned_execution_session'])]=('-'.join(cid.split('-')[-3:]) if cid else '')
        return fills,rounds
    original=root.parent/amendment['baseline_migration']['from']
    if archived(root)[0]!=archived(original)[0]:raise ValueError('BASELINE_FILL_RECONCILIATION_FAILED')
    rows=[];blocked=[]
    for cid,s in sorted(signals.items()):
        if not ('A' in s or 'B' in s):continue
        if 'A' in s and 'B' not in s:raise ValueError('BASELINE_WITHOUT_WEEKLY_PASS')
        row={'candidate_id':cid,**s}
        try:
            for fee in (10,20):
                for arm in ('A','B'):
                    if arm in s:
                        trade=simulate(s['security_id'],cid,s[arm],stocks,calendar,acts,m,fee)
                        price=trade['entry_price_micro']/1e6
                        for label,day in [('decision',s['candidate_round']),('weekly',s['B'])]:
                            premium=price/float(stocks[s['security_id']].loc[pd.Timestamp(day),'raw_close'])-1
                            trade[label+'_premium_pct']=premium
                            trade[label+'_premium_r']=premium/(1-trade['stop_micro']/trade['entry_price_micro'])
                        row[f'{arm}{fee}']=trade
            if 'A' in s:
                row['delta_r']=row['B10']['net_r']-row['A10']['net_r']
                row['entry_delay_sessions']=int(calendar.searchsorted(pd.Timestamp(row['A10']['entry_session']))-calendar.searchsorted(pd.Timestamp(row['B10']['entry_session'])))
            rows.append(row)
        except ValueError as exc:blocked.append({'candidate_id':cid,'reason':str(exc)})
    # Every executed baseline must match its independently reconstructed A signal,
    # next-session open and stop; account shares were checked above, not inferred.
    a_index={(r['security_id'],r['A10']['entry_session']):r['A10'] for r in rows if 'A10' in r}
    archived_fills,round_by_execution=archived(root)
    reconciliation=reconcile_fills(archived_fills,a_index,round_by_execution,cutoff)
    if blocked:
        result={'status':'ENGINEERING_BLOCKED','blocked':blocked,'returns_published':False}
    else:
        paired=[r for r in rows if 'A10'in r];only=[r for r in rows if 'A10'not in r]
        if not paired:raise ValueError('NO_PAIRED_SAMPLE')
        dates=[r['candidate_round'] for r in paired];delta=[r['delta_r'] for r in paired]
        intervals={str(k):block_interval(delta,dates,calendar,k) for k in (10,20,40)}
        mae=block_interval([r['B10']['mae_r']-r['A10']['mae_r'] for r in paired],dates,calendar,20,statistic='median')
        a,b=(distribution([r[f'{arm}10'] for r in paired]) for arm in ('A','B'))
        c=concentration(paired,'delta_r');oc=concentration([dict(r['B10'],security_id=r['security_id']) for r in only],'net_r')
        span=candidate_span(calendar,dates)
        gates={'sample':len(paired)>=30 and span>=60,'candidate_span_sessions':span,'experimental_sample':len(only)>=30,
            'interval':intervals['20'][0]>0,'stop_rate':b['stop_rate']<=a['stop_rate']+.10,
            'tail':b['below_minus_2r_rate']<=a['below_minus_2r_rate']+.03,'worst5':b['worst5']>=a['worst5'],
            'mae':mae[1]<=.10,'concentration':c['top1_share'] is not None and c['top1_share']<=.5 and c['leave_one_out']>0 and c['top2_share']<1,
            'experimental_concentration':oc['top1_share'] is not None and oc['top1_share']<=.5,
            'stress':sum(r['B20']['net_r']-r['A20']['net_r'] for r in paired)>0}
        metrics={'delta_mean':float(np.mean(delta)),'intervals':intervals,'mae_difference_interval':mae,'gates':gates,
            'paired_A':a,'paired_B':b,'concentration':c,'experimental_concentration':oc,
            'policy':{f'{arm}{fee}':distribution([r[f'{arm}{fee}'] for r in rows if f'{arm}{fee}'in r]) for arm in ('A','B') for fee in (10,20)},
            'experimental_only':{str(fee):distribution([r[f'B{fee}'] for r in only]) for fee in (10,20)},
            'missed_winners':{'count':sum(r['B10']['net_r']>0 for r in only),'net_r_sum':sum(r['B10']['net_r'] for r in only if r['B10']['net_r']>0)},
            'avoided_losers':{'count':sum(r['B10']['net_r']<0 for r in only),'net_r_sum':sum(r['B10']['net_r'] for r in only if r['B10']['net_r']<0)}}
        result={'status':verdict(metrics),'metrics':metrics,'paired_count':len(paired),'experimental_only_count':len(only),'rows':rows}
    result['fill_reconciliation']=reconciliation
    result.update(study_id=data['study_id'],registration_sha256=amendment['original_registration_sha256'],
        amendment_sha256=file_hash(doc/'ENTRY-DAILY-CONFIRM-20260921.engineering-amendment.json'),
        analysis_code_hashes=code_hashes(),exploratory=True,account_level_started=False)
    write_json(output,result)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--study',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();r=execute(args.study,args.out);print(json.dumps({k:v for k,v in r.items() if k not in ('rows','analysis_code_hashes')},ensure_ascii=False,indent=2))
