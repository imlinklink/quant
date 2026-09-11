#!/usr/bin/env python3
"""汇总 A/B/C/D 买入实验逐笔 CSV；不负责用同一数据重新生成信号。"""
import argparse
from pathlib import Path

import pandas as pd

from scripts.portfolio_backtest import apply_portfolio_constraints


EXPERIMENTS = {
    'A': '日线Setup + 次日开盘',
    'B': '周线环境门 + 日线Setup + 次日开盘',
    'C': '周线环境门 + 日线反转确认 + 次日开盘',
    'D': 'C组 + LLM研究过滤',
}


def summarize(frames, max_positions=3):
    rows = []
    for key, frame in frames.items():
        accepted, rejected = apply_portfolio_constraints(frame, max_positions)
        closed = (accepted[~accepted['open'].astype(bool)]
                  if not accepted.empty and 'open' in accepted else accepted)
        pnl = closed.get('net_pnl_usd', pd.Series(dtype=float)).astype(float)
        mae = closed.get('mae_pct', pd.Series(dtype=float)).astype(float)
        rows.append({'experiment': key, 'label': EXPERIMENTS[key], 'signals': len(frame),
                     'trades': len(closed), 'portfolio_rejected': len(rejected),
                     'total_net_usd': float(pnl.sum()),
                     'expectancy_usd': float(pnl.mean()) if len(pnl) else None,
                     'win_rate': float((pnl > 0).mean()) if len(pnl) else None,
                     'mean_mae_pct': float(mae.mean()) if len(mae) else None})
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description='A/B/C/D 买入策略实验汇总')
    for key in EXPERIMENTS:
        parser.add_argument(f'--{key.lower()}', required=True, help=f'{key} 组逐笔 CSV')
    parser.add_argument('--max-positions', type=int, default=3)
    parser.add_argument('--output', default='backtests/buy_strategy_abcd.csv')
    args = parser.parse_args()
    frames = {k: pd.read_csv(getattr(args, k.lower())) for k in EXPERIMENTS}
    out = summarize(frames, args.max_positions)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(args.output, index=False)
    print(out.to_string(index=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
