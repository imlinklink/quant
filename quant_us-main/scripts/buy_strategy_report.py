#!/usr/bin/env python3
"""固定格式实验报告：分组统计、group bootstrap、配对 D-C、Holm 校正。"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


def add_independence_group(frame):
    d=frame.copy();dt=pd.to_datetime(d['entry_time'],utc=True)
    week=dt.dt.strftime('%G-W%V')
    strategy=d.get('strategy',pd.Series('baseline',index=d.index)).fillna('baseline')
    d['independence_group']=d.get('independence_group',
        d['stock'].astype(str)+'|'+strategy.astype(str)+'|'+week.astype(str))
    return d


def group_bootstrap_ci(frame, value='net_pnl_pct', iterations=2000, seed=20260910):
    g=frame.groupby('independence_group')[value].mean().dropna()
    if g.empty:return {'mean':None,'low':None,'high':None,'groups':0}
    rng=np.random.default_rng(seed);values=g.to_numpy(float)
    samples=rng.choice(values,(iterations,len(values)),replace=True).mean(axis=1)
    return {'mean':float(values.mean()),'low':float(np.quantile(samples,.025)),
            'high':float(np.quantile(samples,.975)),'groups':len(values)}


def _normal_p(diff):
    if len(diff)<2:return 1.0
    if float(diff.std(ddof=1))==0:return 0.0 if float(diff.mean())!=0 else 1.0
    z=abs(float(diff.mean())/(float(diff.std(ddof=1))/math.sqrt(len(diff))))
    return math.erfc(z/math.sqrt(2))


def holm_adjust(pvalues):
    order=sorted(range(len(pvalues)),key=lambda i:pvalues[i]);out=[1.]*len(pvalues);running=0.
    m=len(pvalues)
    for rank,i in enumerate(order):
        running=max(running,min(1.,(m-rank)*pvalues[i]));out[i]=running
    return out


def paired_d_minus_c(frame):
    key=['setup_id','exit_method','cost_scenario']
    p=frame[frame.experiment.isin(['C','D'])].pivot_table(index=key,columns='experiment',
        values='net_pnl_pct',aggfunc='first').dropna()
    rows=[]
    for (exit_id,cost),g in p.reset_index().groupby(['exit_method','cost_scenario']):
        diff=g['D']-g['C'];rng=np.random.default_rng(20260910)
        boot=(rng.choice(diff.to_numpy(float),(2000,len(diff)),replace=True).mean(axis=1)
              if len(diff) else np.array([np.nan]))
        rows.append({'exit_method':exit_id,'cost_scenario':float(cost),
            'pairs':len(diff),'mean_diff':float(diff.mean()),
            'ci_low':float(np.nanquantile(boot,.025)),'ci_high':float(np.nanquantile(boot,.975)),
            'p_value':_normal_p(diff)})
    adjusted=holm_adjust([r['p_value'] for r in rows]) if rows else []
    for r,a in zip(rows,adjusted):r['holm_p_value']=a
    return rows


def metrics(matrix):
    d=add_independence_group(matrix)
    accepted=d[(d['portfolio_accepted'].astype(bool))&(d['data_quality']=='good')].copy()
    rows=[]
    for keys,g in accepted.groupby(['experiment','exit_method','cost_scenario']):
        ci=group_bootstrap_ci(g)
        pnl=g.net_pnl_usd.astype(float);total=float(pnl.sum())
        by_stock=g.groupby('stock').net_pnl_usd.sum().sort_values(ascending=False)
        realized=g.sort_values('exit_time' if 'exit_time' in g else 'entry_time').net_pnl_usd.astype(float).cumsum()
        max_dd=float((realized.cummax()-realized).max()) if len(realized) else 0.
        rows.append({'experiment':keys[0],'exit_method':keys[1],'cost_scenario':float(keys[2]),
            'trades':len(g),'independence_groups':ci['groups'],'total_net_usd':total,
            'expectancy_usd':float(pnl.mean()),'win_rate':float((pnl>0).mean()),
            'mean_mae_pct':float(g.mae_pct.mean()),'mean_mfe_pct':float(g.mfe_pct.mean()),
            'max_drawdown_usd':max_dd,
            'net_pnl_pct_ci':ci,'top2_stock_share':float(by_stock.head(2).sum()/total) if total else None,
            'top3_trade_share':float(pnl.nlargest(3).sum()/total) if total else None})
    yearly=[]
    accepted['year']=pd.to_datetime(accepted['entry_time'],utc=True).dt.year
    for keys,g in accepted.groupby(['experiment','exit_method','cost_scenario','year']):
        yearly.append({'experiment':keys[0],'exit_method':keys[1],
            'cost_scenario':float(keys[2]),'year':int(keys[3]),'trades':len(g),
            'expectancy_usd':float(g.net_pnl_usd.mean()),'total_net_usd':float(g.net_pnl_usd.sum())})
    return {'groups':rows,'yearly':yearly,'d_minus_c':paired_d_minus_c(accepted),
            'rejected':int((~d['portfolio_accepted'].astype(bool)).sum())}


def render_report(result, experiment_id):
    lines=[f'# 买入策略验证报告：{experiment_id}','',
           '> 本报告由冻结逐笔数据生成；参数与验收标准见 manifest 和实验设计。','',
           '## A/B/C/D × Exit 核心结果','',
           '| 组 | Exit | 成本 | 交易 | 独立组 | 期望$ | 总收益$ | 胜率 | MAE | 最大回撤$ |',
           '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in result['groups']:
        lines.append(f"| {r['experiment']} | {r['exit_method']} | {r['cost_scenario']:.2%} | "
          f"{r['trades']} | {r['independence_groups']} | {r['expectancy_usd']:.2f} | "
          f"{r['total_net_usd']:.2f} | {r['win_rate']:.1%} | {r['mean_mae_pct']:.2%} | "
          f"{r['max_drawdown_usd']:.2f} |")
    lines += ['', '## D-C 配对增量', '', '| Exit | 成本 | 配对数 | 平均差 | Holm p |',
              '|---|---:|---:|---:|---:|']
    for r in result['d_minus_c']:
        lines.append(f"| {r['exit_method']} | {r['cost_scenario']:.2%} | {r['pairs']} | "
                     f"{r['mean_diff']:.4%} | {r['holm_p_value']:.4f} |")
    lines += ['', '## 组合拒绝', '', f"- 被数据质量或最多三仓拒绝的矩阵行：{result['rejected']}", '',
              '## 最好与最差年份', '', '| 组 | Exit | 成本 | 最好年份/期望 | 最差年份/期望 |',
              '|---|---|---:|---|---|']
    years=pd.DataFrame(result['yearly'])
    if not years.empty:
        for keys,g in years.groupby(['experiment','exit_method','cost_scenario']):
            best=g.loc[g.expectancy_usd.idxmax()];worst=g.loc[g.expectancy_usd.idxmin()]
            lines.append(f"| {keys[0]} | {keys[1]} | {keys[2]:.2%} | "
                         f"{int(best.year)}/{best.expectancy_usd:.2f} | "
                         f"{int(worst.year)}/{worst.expectancy_usd:.2f} |")
    lines += ['', '## 判定', '', '- 必须由预先登记的验收程序填写 `retain/reject/inconclusive`；本报告不自动选择最佳参数。','']
    return '\n'.join(lines)


def write_new(path,text):
    p=Path(path)
    if p.exists():raise FileExistsError(f'禁止覆盖实验产物: {p}')
    p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf-8')


def main():
    p=argparse.ArgumentParser(description='生成不可覆盖的买入实验报告')
    p.add_argument('--matrix',required=True);p.add_argument('--manifest',required=True)
    p.add_argument('--output-dir',required=True);args=p.parse_args()
    manifest=json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    from scripts.experiment_manifest import validate_manifest
    errors=validate_manifest(manifest)
    if errors:raise SystemExit('manifest 无效: '+','.join(errors))
    matrix=pd.read_csv(args.matrix)
    expected={(e,x,c) for e in 'ABCD' for x in [f'E{i}' for i in range(1,12)]
              for c in (.001,.002,.005,.01)}
    actual=set(zip(matrix.experiment,matrix.exit_method,matrix.cost_scenario.astype(float)))
    if expected-actual:raise SystemExit(f'矩阵不完整，缺少 {len(expected-actual)} 个单元')
    result=metrics(matrix);out=Path(args.output_dir)
    write_new(out/'metrics.json',json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    write_new(out/'report.md',render_report(result,manifest['experiment_id']))
    print(out/'report.md');return 0


if __name__=='__main__':raise SystemExit(main())
