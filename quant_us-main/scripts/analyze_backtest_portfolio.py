#!/usr/bin/env python3
"""组合口径回测分析：把各窗口的 trades CSV 汇总成净值/回撤/资金占用/regime 对比。

原始回测报告是「单票 + 单笔固定 $5000、逐笔独立累加」，看不出组合层面的回撤与资金占用。
本脚本按平仓时间重建累计盈亏曲线，并计算：

  - 期望/笔、盈亏比、胜率、平均持仓天
  - 累计净$、最大回撤（$，按已实现逐笔累加、不复利）
  - 最大并发持仓数与峰值资金占用（单笔 $5000 × 并发数）
  - 利润集中度（前 3 笔 / 前 2 只股票占累计净利的比例）
  - 分 regime 对比（同一变体在不同窗口的表现）

用法：
    python scripts/analyze_backtest_portfolio.py
    python scripts/analyze_backtest_portfolio.py --output backtests/portfolio_analysis.md

说明：不复利、不含未平仓（open=True 的逐笔单独列出）；成本沿用各回测 CSV 的 net_pnl_usd。
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# (窗口标签, CSV 路径)。注意 bt_us_2y 与主窗口时间重叠，是滚动 2 年窗，不是独立样本。
DEFAULT_SOURCES = [
    ('2020-07~2023-06（含2022熊市）', 'backtests/donchian_2020_2023/donchian_breakout_trades.csv'),
    ('2023-07~2026-09', 'backtests/donchian_breakout_trades.csv'),
    ('2024-07~2026-09（滚动2年）', 'backtests/bt_us_2y/donchian_breakout_trades.csv'),
]

POSITION_USD = 5000.0

# 跨窗口去重键：bt_us_2y 与主窗口时间重叠，直接 concat 会把重叠期的交易算两遍。
DEDUP_KEY = ['stock', 'variant', 'entry_date', 'exit_date', 'entry_price', 'exit_price']


def dedupe(df: pd.DataFrame) -> pd.DataFrame:
    """跨窗口去掉重复交易（同一笔在不同 CSV 出现）。"""
    return df.drop_duplicates(subset=DEDUP_KEY).reset_index(drop=True)


def load_sources(sources):
    frames = []
    for label, rel in sources:
        path = PROJECT_ROOT / rel
        if not path.exists():
            print(f'  [跳过] 找不到 {path}', file=sys.stderr)
            continue
        df = pd.read_csv(path)
        df['window'] = label
        frames.append(df)
    if not frames:
        raise SystemExit('没有任何可用的 trades CSV')
    out = pd.concat(frames, ignore_index=True)
    for c in ('entry_date', 'exit_date'):
        out[c] = pd.to_datetime(out[c])
    return out


def max_drawdown(equity: pd.Series) -> float:
    """累计盈亏曲线从峰值的最大回落（$）。equity 为已实现累计盈亏。"""
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    return float((peak - equity).max())


def max_concurrency(g: pd.DataFrame) -> int:
    """按 entry/exit 事件扫描求最大同时持仓数。"""
    events = []
    for _, r in g.iterrows():
        events.append((r['entry_date'], 1))
        events.append((r['exit_date'], -1))
    if not events:
        return 0
    events.sort(key=lambda x: (x[0], x[1]))  # 同日先加后减（保守估计占用）
    cur = peak = 0
    for _, d in events:
        cur += d
        peak = max(peak, cur)
    return peak


def variant_metrics(g: pd.DataFrame) -> dict:
    closed = g[~g['open'].astype(bool)].sort_values('exit_date')
    wins = closed[closed['net_pnl_usd'] > 0]
    losses = closed[closed['net_pnl_usd'] <= 0]
    gross_win = float(wins['net_pnl_usd'].sum())
    gross_loss = float(abs(losses['net_pnl_usd'].sum()))
    equity = closed['net_pnl_usd'].cumsum()
    total = float(closed['net_pnl_usd'].sum())
    top3 = float(closed['net_pnl_usd'].nlargest(3).sum())
    by_stock = closed.groupby('stock')['net_pnl_usd'].sum().sort_values(ascending=False)
    top2_stock = float(by_stock.head(2).sum())
    conc = max_concurrency(g)
    return {
        'trades': len(closed),
        'open_trades': int(g['open'].astype(bool).sum()),
        'win_rate': len(wins) / len(closed) if len(closed) else 0.0,
        'expectancy': float(closed['net_pnl_usd'].mean()) if len(closed) else 0.0,
        'profit_factor': gross_win / gross_loss if gross_loss else float('inf'),
        'avg_win': float(wins['net_pnl_usd'].mean()) if len(wins) else 0.0,
        'avg_loss': float(losses['net_pnl_usd'].mean()) if len(losses) else 0.0,
        'total': total,
        'max_dd': max_drawdown(equity),
        'max_concurrency': conc,
        'peak_capital': conc * POSITION_USD,
        'top3_share': top3 / total if total else float('nan'),
        'top2_stock_share': top2_stock / total if total else float('nan'),
        'avg_holding_days': float(closed['holding_days'].mean()) if len(closed) else 0.0,
    }


def fmt(v, pct=False, money=False):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return '∞' if isinstance(v, float) and v == float('inf') else 'n/a'
    if pct:
        return f'{v*100:.1f}%'
    if money:
        return f'{v:,.0f}'
    return f'{v:,.2f}'


def bucket_metrics(df: pd.DataFrame, freq: str = 'YS',
                   group_col: str = 'variant') -> pd.DataFrame:
    """按 entry_date 分桶（决策时点归属，无前视），逐桶算期望/盈亏比/笔数。

    group_col 指定分组列（默认 'variant'；regime 实验用 'config'）。
    返回 index=period, columns=[group_col, trades, expectancy, profit_factor, total]。
    """
    d = df[~df['open'].astype(bool)].copy()
    d['period'] = d['entry_date'].dt.to_period('Y' if freq == 'YS' else
                                               {'6MS': '6M', 'QS': 'Q'}.get(freq, 'Y')).astype(str)
    rows = []
    for (period, grp), g in d.groupby(['period', group_col]):
        wins = g[g['net_pnl_usd'] > 0]
        losses = g[g['net_pnl_usd'] <= 0]
        gw = float(wins['net_pnl_usd'].sum())
        gl = float(abs(losses['net_pnl_usd'].sum()))
        rows.append({
            'period': period, group_col: grp, 'trades': len(g),
            'expectancy': float(g['net_pnl_usd'].mean()),
            'profit_factor': gw / gl if gl else float('inf'),
            'total': float(g['net_pnl_usd'].sum()),
        })
    return pd.DataFrame(rows)


def stability_summary(buckets: pd.DataFrame, variant: str, min_trades: int = 5,
                      group_col: str = 'variant') -> dict:
    """稳定性判定：看该变体在各个桶里是否持续为正，而非只靠某一段。

    min_trades 以下的桶样本不足，单独计数，不参与通过率计算。
    """
    g = buckets[buckets[group_col] == variant]
    solid = g[g['trades'] >= min_trades]
    thin = g[g['trades'] < min_trades]
    if solid.empty:
        return {'buckets': 0, 'positive': 0, 'min_pf': float('nan'),
                'worst': None, 'thin': len(thin)}
    positive = int((solid['expectancy'] > 0).sum())
    worst = solid.loc[solid['expectancy'].idxmin()]
    return {
        'buckets': len(solid),
        'positive': positive,
        'pass_rate': positive / len(solid),
        'min_pf': float(solid['profit_factor'].replace([np.inf], np.nan).min()),
        'worst': f"{worst['period']} 期望{worst['expectancy']:,.0f}/笔（{int(worst['trades'])}笔）",
        'thin': len(thin),
    }


def rolling_expectancy(df: pd.DataFrame, variant: str, window: int = 12) -> pd.Series:
    """滚动 N 个月期望/笔（按月重采样后滚动），看是否长期稳定在 0 以上。"""
    g = df[(df['variant'] == variant) & (~df['open'].astype(bool))].copy()
    if g.empty:
        return pd.Series(dtype=float)
    monthly = g.set_index('entry_date').resample('MS').agg(
        n=('net_pnl_usd', 'size'), total=('net_pnl_usd', 'sum'))
    total = monthly['total'].rolling(window, min_periods=max(3, window // 3)).sum()
    n = monthly['n'].rolling(window, min_periods=max(3, window // 3)).sum()
    return (total / n).dropna()


def build_report(df: pd.DataFrame, focus_variant: str = 'dc55_vol',
                 bucket_freq: str = 'YS') -> str:
    lines = []
    lines.append('# 组合口径回测分析\n')
    lines.append(f'- 生成时间: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'- 数据: 三个窗口的 donchian_breakout_trades.csv 合并')
    lines.append(f'- 口径: 单笔名义 ${POSITION_USD:.0f}、逐笔累加不复利、仅统计已平仓；'
                 f'最大回撤 = 累计盈亏曲线从峰值的最大回落（$）\n')
    lines.append('> 注意：`2024-07~2026-09` 是滚动 2 年窗，与 `2023-07~2026-09` 时间重叠，'
                 '两者不是独立样本；独立对比只看 2020-2023 与 2023-2026 两段。\n')

    # ---- 分窗口 × 变体 ----
    lines.append('## 1. 分窗口 × 变体（组合口径）\n')
    lines.append('| 窗口 | 变体 | 笔数 | 胜率 | 期望/笔 | 盈亏比 | 累计净$ | 最大回撤$ | 最大并发 | 峰值占用$ |')
    lines.append('|---|---|---|---|---|---|---|---|---|---|')
    for window, g in df.groupby('window', sort=False):
        for variant, gv in g.groupby('variant', sort=True):
            m = variant_metrics(gv)
            label = gv['variant_label'].iloc[0]
            lines.append(
                f'| {window} | {variant} | {m["trades"]} | {fmt(m["win_rate"], pct=True)} | '
                f'{fmt(m["expectancy"], money=True)} | {fmt(m["profit_factor"])} | '
                f'{fmt(m["total"], money=True)} | {fmt(m["max_dd"], money=True)} | '
                f'{m["max_concurrency"]} | {fmt(m["peak_capital"], money=True)} |')
    lines.append('')

    # ---- 分 regime 对比（只看两段独立样本）----
    indep = ['2020-07~2023-06（含2022熊市）', '2023-07~2026-09']
    lines.append('## 2. 分 regime 对比（同一变体在两端独立样本）\n')
    lines.append('| 变体 | 2020-2023 累计$ | 2020-2023 盈亏比 | 2020-2023 回撤$ | '
                 '2023-2026 累计$ | 2023-2026 盈亏比 | 2023-2026 回撤$ |')
    lines.append('|---|---|---|---|---|---|---|')
    pivot = df[df['window'].isin(indep)]
    for variant in sorted(df['variant'].unique()):
        vals = []
        for w in indep:
            gv = pivot[(pivot['window'] == w) & (pivot['variant'] == variant)]
            if gv.empty:
                vals.append(('n/a', 'n/a', 'n/a'))
                continue
            m = variant_metrics(gv)
            vals.append((fmt(m['total'], money=True), fmt(m['profit_factor']),
                         fmt(m['max_dd'], money=True)))
        lines.append(f'| {variant} | ' + ' | '.join(v for t in vals for v in t) + ' |')
    lines.append('')

    # ---- 集中度 ----
    lines.append('## 3. 利润集中度与资金占用（主窗口 2023-07~2026-09）\n')
    main = df[df['window'] == '2023-07~2026-09']
    lines.append('| 变体 | 前3笔占累计 | 前2只票占累计 | 平均持仓天 | 未平仓 |')
    lines.append('|---|---|---|---|---|')
    for variant, gv in main.groupby('variant', sort=True):
        m = variant_metrics(gv)
        lines.append(f'| {variant} | {fmt(m["top3_share"], pct=True)} | '
                     f'{fmt(m["top2_stock_share"], pct=True)} | '
                     f'{m["avg_holding_days"]:.1f} | {m["open_trades"]} |')
    lines.append('')

    # ---- Walk-forward 稳定性（跨窗口去重后的连续时间线）----
    merged = dedupe(df)
    buckets = bucket_metrics(merged, bucket_freq)
    unit = {'YS': '年', '6MS': '半年', 'QS': '季'}.get(bucket_freq, '年')
    lines.append(f'## 4. Walk-forward 稳定性（按{unit}分桶，按决策时点归属）\n')
    lines.append(f'跨窗口去重后共 {len(merged)} 笔、{len(merged[~merged["open"].astype(bool)])} 笔已平仓；'
                 f'时间线 {merged["entry_date"].min().date()} ~ {merged["exit_date"].max().date()}。'
                 f'笔数 < {5} 的桶样本不足，不计入通过率。\n')

    # 年度期望矩阵（正=该年盈利）
    piv_exp = buckets.pivot(index='period', columns='variant', values='expectancy')
    piv_n = buckets.pivot(index='period', columns='variant', values='trades')
    lines.append(f'**各{unit}期望/笔（$，括号内为笔数）**\n')
    lines.append('| 期间 | ' + ' | '.join(piv_exp.columns) + ' |')
    lines.append('|' + '---|' * (len(piv_exp.columns) + 1))
    for period, row in piv_exp.iterrows():
        cells = []
        for v in piv_exp.columns:
            e, n = row.get(v), piv_n.loc[period, v]
            if pd.isna(e) or pd.isna(n):
                cells.append('—')
            elif n < 5:
                cells.append(f'{e:,.0f} ({int(n)})⚠')
            else:
                cells.append(f'{e:,.0f} ({int(n)})')
        lines.append(f'| {period} | ' + ' | '.join(cells) + ' |')
    lines.append('')

    lines.append('**稳定性判定**\n')
    lines.append(f'| 变体 | 有效桶数 | 正期望桶数 | 通过率 | 最差桶盈亏比 | 最差桶 | 样本不足桶 |')
    lines.append('|---|---|---|---|---|---|---|')
    for variant in sorted(merged['variant'].unique()):
        s = stability_summary(buckets, variant)
        mark = ' ←' if variant == focus_variant else ''
        lines.append(f'| {variant}{mark} | {s["buckets"]} | {s["positive"]} | '
                     f'{fmt(s.get("pass_rate"), pct=True)} | {fmt(s["min_pf"])} | '
                     f'{s["worst"] or "n/a"} | {s["thin"]} |')
    lines.append('')

    # 关注变体的滚动 12 个月期望
    roll = rolling_expectancy(merged, focus_variant)
    if not roll.empty:
        lines.append(f'**关注变体 `{focus_variant}` 的滚动 12 个月期望/笔**\n')
        lines.append('| 月末 | 滚动12月期望/笔$ |')
        lines.append('|---|---|')
        for ts, v in roll.items():
            lines.append(f'| {ts.strftime("%Y-%m")} | {v:,.0f} |')
        neg = int((roll < 0).sum())
        lines.append(f'\n滚动 12 个月窗口共 {len(roll)} 个，其中期望为负 {neg} 个'
                     f'（{neg/len(roll)*100:.0f}%）。\n')

    # ---- 结论提示 ----
    lines.append('## 5. 读法提示\n')
    lines.append('- **最大回撤$ 是本金规划的关键**：若峰值占用为 X，回撤为 Y，则最差时点的'
                 '权益回撤率 ≈ Y/X。用来看「要准备多少钱才能扛住」。')
    lines.append('- **前3笔/前2只集中度越高**，越说明总收益由少数样本驱动，样本外复现风险越大。')
    lines.append('- **两段 regime 对比若方向翻转**，说明表现主要来自市场环境而非策略本身，'
                 '应把差的那段当作压力测试基线来定仓位。')
    lines.append('- 本文口径为固定单笔 $5000、不复利、不含滑点恶化与借券成本，实盘会更差。')
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser(description='组合口径回测分析')
    ap.add_argument('--output', default=str(PROJECT_ROOT / 'backtests' / 'portfolio_analysis.md'))
    ap.add_argument('--focus-variant', default='dc55_vol', help='Walk-forward 重点关注的变体')
    ap.add_argument('--bucket', default='YS', choices=['YS', '6MS', 'QS'], help='分桶粒度')
    args = ap.parse_args()

    df = load_sources(DEFAULT_SOURCES)
    print(f'载入 {len(df)} 笔交易，窗口: {sorted(df["window"].unique())}')
    report = build_report(df, focus_variant=args.focus_variant, bucket_freq=args.bucket)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding='utf-8')
    print(f'已写出: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
