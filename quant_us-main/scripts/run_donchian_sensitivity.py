#!/usr/bin/env python3
"""唐奇安突破参数敏感性扫描：确认 55/2.0 不是拟合出来的甜点。

在「通道长度 × ATR止损倍数」网格上重跑同一套规则，看表现是否在参数邻域内
平滑连续（稳健），还是只在某个点上突出（过拟合）。

判据（关键）：
  - **年度通过率曲面**：每个格子在各年是否为正。宽平台 = 稳健；孤峰 = 拟合。
  - 期望/盈亏比曲面应随参数平滑变化，不应出现孤立高点。

用法（需 Futu OpenD）：
    python scripts/run_donchian_sensitivity.py
    python scripts/run_donchian_sensitivity.py --channels 20,30,40,55,70 --atr-mults 1.5,2.0,2.5,3.0
    python scripts/run_donchian_sensitivity.py --codes US.MU,US.SOXL --start 2020-07-01

输出：
    backtests/donchian_sensitivity.md
    backtests/donchian_sensitivity_trades.csv
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
    bucket_metrics, stability_summary, max_drawdown,
)

DEFAULT_CHANNELS = (20, 30, 40, 55, 70)
DEFAULT_ATR_MULTS = (1.5, 2.0, 2.5, 3.0)
POSITION_USD = 5000.0
MIN_BUCKET_TRADES = 5


def make_vdef(channel: int, atr_mult: float) -> dict:
    """构造一个纯唐奇安突破 + 吊灯（无放量/无MA过滤）的变体定义。"""
    return {
        'key': f'dc{channel}_atr{atr_mult}',
        'label': f'唐奇安{channel} + {atr_mult}ATR吊灯',
        'entry_n': channel,
        'vol_ratio': None,
        'ma_filter': None,
        'exit': 'chandelier',
        'stop_mult': atr_mult,
    }


def sweep_trades(ind_data: dict, channels, atr_mults) -> pd.DataFrame:
    """在网格上逐格回测，返回**逐笔**明细（供前进分析等下游复用）。

    列含 channel / atr_mult / stock / entry_date / exit_date / net_pnl_usd / open 等。
    """
    frames = []
    for ch in channels:
        for mult in atr_mults:
            vdef = make_vdef(ch, mult)
            all_trades = []
            for code, df in ind_data.items():
                for t in run_variant(df, vdef):
                    all_trades.append({**t, 'stock': code,
                                       'variant': vdef['key'],
                                       'variant_label': vdef['label']})
            if not all_trades:
                continue
            tdf = pd.DataFrame(all_trades)
            tdf['channel'] = ch
            tdf['atr_mult'] = mult
            tdf['entry_date'] = pd.to_datetime(tdf['entry_date'])
            tdf['exit_date'] = pd.to_datetime(tdf['exit_date'])
            frames.append(tdf)
    if not frames:
        return pd.DataFrame(columns=['channel', 'atr_mult', 'stock', 'entry_date',
                                     'exit_date', 'net_pnl_usd', 'open'])
    return pd.concat(frames, ignore_index=True)


def summarize_cell(tdf: pd.DataFrame) -> dict:
    """单格汇总指标（tdf 为该格的逐笔明细）。"""
    closed = tdf[~tdf['open'].astype(bool)].sort_values('exit_date')
    if closed.empty:
        return {'trades': 0, 'win_rate': np.nan, 'expectancy': np.nan,
                'profit_factor': np.nan, 'total': np.nan, 'max_dd': np.nan,
                'pass_rate': np.nan, 'positive_years': 0, 'years': 0}
    wins = closed[closed['net_pnl_usd'] > 0]
    losses = closed[closed['net_pnl_usd'] <= 0]
    gw = float(wins['net_pnl_usd'].sum())
    gl = float(abs(losses['net_pnl_usd'].sum()))
    equity = closed['net_pnl_usd'].cumsum()
    buckets = bucket_metrics(tdf, 'YS')
    stab = stability_summary(buckets, tdf['variant'].iloc[0], min_trades=MIN_BUCKET_TRADES)
    return {
        'trades': len(closed),
        'win_rate': len(wins) / len(closed) if len(closed) else np.nan,
        'expectancy': float(closed['net_pnl_usd'].mean()) if len(closed) else np.nan,
        'profit_factor': gw / gl if gl else np.inf,
        'total': float(closed['net_pnl_usd'].sum()),
        'max_dd': max_drawdown(equity),
        'years': stab['buckets'],
        'positive_years': stab['positive'],
        'pass_rate': stab.get('pass_rate', np.nan),
    }


def sweep(ind_data: dict, channels, atr_mults) -> pd.DataFrame:
    """在网格上逐格回测，返回每格汇总指标的长表。"""
    trades = sweep_trades(ind_data, channels, atr_mults)
    rows = []
    for (ch, mult), tdf in trades.groupby(['channel', 'atr_mult']):
        rows.append({'channel': ch, 'atr_mult': mult, **summarize_cell(tdf)})
    # 补齐没有产生任何交易的格子，保证网格完整
    for ch in channels:
        for mult in atr_mults:
            if not any(r['channel'] == ch and r['atr_mult'] == mult for r in rows):
                rows.append({'channel': ch, 'atr_mult': mult, 'trades': 0,
                             'win_rate': np.nan, 'expectancy': np.nan,
                             'profit_factor': np.nan, 'total': np.nan,
                             'max_dd': np.nan, 'pass_rate': np.nan,
                             'positive_years': 0, 'years': 0})
    return pd.DataFrame(rows).sort_values(['channel', 'atr_mult']).reset_index(drop=True)


def _surface(df: pd.DataFrame, value: str, fmt_fn) -> list:
    """把长表透视成 channel(行) × atr_mult(列) 的 markdown 表。"""
    piv = df.pivot(index='channel', columns='atr_mult', values=value).sort_index()
    piv = piv.reindex(columns=sorted(piv.columns))
    lines = ['| 通道 \\ ATR倍数 | ' + ' | '.join(f'{c}' for c in piv.columns) + ' |',
             '|' + '---|' * (len(piv.columns) + 1)]
    for ch, row in piv.iterrows():
        lines.append(f'| dc{int(ch)} | ' + ' | '.join(fmt_fn(v) for v in row) + ' |')
    return lines


def build_report(results: pd.DataFrame, channels, atr_mults,
                 default_channel: int = 55, default_mult: float = 2.0) -> str:
    lines = ['# 唐奇安突破 参数敏感性扫描\n']
    lines.append(f'- 生成时间: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'- 网格: 通道 {list(channels)} × ATR倍数 {list(atr_mults)}，'
                 f'共 {len(results)} 格；每格为同一套规则（无放量/无MA过滤 + 吊灯止损）')
    lines.append(f'- 判定基准: 默认参数 dc{default_channel}/{default_mult}ATR\n')
    lines.append('> 读法：**通过率 = 各年度期望为正的比例**。若高通过率只出现在单一格子，'
                 '说明是拟合出来的甜点；若一整片邻域都高，说明参数稳健。\n')

    def _f(v):
        return '—' if v is None or (isinstance(v, float) and not np.isfinite(v)) else f'{v:,.0f}'

    def _fp(v):
        return '—' if v is None or (isinstance(v, float) and not np.isfinite(v)) else f'{v*100:.0f}%'

    def _fpf(v):
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            return '—'
        return '∞' if v == np.inf else f'{v:.2f}'

    lines.append('## 1. 年度通过率曲面（核心判据）\n')
    lines.extend(_surface(results, 'pass_rate', _fp))
    lines.append('')
    lines.append('## 2. 期望/笔 曲面（$）\n')
    lines.extend(_surface(results, 'expectancy', _f))
    lines.append('')
    lines.append('## 3. 盈亏比 曲面\n')
    lines.extend(_surface(results, 'profit_factor', _fpf))
    lines.append('')
    lines.append('## 4. 最大回撤 曲面（$，越低越好）\n')
    lines.extend(_surface(results, 'max_dd', _f))
    lines.append('')
    lines.append('## 5. 笔数（样本厚度）\n')
    lines.extend(_surface(results, 'trades', lambda v: '—' if pd.isna(v) else f'{int(v)}'))
    lines.append('')

    # 默认格 vs 全网最优
    lines.append('## 6. 默认参数 vs 全网最优\n')
    lines.append('| 格子 | 通道 | ATR倍数 | 笔数 | 通过率 | 期望/笔 | 盈亏比 | 最大回撤$ |')
    lines.append('|---|---|---|---|---|---|---|---|')
    best = results.dropna(subset=['expectancy']).sort_values('expectancy', ascending=False)
    default_row = results[(results['channel'] == default_channel) &
                          (np.isclose(results['atr_mult'], default_mult))]
    show = []
    if not default_row.empty:
        show.append(('默认', default_row.iloc[0]))
    if not best.empty:
        show.append(('最优', best.iloc[0]))
    # 中位数格（稳健性的参考）
    med = results.dropna(subset=['expectancy'])
    if not med.empty:
        show.append(('中位', med.iloc[(med['expectancy'] - med['expectancy'].median()).abs().argsort().iloc[0]]))
    for tag, r in show:
        lines.append(f'| {tag} | dc{int(r["channel"])} | {r["atr_mult"]} | {int(r["trades"])} | '
                     f'{_fp(r["pass_rate"])} | {_f(r["expectancy"])} | {_fpf(r["profit_factor"])} | '
                     f'{_f(r["max_dd"])} |')
    lines.append('')

    lines.append('## 7. 结论提示\n')
    lines.append('- 若「最优格子」与「默认格子」的期望差距很大、而相邻格子明显更差，'
                 '说明默认参数处于孤峰，样本外大概率回落，应选邻域平均更好的那片。')
    lines.append('- 若整片曲面的通过率都在 70% 以上，说明参数不敏感，默认值可接受。')
    lines.append('- 本扫描仍在同一段历史上做，只能证明「参数是否稳健」，不能替代真正的样本外验证。'
                 '最终应以时间上的前进分析（如前 N 年选参、后 M 年验证）为准。')
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser(description='唐奇安突破参数敏感性扫描')
    ap.add_argument('--start', default='2020-07-01')
    ap.add_argument('--end', default=pd.Timestamp.now().strftime('%Y-%m-%d'))
    ap.add_argument('--codes', default=None, help='逗号分隔，默认取 config dip_buy.watch_list')
    ap.add_argument('--channels', default=','.join(str(c) for c in DEFAULT_CHANNELS))
    ap.add_argument('--atr-mults', default=','.join(str(m) for m in DEFAULT_ATR_MULTS))
    ap.add_argument('--output', default=str(PROJECT_ROOT / 'backtests' / 'donchian_sensitivity.md'))
    args = ap.parse_args()

    channels = [int(x) for x in args.channels.split(',') if x.strip()]
    atr_mults = [float(x) for x in args.atr_mults.split(',') if x.strip()]

    cfg = load_config()
    codes = ([c.strip() for c in args.codes.split(',') if c.strip()] if args.codes
             else list(cfg.get('dip_buy', {}).get('watch_list', [])))
    if not codes:
        print('未找到观察池代码'); return 2

    print(f'扫描网格: 通道{channels} × ATR{atr_mults}，股票 {len(codes)} 只')
    data = fetch_daily_data(codes, args.start, args.end)
    if not data:
        print('没有行情数据'); return 3
    # 一次性算好所有通道的并集，避免每格重复计算
    ind_data = {c: add_indicators(df, channels=channels) for c, df in data.items()}

    trades = sweep_trades(ind_data, channels, atr_mults)
    rows = []
    for (ch, mult), tdf in trades.groupby(['channel', 'atr_mult']):
        rows.append({'channel': ch, 'atr_mult': mult, **summarize_cell(tdf)})
    results = pd.DataFrame(rows).sort_values(['channel', 'atr_mult']).reset_index(drop=True)
    report = build_report(results, channels, atr_mults)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding='utf-8')
    results.to_csv(out.with_name(out.stem + '_grid.csv'), index=False, encoding='utf-8')
    # 逐笔明细：前进分析（run_donchian_walkforward.py）直接复用它，避免重复拉行情
    trades_out = out.with_name(out.stem + '_trades.csv')
    trades.to_csv(trades_out, index=False, encoding='utf-8')
    print(f'逐笔明细: {trades_out}（{len(trades)} 行）')
    print(results.to_string(index=False))
    print(f'\n已写出: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
