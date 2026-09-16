"""基于逐日组合净值计算中期策略绩效。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _curve(frame: pd.DataFrame, value_col: str) -> pd.DataFrame:
    required = {'session', value_col}
    if missing := required - set(frame.columns):
        raise ValueError(f'EQUITY_COLUMNS_MISSING:{",".join(sorted(missing))}')
    d = frame[['session', value_col]].copy()
    d['session'] = pd.to_datetime(d.session).dt.tz_localize(None).dt.normalize()
    d[value_col] = pd.to_numeric(d[value_col], errors='coerce')
    if d.session.duplicated().any():
        raise ValueError('DUPLICATE_EQUITY_SESSION')
    if d.empty or (~np.isfinite(d[value_col]) | (d[value_col] <= 0)).any():
        raise ValueError('INVALID_EQUITY_CURVE')
    return d.sort_values('session').reset_index(drop=True)


def performance_metrics(equity: pd.DataFrame, benchmark: pd.DataFrame | None = None, *,
                        equity_col='equity', benchmark_col='equity') -> dict:
    """计算组合指标；benchmark 应是同成本口径的逐日净值或价格指数。"""
    d = _curve(equity, equity_col)
    initial = (float(equity.initial_equity.iloc[0])
               if 'initial_equity' in equity and pd.notna(equity.initial_equity.iloc[0])
               else float(d[equity_col].iloc[0]))
    final = float(d[equity_col].iloc[-1])
    elapsed_years = (d.session.iloc[-1] - d.session.iloc[0]).days / 365.2425
    total_return = final / initial - 1
    cagr = ((final / initial) ** (1 / elapsed_years) - 1
            if elapsed_years >= 1 else None)
    returns = d[equity_col].pct_change(fill_method=None)
    if 'initial_equity' in equity:
        returns.iloc[0] = float(d[equity_col].iloc[0]) / initial - 1
    returns = returns.dropna()
    volatility = float(returns.std(ddof=1) * math.sqrt(252)) if len(returns) > 1 else None
    sharpe = (float(returns.mean() / returns.std(ddof=1) * math.sqrt(252))
              if len(returns) > 1 and returns.std(ddof=1) > 0 else None)
    drawdown = d[equity_col] / d[equity_col].cummax().clip(lower=initial) - 1
    trough = int(drawdown.idxmin())
    peak = int(d.loc[:trough, equity_col].idxmax())
    if float(d.loc[:trough, equity_col].max()) < initial:
        peak = 0  # 初始资金高水位在首日开盘前。
    max_drawdown = float(drawdown.iloc[trough])
    calmar = (float(cagr / abs(max_drawdown))
              if cagr is not None and max_drawdown < 0 else None)
    result = {
        'start_date': str(d.session.iloc[0].date()),
        'end_date': str(d.session.iloc[-1].date()),
        'initial_equity': initial, 'final_equity': final,
        'total_return': total_return, 'CAGR': cagr,
        'annualized_volatility': volatility, 'Sharpe': sharpe,
        'max_drawdown': max_drawdown,
        'max_drawdown_start': str(d.session.iloc[peak].date()),
        'max_drawdown_end': str(d.session.iloc[trough].date()),
        'Calmar': calmar,
    }
    if 'gross_exposure' in equity:
        result['average_gross_exposure'] = float(equity.gross_exposure.mean())
        result['cash_ratio'] = float(1 - equity.gross_exposure.mean())
    if 'cumulative_turnover' in equity:
        result['turnover'] = float(equity.cumulative_turnover.iloc[-1] / d[equity_col].mean())

    if benchmark is not None:
        b = _curve(benchmark, benchmark_col).rename(columns={benchmark_col: 'benchmark'})
        joined = d.merge(b, on='session', how='inner')
        if len(joined) != len(d) or len(joined) != len(b):
            raise ValueError('BENCHMARK_SESSIONS_MISMATCH')
        if len(joined) < 2:
            raise ValueError('BENCHMARK_OVERLAP_INSUFFICIENT')
        bench_initial = (float(benchmark.initial_equity.iloc[0])
                         if 'initial_equity' in benchmark and pd.notna(benchmark.initial_equity.iloc[0])
                         else float(joined.benchmark.iloc[0]))
        bench_final = float(joined.benchmark.iloc[-1])
        bench_years = (joined.session.iloc[-1] - joined.session.iloc[0]).days / 365.2425
        bench_cagr = ((bench_final / bench_initial) ** (1 / bench_years) - 1
                      if bench_years >= 1 else None)
        result['benchmark_CAGR'] = bench_cagr
        result['excess_CAGR'] = cagr - bench_cagr if cagr is not None and bench_cagr is not None else None
        strategy_252 = joined[equity_col] / joined[equity_col].shift(252) - 1
        benchmark_252 = joined.benchmark / joined.benchmark.shift(252) - 1
        valid = strategy_252.notna() & benchmark_252.notna()
        result['rolling_12m_windows'] = int(valid.sum())
        result['rolling_12m_win_rate_vs_benchmark'] = (
            float((strategy_252[valid] > benchmark_252[valid]).mean()) if valid.any() else None)
    return result
