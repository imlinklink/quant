#!/usr/bin/env python3
"""唐奇安突破 时间前进分析（walk-forward / 样本外验证）。

回答「这套规则能不能用」，而不是「参数在历史上稳不稳」：

  锚定（expanding）前进：每一折用**该年之前的所有年份**选参，只在**下一年**上验证。
  逐年滚动，最后把各折的样本外结果拼起来。

三个核心指标：
  1. **IC（训练/测试的秩相关）**：训练期表现能否预测测试期表现。
     IC 接近 0 → 选参无效，历史最优参数对未来没有信息量。
  2. **选参是否战胜中位**：每年选出的「最优格」在样本外是否好于同期所有格的中位数。
  3. **前进组合 vs 固定默认**：每年重选参数的样本外总收益，对比一直用 55/2.0 的总收益。
     若前者不显著优于后者，说明「优化参数」没有实际价值，应直接用默认值。

数据来源：优先读敏感性扫描产出的逐笔 CSV；缺失则现场拉行情重跑（需 OpenD）。

用法：
    python scripts/run_donchian_walkforward.py
    python scripts/run_donchian_walkforward.py --trades-csv backtests/donchian_sensitivity_trades.csv
    python scripts/run_donchian_walkforward.py --fetch --start 2020-07-01
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_donchian_sensitivity import (  # noqa: E402
    DEFAULT_ATR_MULTS, DEFAULT_CHANNELS, make_vdef, sweep_trades,
)
from scripts.run_donchian_backtest import add_indicators, fetch_daily_data, load_config  # noqa: E402

DEFAULT_CELL = (55, 2.0)
MIN_TRAIN_TRADES = 30
MIN_TEST_TRADES = 5


def spearman(x, y) -> float:
    """秩相关（并列取平均秩）。样本不足或秩无变化返回 NaN。"""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 3:
        return float('nan')
    rx = pd.Series(x[mask]).rank().values
    ry = pd.Series(y[mask]).rank().values
    if np.std(rx) == 0 or np.std(ry) == 0:
        return float('nan')
    return float(np.corrcoef(rx, ry)[0, 1])


def cell_expectancy(tdf: pd.DataFrame) -> float:
    """一格在给定区间内的期望/笔（仅已平仓）。"""
    closed = tdf[~tdf['open'].astype(bool)]
    return float(closed['net_pnl_usd'].mean()) if len(closed) else float('nan')


def walk_forward(trades: pd.DataFrame, metric: str = 'expectancy',
                 min_train: int = MIN_TRAIN_TRADES,
                 min_test: int = MIN_TEST_TRADES,
                 default_cell=DEFAULT_CELL) -> pd.DataFrame:
    """逐年锚定前进。返回每折一行。

    每折：train = 该年之前全部；test = 该年。用 train 选出最优格，看它在 test 的表现。
    """
    d = trades.copy()
    d = d[~d['open'].astype(bool)]
    d['entry_year'] = pd.to_datetime(d['entry_date']).dt.year
    years = sorted(d['entry_year'].unique())
    rows = []
    for test_year in years[1:]:
        train = d[d['entry_year'] < test_year]
        test = d[d['entry_year'] == test_year]
        if len(train) < min_train or len(test) < min_test:
            continue
        train_metric, test_metric = {}, {}
        for cell in sorted(set(zip(train['channel'], train['atr_mult']))):
            tr = train[(train['channel'] == cell[0]) & (train['atr_mult'] == cell[1])]
            te = test[(test['channel'] == cell[0]) & (test['atr_mult'] == cell[1])]
            if len(tr) < 5 or len(te) < 2:
                continue
            train_metric[cell] = cell_expectancy(tr)
            test_metric[cell] = cell_expectancy(te)
        if len(train_metric) < 3:
            continue
        cells = list(train_metric)
        ic = spearman([train_metric[c] for c in cells], [test_metric[c] for c in cells])
        best = max(cells, key=lambda c: train_metric[c])
        test_vals = [test_metric[c] for c in cells]
        med = float(np.nanmedian(test_vals))
        default_val = test_metric.get(default_cell, float('nan'))
        rows.append({
            'test_year': test_year,
            'train_trades': len(train),
            'test_trades': len(test),
            'cells': len(cells),
            'ic': ic,
            'train_best': f'dc{best[0]}/{best[1]}',
            'train_best_metric': train_metric[best],
            'test_best': test_metric[best],
            'test_median': med,
            'test_default': default_val,
            'beat_median': test_metric[best] > med,
        })
    return pd.DataFrame(rows)


def held_out_pool(trades: pd.DataFrame, folds: pd.DataFrame,
                  default_cell=DEFAULT_CELL) -> dict:
    """把各折测试期拼起来：① 每年重选参数的「前进组合」② 固定默认参数。

    两者用**同一批测试年份**，可直接对比样本外总收益。
    """
    d = trades[~trades['open'].astype(bool)].copy()
    d['entry_year'] = pd.to_datetime(d['entry_date']).dt.year
    # 无折（样本不足）时不能假设列存在
    if folds is None or folds.empty or 'test_year' not in folds.columns:
        return {}
    years = set(folds['test_year'])
    if not years:
        return {}
    sub = d[d['entry_year'].isin(years)]

    # ① 前进组合：每折取该折训练最优格在该年的交易
    picked = []
    for _, f in folds.iterrows():
        ch, mult = _parse_cell(f['train_best'])
        cell = sub[(sub['entry_year'] == f['test_year']) &
                   (sub['channel'] == ch) & (sub['atr_mult'] == mult)]
        picked.append(cell)
    wf = pd.concat(picked, ignore_index=True) if picked else pd.DataFrame()

    # ② 固定默认：同样年份上一直用默认格
    fixed = sub[(sub['channel'] == default_cell[0]) & (sub['atr_mult'] == default_cell[1])]

    def _stat(x):
        return {'trades': len(x),
                'total': float(x['net_pnl_usd'].sum()) if len(x) else 0.0,
                'expectancy': float(x['net_pnl_usd'].mean()) if len(x) else float('nan')}
    return {'years': sorted(years), 'walkforward': _stat(wf), 'fixed_default': _stat(fixed)}


def _parse_cell(tag: str):
    """'dc55/2.0' → (55, 2.0)"""
    ch_part, mult_part = str(tag).split('/')
    return int(ch_part.replace('dc', '')), float(mult_part)


def build_report(folds: pd.DataFrame, pool: dict, default_cell=DEFAULT_CELL) -> str:
    lines = ['# 唐奇安突破 时间前进分析（样本外验证）\n']
    lines.append(f'- 生成时间: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'- 方法: 锚定前进 —— 每折用该年之前全部年份选参，只在该年验证')
    lines.append(f'- 选参准则: 训练期期望/笔；对比基准: 55/2.0 固定参数（`dc{default_cell[0]}/{default_cell[1]}`）\n')

    if folds.empty:
        lines.append('> 没有足够的样本完成任何一折（训练期或测试期笔数不足）。'
                     '请拉长区间或扩大股票池后重跑。\n')
        return '\n'.join(lines) + '\n'

    lines.append('## 1. 逐折结果\n')
    lines.append('| 测试年 | 训练笔数 | 测试笔数 | 可选格数 | IC | 训练最优格 | 该格测试期望$ | '
                 '测试中位$ | 默认格测试期望$ | 最优>中位 |')
    lines.append('|---|---|---|---|---|---|---|---|---|---|')

    def _n(v, fmt='{:,.0f}'):
        return '—' if v is None or (isinstance(v, float) and not np.isfinite(v)) else fmt.format(v)
    for _, f in folds.iterrows():
        lines.append(
            f"| {int(f['test_year'])} | {int(f['train_trades'])} | {int(f['test_trades'])} | "
            f"{int(f['cells'])} | {_n(f['ic'], '{:.2f}')} | {f['train_best']} | "
            f"{_n(f['test_best'])} | {_n(f['test_median'])} | {_n(f['test_default'])} | "
            f"{'✓' if f['beat_median'] else '✗'} |")
    lines.append('')

    ics = [f for f in folds['ic'] if np.isfinite(f)]
    beat = int(folds['beat_median'].sum())
    lines.append('## 2. 汇总\n')
    lines.append(f"- 折数: {len(folds)}；平均 IC = "
                 f"{(np.mean(ics) if ics else float('nan')):.2f}"
                 f"（正=训练期表现能预测测试期；接近 0=选参无信息量）")
    lines.append(f"- 训练最优格在样本外战胜中位数的比例: {beat}/{len(folds)} "
                 f"（{beat/len(folds)*100:.0f}%）")
    lines.append('')

    if pool:
        wf, fx = pool['walkforward'], pool['fixed_default']
        lines.append('## 3. 前进组合 vs 固定默认（同一批测试年份）\n')
        lines.append(f"测试年份: {', '.join(str(y) for y in pool['years'])}\n")
        lines.append('| 方案 | 笔数 | 样本外总收益$ | 期望/笔$ |')
        lines.append('|---|---|---|---|')
        lines.append(f"| 每年重选参数（前进组合） | {wf['trades']} | {_n(wf['total'])} | "
                     f"{_n(wf['expectancy'])} |")
        lines.append(f"| 固定 dc{default_cell[0]}/{default_cell[1]} | {fx['trades']} | "
                     f"{_n(fx['total'])} | {_n(fx['expectancy'])} |")
        diff = wf['total'] - fx['total']
        verdict = ('重选参数有实际价值' if diff > 0.15 * abs(fx['total']) and fx['total'] else
                   '重选参数没有明显价值，建议直接用固定参数')
        lines.append(f"\n差异: {_n(diff)} → **{verdict}**\n")

    lines.append('## 4. 怎么读\n')
    lines.append('- **IC 是主判据**：如果各折 IC 平均接近 0 甚至为负，说明「用历史挑参数」'
                 '这件事本身没有信息量，任何参数优化的结果都不能外推。')
    lines.append('- **最优格战胜中位的比例**：长期看应该在 50% 附近徘徊（相当于掷硬币）；'
                 '显著高于 50% 才说明选参有效。')
    lines.append('- **前进组合 vs 固定默认**是最终答案：只有前者明显更好，才值得做参数寻优；'
                 '否则老老实实用默认值，把精力放在仓位和风控上。')
    lines.append('- 本分析仍受样本量限制：每年只有十几到几十笔，单折结果噪声大，'
                 '应看多折的整体方向而非单折。')
    return '\n'.join(lines) + '\n'


def main():
    ap = argparse.ArgumentParser(description='唐奇安突破 时间前进分析')
    ap.add_argument('--trades-csv',
                    default=str(PROJECT_ROOT / 'backtests' / 'donchian_sensitivity_trades.csv'))
    ap.add_argument('--fetch', action='store_true',
                    help='忽略本地逐笔 CSV，现场拉行情重跑网格（需 OpenD）')
    ap.add_argument('--start', default='2020-07-01')
    ap.add_argument('--end', default=pd.Timestamp.now().strftime('%Y-%m-%d'))
    ap.add_argument('--codes', default=None)
    ap.add_argument('--channels', default=','.join(str(c) for c in DEFAULT_CHANNELS))
    ap.add_argument('--atr-mults', default=','.join(str(m) for m in DEFAULT_ATR_MULTS))
    ap.add_argument('--output', default=str(PROJECT_ROOT / 'backtests' / 'donchian_walkforward.md'))
    args = ap.parse_args()

    trades_path = Path(args.trades_csv)
    if not args.fetch and trades_path.exists():
        print(f'读取逐笔明细: {trades_path}')
        trades = pd.read_csv(trades_path)
        trades['entry_date'] = pd.to_datetime(trades['entry_date'])
        trades['exit_date'] = pd.to_datetime(trades['exit_date'])
    else:
        channels = [int(x) for x in args.channels.split(',') if x.strip()]
        atr_mults = [float(x) for x in args.atr_mults.split(',') if x.strip()]
        cfg = load_config()
        codes = ([c.strip() for c in args.codes.split(',') if c.strip()] if args.codes
                 else list(cfg.get('dip_buy', {}).get('watch_list', [])))
        if not codes:
            print('未找到观察池代码'); return 2
        print(f'本地无逐笔明细，现场拉取 {len(codes)} 只 {args.start}~{args.end}')
        data = fetch_daily_data(codes, args.start, args.end)
        if not data:
            print('没有行情数据'); return 3
        ind_data = {c: add_indicators(df, channels=channels) for c, df in data.items()}
        trades = sweep_trades(ind_data, channels, atr_mults)
        trades.to_csv(trades_path, index=False, encoding='utf-8')
        print(f'已写出逐笔明细: {trades_path}')

    folds = walk_forward(trades)
    pool = held_out_pool(trades, folds)
    report = build_report(folds, pool)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding='utf-8')
    if not folds.empty:
        folds.to_csv(out.with_name(out.stem + '_folds.csv'), index=False, encoding='utf-8')
    print(report)
    print(f'已写出: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
