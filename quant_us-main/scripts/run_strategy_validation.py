"""离线组合验证：CSV已收盘信号 + 15分钟OHLC，按时间分割。

signals: code,strategy,signal_time,approved_at,price,initial_stop,risk_group;
可选 target,breakout_level,signal_atr,max_chase_atr,time_exit_bars,failure_sessions。
bars: code,date,open,high,low,close。
approved_at 是显式的模拟/历史人工确认时间；缺失则不成交。规则退出也延迟
approval_delay_bars 根才执行。此模拟不宣称重现历史LLM判断或人工决定。
"""
import argparse
import json
import math
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pandas as pd
from scripts.live_trading.execution import risk_quantity
from scripts.live_trading.strategy_rules import exit_reason, atr


def run_portfolio(bars, signals, cfg, approval_delay_bars=1):
    if approval_delay_bars<1:raise ValueError('退出至少下一根执行，不能同根审批成交')
    bars=bars.copy();signals=signals.copy()
    bars['date']=pd.to_datetime(bars.date,utc=True)
    for c in ['signal_time','approved_at']:signals[c]=pd.to_datetime(signals[c],utc=True)
    initial=float(cfg.get('dry_run_equity',100000));cash=initial
    positions={}; trades=[]; equity=[]; decisions=[]; processed=set(); last={}
    cost=float(cfg.get('cost_per_share',.05)); pending={}
    for timestamp, batch in bars.sort_values(['date','code']).groupby('date',sort=True):
        for row in batch.to_dict('records'):
            last[row['code']]=float(row['open'])
        for row in batch.to_dict('records'):
            code=row['code']
            pos=positions.get(code)
            if pos and code in pending:
                pending[code]-=1
                if pending[code]<=0:
                    fill=float(row['open'])
                    cash+=pos['qty']*(fill-cost)
                    pnl=pos['qty']*(fill-pos['entry_price']-2*cost)
                    trades.append(dict(code=code,strategy=pos['entry_mode'],entry_time=pos['entry_time'].isoformat(),
                        exit_time=timestamp.isoformat(),net_pnl=pnl,R=pnl/pos['initial_risk'],
                        holding_minutes=(timestamp-pos['entry_time']).total_seconds()/60,reason=pos['exit_reason']))
                    del positions[code];del pending[code];pos=None
            if pos:
                # 先按此前保护线判断整根，收盘信息只能影响下一根。
                if float(row['low'])<=pos['stop_line']:
                    pos['exit_reason']='HARD_STOP' if float(row['low'])<=pos['initial_stop'] else 'TRAILING_EXIT'
                    pending.setdefault(code,approval_delay_bars)
                history=bars[(bars.code==code)&(bars.date<=timestamp)].copy()
                history['time_key']=history.date
                daily=history.assign(date=history.date.dt.tz_convert('America/New_York')).set_index('date').resample('1D').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna().reset_index()
                why=exit_reason(pos,float(row['close']),daily,history,timestamp+pd.Timedelta(minutes=15))
                if why:
                    pos['exit_reason']=why;pending.setdefault(code,approval_delay_bars)
                if pos['entry_mode']!='dip_buy':
                    closed=daily[daily.date<timestamp.normalize()]
                    held=closed[closed.date>pos['entry_time'].tz_convert('America/New_York').normalize()]
                    if len(closed)>=14 and len(held):
                        a=float(atr(closed).iloc[-1])
                        pos['stop_line']=max(pos['stop_line'],float(held.high.max())-2*a)
            candidates=signals[(signals.code==code)&(signals.signal_time<timestamp)&(signals.approved_at<=timestamp)]
            for index, signal in candidates.iterrows():
                if index in processed:continue
                processed.add(index)
                if code in positions:
                    decisions.append(dict(code=code,status='held'));continue
                # 当前bar开盘是第一笔可交易报价；不凭之后的低点假定限价单成交。
                price=float(row['open']); limit=float(signal.price)
                if price>limit:
                    decisions.append(dict(code=code,status='unfilled'));continue
                meta={k:v for k,v in signal.to_dict().items() if pd.notna(v)}
                if meta.get('breakout_level'):
                    a=float(meta.get('signal_atr',0))
                    if a<=0 or (price-meta['breakout_level'])/a>float(meta.get('max_chase_atr',.5)):
                        decisions.append(dict(code=code,status='chase_blocked'));continue
                try:
                    nav=cash+sum(p['qty']*last[c] for c,p in positions.items())
                    qty,risk=risk_quantity(price,float(signal.initial_stop),nav,cash,list(positions.values()),[],cfg,
                        signal.risk_group,float(cfg.get('max_notional',5000)))
                except ValueError:
                    decisions.append(dict(code=code,status='risk_blocked'));continue
                if meta.get('target') and (meta['target']-price)/(price-signal.initial_stop)<float(meta.get('min_rr',1.5)):
                    decisions.append(dict(code=code,status='rr_blocked'));continue
                cash-=qty*(price+cost)
                positions[code]=dict(meta,qty=qty,entry_price=price,entry_mode=signal.strategy,
                    opened_at=timestamp.timestamp(),entry_time=timestamp,initial_risk=risk,stop_line=float(signal.initial_stop))
                # 入场同根的硬止损也生成审批事件，下一根才成交。
                if float(row['low'])<=float(signal.initial_stop):
                    positions[code]['exit_reason']='HARD_STOP';pending[code]=approval_delay_bars
                decisions.append(dict(code=code,status='filled'))
        for row in batch.to_dict('records'):
            last[row['code']]=float(row['close'])
        nav=cash+sum(p['qty']*last[c] for c,p in positions.items())
        equity.append(nav)
    peak=initial;max_dd=0
    for nav in equity:peak=max(peak,nav);max_dd=min(max_dd,nav/peak-1)
    grouped={}
    for strategy in sorted({t['strategy'] for t in trades}):
        rows=[t for t in trades if t['strategy']==strategy];profits=sorted([t['net_pnl'] for t in rows if t['net_pnl']>0],reverse=True)
        grouped[strategy]=dict(trades=len(rows),net_expectancy_R=sum(t['R'] for t in rows)/len(rows),
            mean_holding_minutes=sum(t['holding_minutes'] for t in rows)/len(rows),
            top3_profit_share=sum(profits[:3])/sum(profits) if profits else None)
    return dict(initial_equity=initial,final_equity=equity[-1] if equity else initial,max_drawdown=max_dd,
                open_positions=len(positions),strategies=grouped,trades=trades,decisions=decisions)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--bars',required=True);p.add_argument('--signals',required=True)
    p.add_argument('--split',required=True);p.add_argument('--config',required=True)
    p.add_argument('--output',required=True);p.add_argument('--approval-delay-bars',type=int,default=1)
    args=p.parse_args()
    import yaml
    cfg=yaml.safe_load(Path(args.config).read_text())['risk_budget']
    bars=pd.read_csv(args.bars);signals=pd.read_csv(args.signals)
    bar_time=pd.to_datetime(bars.date,utc=True);signal_time=pd.to_datetime(signals.signal_time,utc=True)
    split=pd.Timestamp(args.split,tz='UTC')
    result={}
    for name,mask,smask in [('train',bar_time<split,signal_time<split),('out_of_sample',bar_time>=split,signal_time>=split)]:
        result[name]=run_portfolio(bars[mask],signals[smask],cfg,args.approval_delay_bars)
    result['assumptions']='显式入场批准时间；退出审批延迟按参数模拟；期末持仓按收盘标记，未强制平仓；不重现历史LLM判断。'
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))


if __name__=='__main__':main()
