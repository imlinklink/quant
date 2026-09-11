#!/usr/bin/env python3
"""固定格式实验报告：分组统计、group bootstrap、聚合增量、Holm 校正。"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

from scripts.experiment_manifest import (GROUP_ORDER, LLM_UNAVAILABLE_REASON,
                                         check_groups_consistent, parse_groups)

EXIT_IDS = tuple(f'E{i}' for i in range(1, 12))
COSTS = (.001, .002, .005, .01)
# 报告中展示的 D 组不可判定原因（机器可读值见 manifest.llm_evaluation.reason）。
LLM_INCREMENT_INCONCLUSIVE_REASON = 'HISTORICAL_LLM_LABELS_UNAVAILABLE'


def llm_increment_status(groups, d_rows):
    """只有 C、D 都在且存在可比样本时才评估 LLM 增量，否则标记不可判定。"""
    if 'D' not in tuple(groups):
        return {'status': 'inconclusive', 'reason': LLM_INCREMENT_INCONCLUSIVE_REASON}
    if not d_rows:
        return {'status': 'inconclusive', 'reason': 'NO_D_C_SAMPLES'}
    return {'status': 'evaluated', 'reason': ''}


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


def _two_sample_p(a, b):
    """以独立组均值为单位的双样本正态近似 p 值。"""
    if len(a) < 2 or len(b) < 2: return 1.0
    se = math.sqrt(a.var(ddof=1)/len(a) + b.var(ddof=1)/len(b))
    if se == 0: return 0.0 if (float(b.mean())-float(a.mean())) != 0 else 1.0
    z = abs(float(b.mean())-float(a.mean()))/se
    return math.erfc(z/math.sqrt(2))


def holm_adjust(pvalues):
    order=sorted(range(len(pvalues)),key=lambda i:pvalues[i]);out=[1.]*len(pvalues);running=0.
    m=len(pvalues)
    for rank,i in enumerate(order):
        running=max(running,min(1.,(m-rank)*pvalues[i]));out[i]=running
    return out


def _arm_group_means(frame, experiment, value='net_pnl_pct'):
    g=frame[frame.experiment==experiment].groupby('independence_group')[value].mean().dropna()
    return g.to_numpy(float)


def increment_rows(frame, child, parent, iterations=2000, seed=20260910):
    """child 相对 parent 的**聚合期望增量**（独立组 bootstrap）+ 年份同方向计数。

    分组是嵌套的（child ⊆ parent），同一 setup 在两组结果相同，因此“同 setup 配对”
    的差值恒为 0、无法回答增量问题。这里改为比较两组各自的聚合期望。
    """
    sub=frame[frame.experiment.isin([parent,child])]
    if sub.empty: return []
    sub=sub.assign(_year=pd.to_datetime(sub['entry_time'],utc=True).dt.year)
    rows=[]
    for (exit_id,cost),g in sub.groupby(['exit_method','cost_scenario']):
        p=_arm_group_means(g,parent); c=_arm_group_means(g,child)
        parent_trades=int((g.experiment==parent).sum()); child_trades=int((g.experiment==child).sum())
        if len(p)==0 or len(c)==0:
            rows.append({'exit_method':exit_id,'cost_scenario':float(cost),
                'parent_trades':parent_trades,'child_trades':child_trades,
                'parent_mean':None,'child_mean':None,'mean_diff':None,
                'ci_low':None,'ci_high':None,'years_positive':0,'years_total':0,'p_value':1.0})
            continue
        rng=np.random.default_rng(seed)
        cs=rng.choice(c,(iterations,len(c)),replace=True).mean(axis=1)
        ps=rng.choice(p,(iterations,len(p)),replace=True).mean(axis=1)
        diffs=cs-ps
        ytot=ypos=0
        for _,gy in g.groupby('_year'):
            pm=gy.loc[gy.experiment==parent,'net_pnl_pct'].mean();cm=gy.loc[gy.experiment==child,'net_pnl_pct'].mean()
            if pd.notna(pm) and pd.notna(cm): ytot+=1; ypos+=int(cm>pm)
        rows.append({'exit_method':exit_id,'cost_scenario':float(cost),
            'parent_trades':parent_trades,'child_trades':child_trades,
            'parent_mean':float(p.mean()),'child_mean':float(c.mean()),
            'mean_diff':float(c.mean()-p.mean()),
            'ci_low':float(np.quantile(diffs,.025)),'ci_high':float(np.quantile(diffs,.975)),
            'years_positive':ypos,'years_total':ytot,'p_value':_two_sample_p(p,c)})
    adjusted=holm_adjust([r['p_value'] for r in rows]) if rows else []
    for r,a in zip(rows,adjusted):r['holm_p_value']=a
    return rows


def load_asset_types(path):
    """读取 `code,asset_type` 映射（供资产类型切片）。缺失则返回 None。"""
    if not path or not Path(path).is_file(): return None
    d=pd.read_csv(path)
    if not {'code','asset_type'}.issubset(d.columns): return None
    return dict(zip(d['code'].astype(str),d['asset_type'].astype(str)))


def asset_slices(accepted, groups, asset_types):
    """按资产类型分别计算相邻组增量，判断方向是否只在某一类资产成立。"""
    if not asset_types: return []
    d=accepted.copy(); d['asset_type']=d['stock'].map(asset_types)
    out=[]
    for at,sub in d.groupby('asset_type'):
        stocks=int(sub['stock'].nunique())
        for parent,child in zip(groups,groups[1:]):
            rows=increment_rows(sub,child,parent)
            valid=[r for r in rows if r.get('mean_diff') is not None]
            if not valid:
                out.append({'asset_type':at,'parent':parent,'child':child,'stocks':stocks,
                            'cells':len(rows),'positive':0,'median_diff':None,'mean_diff':None,
                            'years_positive_median':None}); continue
            diffs=[r['mean_diff'] for r in valid]
            yf=[r['years_positive']/r['years_total'] for r in valid if r['years_total']]
            out.append({'asset_type':at,'parent':parent,'child':child,'stocks':stocks,
                        'cells':len(valid),'positive':int(sum(1 for x in diffs if x>0)),
                        'median_diff':float(np.median(diffs)),'mean_diff':float(np.mean(diffs)),
                        'years_positive_median':float(np.median(yf)) if yf else None})
    return out


def metrics(matrix, groups=GROUP_ORDER, asset_types=None):
    groups=tuple(groups)
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
    increments=[]
    for parent,child in zip(groups,groups[1:]):
        increments.append({'parent':parent,'child':child,
                           'rows':increment_rows(accepted,child,parent)})
    d_minus_c=next((i['rows'] for i in increments if i['child']=='D'),[])
    slices=asset_slices(accepted,groups,asset_types)
    return {'groups':rows,'yearly':yearly,'d_minus_c':d_minus_c,'increments':increments,
            'asset_slices':slices,
            'asset_type_universe':sorted(set(asset_types.values())) if asset_types else [],
            'asset_types_missing':sorted(set(asset_types.values())-{s['asset_type'] for s in slices}) if asset_types else [],
            'has_asset_types':bool(asset_types),
            'selected_groups':list(groups),
            'llm_increment':llm_increment_status(groups,d_minus_c),
            'rejected':int((~d['portfolio_accepted'].astype(bool)).sum())}


def _pct(value, digits=1):
    return '-' if value is None else f"{value:.{digits}%}"


def _increment_table(rows):
    lines=['| Exit | 成本 | 交易 父→子 | 期望% 父 | 期望% 子 | 增量% | 95% CI | 年份同向 | Holm p |',
           '|---|---|---:|---:|---:|---:|---|---:|---:|']
    if not rows:
        lines.append('| - | - | 0 | - | - | - | - | - | - |');return lines
    for r in rows:
        if r.get('mean_diff') is None:
            lines.append(f"| {r['exit_method']} | {r['cost_scenario']:.2%} | "
                         f"{r['parent_trades']}→{r['child_trades']} | - | - | - | - | - | - |")
            continue
        lines.append(f"| {r['exit_method']} | {r['cost_scenario']:.2%} | "
                     f"{r['parent_trades']}→{r['child_trades']} | {r['parent_mean']:.4%} | {r['child_mean']:.4%} | "
                     f"{r['mean_diff']:.4%} | [{r['ci_low']:.4%}, {r['ci_high']:.4%}] | "
                     f"{r['years_positive']}/{r['years_total']} | {r['holm_p_value']:.4f} |")
    return lines


def render_report(result, experiment_id, groups=None):
    groups=tuple(groups or result.get('selected_groups') or GROUP_ORDER)
    label='/'.join(groups)
    lines=[f'# 买入策略验证报告：{experiment_id}','',
           '> 本报告由冻结逐笔数据生成；参数与验收标准见 manifest 和实验设计。','',
           f'## {label} × Exit 核心结果','',
           '| 组 | Exit | 成本 | 交易 | 独立组 | 期望$ | 总收益$ | 胜率 | MAE | 最大回撤$ | top2股占比 | top3笔占比 |',
           '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in result['groups']:
        lines.append(f"| {r['experiment']} | {r['exit_method']} | {r['cost_scenario']:.2%} | "
          f"{r['trades']} | {r['independence_groups']} | {r['expectancy_usd']:.2f} | "
          f"{r['total_net_usd']:.2f} | {r['win_rate']:.1%} | {r['mean_mae_pct']:.2%} | "
          f"{r['max_drawdown_usd']:.2f} | {_pct(r['top2_stock_share'])} | {_pct(r['top3_trade_share'])} |")
    # 相邻组增量（D−C 单独在 LLM 段落呈现）。
    for inc in result.get('increments',[]):
        if inc['child']=='D': continue
        lines += ['', f"## {inc['child']}−{inc['parent']} 增量（聚合期望对比）", '']
        lines += _increment_table(inc['rows'])
    lines += ['', '## LLM 增量（D−C）', '']
    llm=result.get('llm_increment') or {}
    if 'D' in groups:
        dinc=next((i for i in result.get('increments',[]) if i['child']=='D'),None)
        lines += _increment_table(dinc['rows'] if dinc else [])
    else:
        reason=llm.get('reason') or LLM_INCREMENT_INCONCLUSIVE_REASON
        lines += [f"LLM_INCREMENT_STATUS = {llm.get('status','inconclusive')}",
                  f"reason = {reason}",
                  '',
                  '> 本实验不含 D 组，无法回答“大模型是否创造选股增益”。']
    lines += ['', '## 资产类型切片（增量方向）', '']
    if not result.get('asset_slices'):
        lines += ['> 未提供证券主数据（security_master.csv），跳过资产类型切片。','']
    else:
        lines += ['| 资产 | 增量 | 标的数 | 单元 | 为正 | 增量中位 | 增量均值 | 年份同向中位 |',
                  '|---|---|---:|---:|---:|---:|---:|---:|']
        for s in result['asset_slices']:
            lines.append(f"| {s['asset_type']} | {s['child']}−{s['parent']} | {s['stocks']} | "
                         f"{s['cells']} | {s['positive']} | {_pct(s['median_diff'],2)} | "
                         f"{_pct(s['mean_diff'],2)} | {_pct(s['years_positive_median'])} |")
        missing=result.get('asset_types_missing') or []
        if missing:
            lines += ['', f"> 未产生可交易样本的资产类型：{', '.join(missing)}（其方向无法评估）。"]
        lines += ['', '> 增量方向可能按资产类型相反（如普通股与杠杆 ETF），必须分层判读，不能只看总体。']
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
    lines += ['', '## 判定', '',
              '- 增量列是两组**聚合期望差**（独立组 bootstrap），不是逐笔配对；只有多数年份同向且成本/退出档方向一致才算信号。',
              '- 相邻组增量只回答：B−A 周线环境门的价值、C−B 日线确认的价值；退出稳定性看 E1−E11 跨年份/成本方向。',
              '- 必须由预先登记的验收程序填写 `retain/reject/inconclusive`；本报告不自动选择最佳参数。','']
    return '\n'.join(lines)


def write_new(path,text):
    p=Path(path)
    if p.exists():raise FileExistsError(f'禁止覆盖实验产物: {p}')
    p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf-8')


def main():
    p=argparse.ArgumentParser(description='生成不可覆盖的买入实验报告')
    p.add_argument('--matrix',required=True);p.add_argument('--manifest',required=True)
    p.add_argument('--output-dir',required=True)
    p.add_argument('--groups',default='ABCD',
                   help='实验分组，A/B/C/D 的有序子集，默认 ABCD（例如 ABC）')
    p.add_argument('--security-master',
                   help='可选：含 code,asset_type 的证券主数据，用于资产类型切片；缺省时从 manifest data_files 自动查找 security_master.csv')
    args=p.parse_args()
    try:
        groups=parse_groups(args.groups)
    except ValueError as exc:
        raise SystemExit(str(exc))
    manifest=json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    from scripts.experiment_manifest import validate_manifest
    errors=validate_manifest(manifest)
    if errors:raise SystemExit('manifest 无效: '+','.join(errors))
    check_groups_consistent(groups,manifest)
    matrix=pd.read_csv(args.matrix)
    expected={(g,x,c) for g in groups for x in EXIT_IDS for c in COSTS}
    actual=set(zip(matrix.experiment,matrix.exit_method,matrix.cost_scenario.astype(float)))
    if expected-actual:raise SystemExit(f'矩阵不完整，缺少 {len(expected-actual)} 个单元')
    master=args.security_master
    if not master:
        for rec in manifest.get('data_files') or []:
            pth=rec.get('path','')
            if Path(pth).name=='security_master.csv' and Path(pth).is_file(): master=pth;break
    asset_types=load_asset_types(master)
    result=metrics(matrix,groups,asset_types=asset_types);out=Path(args.output_dir)
    write_new(out/'metrics.json',json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    write_new(out/'report.md',render_report(result,manifest['experiment_id'],groups))
    print(out/'report.md');return 0


if __name__=='__main__':raise SystemExit(main())
