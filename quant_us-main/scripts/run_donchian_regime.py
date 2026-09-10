#!/usr/bin/env python3
"""唐奇安突破 市场状态（regime）过滤实验。

参数优化已被证明无样本外价值（见 donchian_walkforward.md）；本脚本验证另一个假设：
**是否只在趋势环境下开仓，就能救回坏年份、且不摧毁好年份。**

闸门（regime gate）在**信号日**判定，只用当日及之前信息，不得前视：
  - marketMA{N}：基准指数（默认 SPY）收盘 > 其 N 日均线，才允许开仓；
  - stockMA{N} ：个股自身收盘 > 其 N 日均线，才允许开仓。

对照组：无过滤基线（dc55/2.0）。

判据：
  1. 坏年份（基线为负的年份）是否被救回；
  2. 好年份（基线大幅为正的年份）是否被保留；
  3. 多个 lookback 是否**一致**改善——只在某个 N 上有用即为拟合。

用法（需 Futu OpenD）：
    python scripts/run_donchian_regime.py
    python scripts/run_donchian_regime.py --benchmark US.QQQ --lookbacks 50,100,200
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_donchian_backtest import (  # noqa: E402
    add_indicators, fetch_daily_data, load_config, run_variant,
)
from scripts.analyze_backtest_portfolio import (  # noqa: E402
    bucket_metrics, max_drawdown, stability_summary,
)

DEFAULT_CHANNEL = 55
DEFAULT_ATR_MULT = 2.0
DEFAULT_LOOKBACKS = (50, 100, 200)
MIN_BUCKET_TRADES = 5
BENCHMARK = 'US.SPY'


def base_vdef(lookback_free=True):
    return {'key': 'dc55', 'label': '唐奇安55 + 2ATR吊灯', 'entry_n': DEFAULT_CHANNEL,
            'vol_ratio': None, 'ma_filter': None, 'exit': 'chandelier',
            'stop_mult': DEFAULT_ATR_MULT}


def build_market_gate(bench_df: pd.DataFrame, lookback: int, all_dates) -> dict:
    """基准指数均线闸门：返回 {date: risk_on}。缺失日期向前填充上一已知状态。

    bench_df 需含 date/close；用收盘价与该日 N 日均线比较（收盘时已知，无前视）。
    """
    d = bench_df.copy()
    d['date'] = pd.to_datetime(d['date']).dt.normalize()
    d = d.sort_values('date').drop_duplicates('date').set_index('date')
    ma = d['close'].rolling(lookback).mean()
    on = (d['close'] > ma)
    # 对齐到所有股票交易日：缺失（基准停牌/未上市）向前填充
    idx = pd.DatetimeIndex(sorted(set(on.index) | set(pd.to_datetime(list(all_dates)))))
    # 先转 nullable boolean 再 ffill，避免 object dtype 下填充触发弃用告警
    filled = on.reindex(idx).astype('boolean').ffill().fillna(False).astype(bool)
    return {ts: bool(v) for ts, v in filled.items()}


def add_stock_ma(ind: pd.DataFrame, lookbacks) -> pd.DataFrame:
    """给个股指标表加自身均线列（收盘时已知，无前视）。一次加多个 N。"""
    out = ind.copy()
    if isinstance(lookbacks, int):
        lookbacks = [lookbacks]
    for n in lookbacks:
        out[f'ma{n}'] = out['close'].rolling(n).mean()
    return out


def make_gate(kind, lookback, market_map):
    """构造 entry_allowed 回调。kind: None / 'market' / 'stock'。"""
    if kind is None:
        return None
    if kind == 'market':
        def gate(row, arr, i):
            ts = pd.Timestamp(row['date']).normalize()
            return market_map.get(ts, False)
        return gate
    if kind == 'stock':
        col = f'ma{lookback}'

        def gate(row, arr, i):
            m = row.get(col)
            return bool(np.isfinite(m) and row['close'] > m)
        return gate
    raise ValueError(f'未知闸门类型: {kind}')


def configs(lookbacks=DEFAULT_LOOKBACKS):
    out = [{'key': 'baseline', 'label': '无过滤（基线）', 'kind': None, 'lookback': None}]
    for n in lookbacks:
        out.append({'key': f'marketMA{n}', 'label': f'指数>MA{n}', 'kind': 'market',
                    'lookback': n})
    for n in lookbacks:
        out.append({'key': f'stockMA{n}', 'label': f'个股>MA{n}', 'kind': 'stock',
                    'lookback': n})
    return out


def run_experiment(ind_data: dict, bench_df: pd.DataFrame, cfgs) -> pd.DataFrame:
    """对每个配置跑一遍，返回逐笔明细（含 config 列）。"""
    all_dates = sorted(set().union(*[set(pd.to_datetime(df['date'])) for df in ind_data.values()]))
    frames = []
    for cfg in cfgs:
        market_map = (build_market_gate(bench_df, cfg['lookback'], all_dates)
                      if cfg['kind'] == 'market' else None)
        gate = make_gate(cfg['kind'], cfg['lookback'], market_map)
        rows = []
        for code, df in ind_data.items():
            for t in run_variant(df, base_vdef(), entry_allowed=gate):
                rows.append({**t, 'stock': code, 'config': cfg['key'],
                             'config_label': cfg['label']})
        if not rows:
            continue
        tdf = pd.DataFrame(rows)
        tdf['entry_date'] = pd.to_datetime(tdf['entry_date'])
        tdf['exit_date'] = pd.to_datetime(tdf['exit_date'])
        frames.append(tdf)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def summarize(tdf: pd.DataFrame) -> dict:
    closed = tdf[~tdf['open'].astype(bool)].sort_values('exit_date')
    if closed.empty:
        return {'trades': 0, 'win_rate': np.nan, 'expectancy': np.nan,
                'profit_factor': np.nan, 'total': np.nan, 'max_dd': np.nan,
                'pass_rate': np.nan}
    wins = closed[closed['net_pnl_usd'] > 0]
    losses = closed[closed['net_pnl_usd'] <= 0]
    gw = float(wins['net_pnl_usd'].sum())
    gl = float(abs(losses['net_pnl_usd'].sum()))
    stab = stability_summary(bucket_metrics(tdf, 'YS', group_col='config'),
                             tdf['config'].iloc[0], min_trades=MIN_BUCKET_TRADES,
                             group_col='config')
    return {'trades': len(closed),
            'win_rate': len(wins) / len(closed),
            'expectancy': float(closed['net_pnl_usd'].mean()),
            'profit_factor': gw / gl if gl else np.inf,
            'total': float(closed['net_pnl_usd'].sum()),
            'max_dd': max_drawdown(closed['net_pnl_usd'].cumsum()),
            'pass_rate': stab.get('pass_rate', np.nan)}


def yearly_expectancy(tdf: pd.DataFrame) -> pd.DataFrame:
    buckets = bucket_metrics(tdf, 'YS', group_col='config')
    return buckets.pivot(index='period', columns='config', values='expectancy')


def build_report(trades: pd.DataFrame, cfgs) -> str:
    order = [c['key'] for c in cfgs]
    labels = {c['key']: c['label'] for c in cfgs}
    lines = ['# 唐奇安突破 市场状态（regime）过滤实验\n']
    lines.append(f'- 生成时间: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'- 基准变体: dc{DEFAULT_CHANNEL}/{DEFAULT_ATR_MULT}（无过滤为基线）')
    lines.append('- 闸门在信号日判定，只用当日及之前信息（无前视）\n')

    if trades.empty:
        lines.append('> 没有产生任何交易，请检查数据区间与股票池。\n')
        return '\n'.join(lines) + '\n'

    def _n(v, f='{:,.0f}'):
        return '—' if v is None or (isinstance(v, float) and not np.isfinite(v)) else f.format(v)

    lines.append('## 1. 全期对比\n')
    lines.append('| 配置 | 笔数 | 胜率 | 期望/笔$ | 盈亏比 | 总收益$ | 最大回撤$ | 年度通过率 |')
    lines.append('|---|---|---|---|---|---|---|---|')
    for key in order:
        g = trades[trades['config'] == key]
        if g.empty:
            continue
        m = summarize(g)
        lines.append(f"| {labels[key]} | {m['trades']} | {_n(m['win_rate'], '{:.1%}')} | "
                     f"{_n(m['expectancy'])} | "
                     f"{'∞' if m['profit_factor'] == np.inf else _n(m['profit_factor'], '{:.2f}')} | "
                     f"{_n(m['total'])} | {_n(m['max_dd'])} | "
                     f"{_n(m['pass_rate'], '{:.0%}')} |")
    lines.append('')

    lines.append('## 2. 分年期望/笔（$）—— 坏年份是否救回、好年份是否保住\n')
    piv = yearly_expectancy(trades)
    piv = piv.reindex(columns=[c for c in order if c in piv.columns])
    lines.append('| 年份 | ' + ' | '.join(labels[c] for c in piv.columns) + ' |')
    lines.append('|' + '---|' * (len(piv.columns) + 1))
    for year, row in piv.iterrows():
        lines.append(f'| {year} | ' + ' | '.join(_n(v) for v in row) + ' |')
    lines.append('')

    # 相对基线的增减（按年）
    if 'baseline' in piv.columns:
        lines.append('## 3. 相对基线的差异（期望/笔，$）\n')
        lines.append('| 年份 | ' + ' | '.join(labels[c] for c in piv.columns if c != 'baseline') + ' |')
        lines.append('|' + '---|' * len(piv.columns))
        base = piv['baseline']
        for year in piv.index:
            cells = []
            for c in piv.columns:
                if c == 'baseline':
                    continue
                v = piv.loc[year, c]
                cells.append('—' if pd.isna(v) or pd.isna(base.get(year))
                             else f'{v - base[year]:+,.0f}')
            lines.append(f'| {year} | ' + ' | '.join(cells) + ' |')
        lines.append('')

    lines.append('## 4. 怎么读\n')
    lines.append('- **救回坏年份**：看基线上为负的年份（历史上是 2021 / 2023 / 2024），'
                 '过滤后是否转正或显著减亏。')
    lines.append('- **保住好年份**：看基线上大赚的年份（2025 / 2026），过滤后是否被削掉太多——'
                 '如果代价是把好年份也砍掉一半，那就不是好过滤器。')
    lines.append('- **一致性**：若只有某一个 lookback 有效、其余都无效，那多半是拟合；'
                 '多个 N 一致改善才可信。')
    lines.append('- **样本量限制**：每个「配置 × 年」的笔数很少，单格结论噪声大，'
                 '要看整行/整列的方向。')
    lines.append('- 本实验只改了「是否开仓」，未改止损、仓位与选股范围。')
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser(description='唐奇安突破 市场状态过滤实验')
    ap.add_argument('--start', default='2020-07-01')
    ap.add_argument('--end', default=pd.Timestamp.now().strftime('%Y-%m-%d'))
    ap.add_argument('--codes', default=None, help='默认取 config dip_buy.watch_list')
    ap.add_argument('--benchmark', default=BENCHMARK, help='市场状态基准，默认 US.SPY')
    ap.add_argument('--lookbacks', default=','.join(str(n) for n in DEFAULT_LOOKBACKS))
    ap.add_argument('--output', default=str(PROJECT_ROOT / 'backtests' / 'donchian_regime.md'))
    args = ap.parse_args()

    lookbacks = [int(x) for x in args.lookbacks.split(',') if x.strip()]
    cfg = load_config()
    codes = ([c.strip() for c in args.codes.split(',') if c.strip()] if args.codes
             else list(cfg.get('dip_buy', {}).get('watch_list', [])))
    if not codes:
        print('未找到观察池代码'); return 2

    print(f'拉取行情：{len(codes)} 只 + 基准 {args.benchmark}，{args.start}~{args.end}')
    data = fetch_daily_data(codes, args.start, args.end)
    bench = fetch_daily_data([args.benchmark], args.start, args.end).get(args.benchmark)
    if not data or bench is None or bench.empty:
        print('行情数据不足（含基准），退出'); return 3

    ind_data = {c: add_indicators(df) for c, df in data.items()}
    ind_data = {c: add_stock_ma(df, lookbacks) for c, df in ind_data.items()}

    cfgs = configs(lookbacks)
    trades = run_experiment(ind_data, bench, cfgs)
    report = build_report(trades, cfgs)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding='utf-8')
    trades.to_csv(out.with_name(out.stem + '_trades.csv'), index=False, encoding='utf-8')
    print(report)
    print(f'已写出: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
