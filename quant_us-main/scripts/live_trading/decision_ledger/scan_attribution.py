"""抄底扫描归因（P2 评估闭环 · 第 3 块）。

把 dip_scans.jsonl 与 dip_scan_outcomes.jsonl 关联，回答：
  - 哪个评分组件（超卖/布林/量/真背离/反转确认/60m环境）真的预测反弹？
  - 各闸门（blocked_reversal / blocked_rr / blocked_60m …）拦掉的扫描，
    如果不拦，前向收益是否确实更差？（验证闸门有效性）

用法：
    python scripts/live_trading/decision_ledger/scan_attribution.py
    python scripts/live_trading/decision_ledger/scan_attribution.py --horizon 24
输出：控制台表格 + data/decision_ledger/scan_attribution.md
"""
import argparse
import os
import sys
from datetime import datetime
from typing import Dict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, BASE_DIR)

import pandas as pd  # noqa: E402

from scripts.live_trading.decision_ledger import scan_ledger  # noqa: E402

HORIZON_COL = {12: 'r12', 24: 'r24', 48: 'r48'}
HORIZON_LABEL = {12: '1小时', 24: '2小时', 48: '4小时'}


def _fmt(v, suffix=''):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return '-'
    return f'{v:.2f}{suffix}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--horizon', type=int, choices=[12, 24, 48], default=24)
    ap.add_argument('--min-bars', type=int, default=12)
    args = ap.parse_args()

    scans = scan_ledger.load_scans()
    if not scans:
        print('暂无扫描流水（重启后在盘中/盘后运行才会累积）')
        return
    outcomes: Dict[str, Dict] = {}
    for o in scan_ledger.load_outcomes():
        outcomes[o['scan_id']] = o  # 保留最新一次回填（含更长的前向）
    rows = []
    for s in scans:
        o = outcomes.get(s.get('scan_id'))
        if not o or int(o.get('bars_after') or 0) < args.min_bars:
            continue
        rows.append({**s, **o})
    if not rows:
        print(f'暂无满足回填条件（至少 {args.min_bars} 根后向K）的扫描，'
              f'先跑 backfill_scan_outcomes.py')
        return
    df = pd.DataFrame(rows)
    rcol = HORIZON_COL[args.horizon]
    df[rcol] = pd.to_numeric(df.get(rcol), errors='coerce')
    df = df[df[rcol].notna()].copy()
    label = HORIZON_LABEL[args.horizon]
    if df.empty:
        print(f'该时间窗口({label})还没有可用的完整样本')
        return

    def stats(g):
        return pd.Series({
            'n': int(len(g)),
            '均值%': round(float(g[rcol].mean()), 3),
            '中位%': round(float(g[rcol].median()), 3),
            '命中率%': round(float((g[rcol] > 0).mean() * 100), 1),
        })

    lines = []
    lines.append(f'# 抄底扫描归因（前向 {label}）\n')
    lines.append(f'- 生成: {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    lines.append(f'- 有效样本: {len(df)} 条\n')

    def add_group(title, group_col, mapping=None, order=None):
        g = df.groupby(group_col).apply(stats).reset_index()
        if mapping:
            g['组'] = g[group_col].map(mapping)
        else:
            g['组'] = g[group_col].astype(str)
        g = g[['组', 'n', '均值%', '中位%', '命中率%']]
        if order is not None:
            g['__o'] = g['组'].map({k: i for i, k in enumerate(order)})
            g = g.sort_values('__o').drop(columns='__o')
        lines.append(f'## {title}\n')
        lines.append('| 组 | n | 均值% | 中位% | 命中率% |')
        lines.append('|---|---|---|---|---|')
        for _, r in g.iterrows():
            lines.append(f"| {r['组']} | {r['n']} | {_fmt(r['均值%'])} | "
                         f"{_fmt(r['中位%'])} | {_fmt(r['命中率%'])} |")
        lines.append('')
        print(f'\n{title}\n{g.to_string(index=False)}')

    add_group('整体', '__all__', mapping={'__all__': '全部有效扫描'})
    add_group('最终去向 outcome', 'outcome',
              mapping={'below_threshold': '未达阈值', 'blocked_reversal': '反转确认拦截',
                       'blocked_rr': '盈亏比不足', 'blocked_60m': '60m强下行拦截',
                       'blocked_earnings': '财报窗口拦截', 'passed': '通过全部闸门',
                       'shadow_index_would_block': '指数门影子拦截(仍执行)',
                       'blocked_index_pause': '指数门暂停拦截',
                       'blocked_index_stricter': '指数门门槛拦截'})
    if 'index_action' in df.columns:
        add_group('指数门状态 index_action', 'index_action',
                  mapping={'': '无(指数正常/未到指数门)', 'stricter': '弱势·提高门槛',
                           'pause': '急跌·暂停', 'info': '仅提示'},
                  order=['无(指数正常/未到指数门)', '弱势·提高门槛',
                         '急跌·暂停', '仅提示'])
        # 影子反事实：只看“通过了 reversal/60m/RR/财报”的准可买信号，
        # 比较 实际放行 vs 指数门影子应拦截 的前向收益。
        _shadow = df[df['outcome'].isin(
            ['passed', 'queue_skipped', 'shadow_index_would_block'])].copy()
        if len(_shadow) > 0:
            add_group('指数门影子反事实（准可买信号）', 'outcome',
                      mapping={'passed': '放行·推送/执行',
                               'queue_skipped': '放行·队列跳过',
                               'shadow_index_would_block': '影子应拦截(仍执行)'},
                      order=['放行·推送/执行', '放行·队列跳过',
                             '影子应拦截(仍执行)'])
    add_group('超卖分 rsi_score', 'rsi_score',
              mapping={0: '0分', 1: '1分', 2: '2分', 3: '3分'},
              order=['0分', '1分', '2分', '3分'])
    add_group('布林分 bb_score', 'bb_score',
              mapping={0: '0分', 1: '1分', 2: '2分'}, order=['0分', '1分', '2分'])
    add_group('量分 volume_score', 'volume_score',
              mapping={0: '0分', 1: '1分', 2: '2分'}, order=['0分', '1分', '2分'])
    div = df.copy()
    div['背离分桶'] = div['divergence_score'].apply(lambda x: '≥1(有背离)' if (x or 0) >= 1 else '0(无)')
    # 用辅助列单独分组
    group_df = div.groupby('背离分桶').apply(stats).reset_index()
    group_df.columns = ['组', 'n', '均值%', '中位%', '命中率%']
    lines.append('## 真背离\n')
    lines.append('| 组 | n | 均值% | 中位% | 命中率% |')
    lines.append('|---|---|---|---|---|')
    for _, r in group_df.iterrows():
        lines.append(f"| {r['组']} | {r['n']} | {_fmt(r['均值%'])} | {_fmt(r['中位%'])} | {_fmt(r['命中率%'])} |")
    lines.append('')
    print(f'\n真背离\n{group_df.to_string(index=False)}')
    add_group('反转确认 reversal_ok', 'reversal_ok',
              mapping={True: '有反转确认', False: '无反转确认'})
    add_group('60m环境 htf_env_score', 'htf_env_score',
              mapping={-2: '强下行-2', -1: '超买-1', 0: '中性0', 1: '深超卖+1'},
              order=['-2', '-1', '0', '1'])
    rr = df.copy()
    rr['RR桶'] = rr['rr'].apply(lambda x: '≥1.5' if (x or 0) >= 1.5 else '<1.5')
    rr_stats = rr.groupby('RR桶').apply(stats).reset_index()
    rr_stats.columns = ['组', 'n', '均值%', '中位%', '命中率%']
    lines.append('## 盈亏比 RR\n')
    lines.append('| 组 | n | 均值% | 中位% | 命中率% |')
    lines.append('|---|---|---|---|---|')
    for _, r in rr_stats.iterrows():
        lines.append(f"| {r['组']} | {r['n']} | {_fmt(r['均值%'])} | {_fmt(r['中位%'])} | {_fmt(r['命中率%'])} |")
    lines.append('')
    print(f'\n盈亏比 RR\n{rr_stats.to_string(index=False)}')

    # 资金流/期权影子字段（只有新扫描才有，旧样本自动跳过）
    flow_map = {'主力强净流入': '强净流入', '主力净流入': '净流入',
                '主力强净流出': '强净流出', '主力净流出': '净流出',
                '主力中性': '中性', 'no_data': '无数据'}
    if 'flow_label' in df.columns:
        add_group('资金流(影子)', 'flow_label',
                  mapping=lambda x: flow_map.get(str(x), str(x)))
    option_map = {'IV正常': 'IV正常', 'IV偏高': 'IV偏高', 'IV极端': 'IV极端',
                  'PUT压力': 'PUT压力', 'no_data': '无数据'}
    if 'option_label' in df.columns:
        add_group('期权(影子)', 'option_label',
                  mapping=lambda x: option_map.get(str(x), str(x)))

    md_path = os.path.join(BASE_DIR, 'data', 'decision_ledger', 'scan_attribution.md')
    os.makedirs(os.path.dirname(md_path), exist_ok=True)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f'\n报告已保存: {md_path}')


if __name__ == '__main__':
    main()
