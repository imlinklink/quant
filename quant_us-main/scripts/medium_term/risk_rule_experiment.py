"""P1 同风险规则实验：统一口径下找 ~20% 回撤约束的规则基线。

在**同一股票池（13 tech）、同一评价窗口（union，空闲段持现金）、同一成本（round-trip 0.2%）、
同一资金（$100k）、同一执行规则（入场日硬止损 + 五仓限制）** 下，比较三条规则策略
P1（固定 A 入场）/ B2（月度动量前 5，T+1 开盘）/ B3（动量前 5 + 周线门 + 日线择时），
在 单笔风险{0.5%,0.75%,1.0%} × 持有上限{60,90} 网格上产出账户指标；P1@20 作为低回撤参照。

「单笔风险 X%」== `simulate_multi_asset_portfolio(risk_fraction=X/100)`：`risk_sized_shares`
用 `min(nav*risk_fraction/distance, nav*max_weight/price, cash)`。矩阵（每 entry 的 gross/net
pnl、entry/exit、exit_phase）与 risk_fraction 无关，建一次复用。

筛选：MDD 硬界 ≤20%（缓冲目标 15–18%），在硬界内取 Pareto 前沿，兼顾年度稳定性、盈利集中度、
压力成本（单边 0.2%/0.3%）后冻结一个前瞻基线 manifest；无合格方案则如实记录 `no_qualified_baseline`。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

from .action_coverage import audit_action_coverage, blocked_sessions
from .performance import performance_metrics
from .portfolio_engine import simulate_multi_asset_portfolio
from .qqq_benchmark import build_qqq_benchmark
from .p1_account_check import (ACTIONS, ENTRIES as P1_ENTRIES, ETF_RAW, NON_TECH,
                               QQQ_DIVIDENDS, QUALITY, _load_adjusted,
                               _load_qqq_dividends, _load_qqq_prices,
                               build_matrix as p1_build_matrix, prepare_entries)
from .p2_selection_check import (PANELS, TECH, TOP_N, build_entries, build_matrix as p2_build_matrix,
                                 build_timed_entries, generate_monthly_candidates, load_panels,
                                 market_frame, trading_calendar)

STRATEGIES = ('P1', 'B2', 'B3')
RISK_FRACTIONS = (('0.5pct', 0.005), ('0.75pct', 0.0075), ('1.0pct', 0.01))
GRID_HORIZONS = (60, 90)
REFERENCE_HORIZON = 20
COST = .002  # round-trip；fee_rate = COST/2 每腿（单边 0.1%）
INITIAL_CASH = 100_000.
MAX_WEIGHT = .20
MAX_POSITIONS = 5
MDD_HARD_BOUND = .20
MDD_BUFFER = (.15, .18)
STRESS_ROUND_TRIP = (.004, .006)  # 单边 0.2% / 0.3%

ROOT = Path(__file__).resolve().parents[2]


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _build_shared_audits(prices: pd.DataFrame, actions: pd.DataFrame) -> tuple[dict, dict]:
    """13 tech 的公司行动覆盖审计；P1/B2/B3 共用同一行动门。"""
    audits, blocked = {}, {}
    for sid in TECH:
        raw_window = (prices.loc[prices.security_id.eq(sid), ['session', 'raw_close']]
                      .rename(columns={'raw_close': 'close'}))
        audit = audit_action_coverage(raw_window, _load_adjusted(sid.replace('SEC-US-', 'US_')),
                                      actions, sid)
        audits[sid] = audit
        blocked[sid] = blocked_sessions(audit)
    return audits, blocked


def _build_p1_matrix(prices: pd.DataFrame, quality: pd.DataFrame, actions: pd.DataFrame,
                     blocked: dict) -> tuple[pd.DataFrame, dict]:
    """P1：固定 A 入场（tech13，排除 BAC/HD），原始价 + 硬止损。"""
    entries = pd.read_csv(P1_ENTRIES)
    entries = entries[entries.experiment.astype(str).eq('A')]
    entries = entries[~entries.security_id.astype(str).isin(NON_TECH)]
    prepared, funnel = prepare_entries(entries, quality, prices, blocked_dates=blocked,
                                       common_window=True)
    if prepared.empty:
        raise ValueError('NO_PREPARED_P1_ENTRIES')
    matrix = p1_build_matrix(prepared, prices, actions, quality_end=None)
    return matrix, funnel


def _build_p2_matrices(prices: pd.DataFrame, quality: pd.DataFrame, actions: pd.DataFrame,
                       blocked: dict, etf_path: Path) -> tuple[dict, dict]:
    """B2/B3：月度动量前 5（B2 T+1 开盘 / B3 周线门+日线择时）。"""
    view_bars = prices[['security_id', 'session', 'raw_open', 'raw_high', 'raw_low',
                        'raw_close', 'volume']].rename(columns={
        'raw_open': 'open', 'raw_high': 'high', 'raw_low': 'low', 'raw_close': 'close'})
    candidates = generate_monthly_candidates(view_bars, market_frame(etf_path),
                                             top_n=TOP_N, actions=actions)
    selected = candidates[candidates.selected.astype(bool)].copy()
    timed = build_timed_entries(
        selected[['security_id', 'decision_session', 'execution_session', 'rank']],
        prices[['security_id', 'session', 'raw_open', 'raw_high', 'raw_low',
                'raw_close', 'volume']],
        trading_calendar(etf_path), actions=actions)
    specs = {'B2': (selected, 'decision_session'),
             'B3': (timed[timed.entry_type.ne('EXPIRED') & timed.execution_session.notna()],
                    'signal_session')}
    matrices, funnels = {}, {}
    for strategy, (spec, atr_col) in specs.items():
        prepared, funnel = build_entries(spec, prices, quality, blocked,
                                         atr_session_col=atr_col)
        if prepared.empty:
            raise ValueError(f'NO_PREPARED_ENTRIES:{strategy}')
        matrix = p2_build_matrix(prepared, prices, actions)
        matrices[strategy] = matrix
        funnels[strategy] = {**funnel, 'position_limit': {
            str(k): int(v) for k, v in matrix.loc[
                ~matrix.portfolio_accepted.astype(bool), 'portfolio_reject_reason'
            ].value_counts().items()}}
    return matrices, funnels


def _annual_stability(equity: pd.DataFrame) -> dict:
    """历年简单收益、最差年、下跌年占比（来自逐日净值）。"""
    d = equity[['session', 'equity']].copy()
    d['session'] = pd.to_datetime(d.session).dt.tz_localize(None).dt.normalize()
    d = d.sort_values('session').set_index('session')
    annual = d.equity.groupby(d.index.year).agg(first='first', last='last')
    returns = annual['last'] / annual['first'] - 1
    return {'annual_returns': {int(y): float(r) for y, r in returns.items()},
            'worst_year_return': float(returns.min()),
            'best_year_return': float(returns.max()),
            'down_years': int((returns < 0).sum()),
            'down_year_ratio': float((returns < 0).mean()),
            'n_years': int(len(returns))}


def _concentration(trades: pd.DataFrame) -> dict:
    """已实现交易层面的盈利集中度（不含未平仓与分红）。"""
    if trades.empty:
        return {'top1_winner_share': None, 'top5_winner_share': None, 'win_rate': None,
                'total_realized_pnl': 0., 'pnl_ex_top1': 0., 'closed_trades': 0}
    buys = (trades[trades.side.eq('BUY')].groupby('trade_id')
            .agg(buy_notional=('gross_notional', 'sum'), buy_fee=('fee', 'sum')))
    sells = (trades[trades.side.eq('SELL')].groupby('trade_id')
             .agg(sell_notional=('gross_notional', 'sum'), sell_fee=('fee', 'sum')))
    closed = buys.join(sells, how='inner')
    pnl = closed.sell_notional - closed.sell_fee - closed.buy_notional - closed.buy_fee
    total = float(pnl.sum())
    if total > 0:
        top1 = float(pnl.nlargest(1).sum()) / total
        top5 = float(pnl.nlargest(5).sum()) / total
    else:
        top1 = top5 = None
    return {'top1_winner_share': top1, 'top5_winner_share': top5,
            'win_rate': float((pnl > 0).mean()) if len(pnl) else None,
            'total_realized_pnl': total,
            'pnl_ex_top1': total - float(pnl.max()) if len(pnl) else 0.,
            'closed_trades': int(len(pnl))}


def _run_config(strategy: str, horizon: int, rf_label: str, rf: float, group: pd.DataFrame,
                window_prices: pd.DataFrame, actions: pd.DataFrame, benchmark: pd.DataFrame,
                common_start, common_end, *, round_trip_cost: float = COST) -> tuple[dict, pd.DataFrame,
                                                                                    pd.DataFrame,
                                                                                    pd.DataFrame]:
    """跑一个 (strategy, horizon, risk_fraction) 账户；position 限制统一走运行时拒绝。"""
    portfolio = simulate_multi_asset_portfolio(
        window_prices, group, initial_cash=INITIAL_CASH, risk_fraction=rf,
        max_weight=MAX_WEIGHT, round_trip_cost=round_trip_cost, actions=actions,
        allow_fractional=False, max_positions=MAX_POSITIONS, evaluation_start=common_start)
    metrics = performance_metrics(portfolio.equity, benchmark=benchmark)
    bench = performance_metrics(benchmark)
    buys = portfolio.trades[portfolio.trades.side.eq('BUY')]
    sells = portfolio.trades[portfolio.trades.side.eq('SELL')]
    stability = _annual_stability(portfolio.equity)
    concentration = _concentration(portfolio.trades)
    mdd = metrics.get('max_drawdown')
    row = {'strategy': strategy, 'holding_sessions': int(horizon),
           'risk_fraction_label': rf_label, 'risk_fraction': rf,
           'candidate_entries': int(len(group)),
           'accepted_entries': int(len(buys)), 'rejected_entries': int(len(portfolio.rejected)),
           'round_trips': int(len(sells)),
           'right_censored': int(len(buys) - len(sells)),
           'start_date': metrics.get('start_date'), 'end_date': metrics.get('end_date'),
           'final_equity': metrics.get('final_equity'),
           'CAGR': metrics.get('CAGR'), 'max_drawdown': mdd,
           'mdd_magnitude': (-mdd if mdd is not None else None),
           'Calmar': metrics.get('Calmar'), 'Sharpe': metrics.get('Sharpe'),
           'annualized_volatility': metrics.get('annualized_volatility'),
           'average_gross_exposure': metrics.get('average_gross_exposure'),
           'turnover': metrics.get('turnover'),
           'benchmark_CAGR': metrics.get('benchmark_CAGR'),
           'excess_CAGR': metrics.get('excess_CAGR'),
           'benchmark_max_drawdown': bench.get('max_drawdown'),
           'benchmark_Calmar': bench.get('Calmar'),
           'worst_year_return': stability['worst_year_return'],
           'down_year_ratio': stability['down_year_ratio'],
           'top1_winner_share': concentration['top1_winner_share'],
           'top5_winner_share': concentration['top5_winner_share'],
           'win_rate': concentration['win_rate']}
    return row, portfolio.equity, portfolio.trades, portfolio.rejected


def _pareto_frontier(summary: pd.DataFrame) -> pd.DataFrame:
    """在 (CAGR, mdd_magnitude) 上取非支配集（收益更高且回撤更低即支配）。"""
    qual = summary[summary.mdd_magnitude.le(MDD_HARD_BOUND) & summary.CAGR.notna()].copy()
    if qual.empty:
        return qual.iloc[0:0]
    dominated = np.zeros(len(qual), dtype=bool)
    cagr = qual.CAGR.to_numpy(float)
    mdd = qual.mdd_magnitude.to_numpy(float)
    for i in range(len(qual)):
        for j in range(len(qual)):
            if i != j and cagr[j] >= cagr[i] and mdd[j] <= mdd[i] and (
                    cagr[j] > cagr[i] or mdd[j] < mdd[i]):
                dominated[i] = True
                break
    return qual.loc[~dominated].reset_index(drop=True)


def _grid() -> list[tuple[str, int, str, float, bool]]:
    """返回全部 (strategy, horizon, rf_label, rf, is_reference) 配置。"""
    configs = []
    for strategy in STRATEGIES:
        horizons = list(GRID_HORIZONS)
        if strategy == 'P1':
            horizons = [REFERENCE_HORIZON] + horizons
        for horizon in horizons:
            risk_grid = [('1.0pct', 0.01)] if horizon == REFERENCE_HORIZON else RISK_FRACTIONS
            for rf_label, rf in risk_grid:
                configs.append((strategy, horizon, rf_label, rf, horizon == REFERENCE_HORIZON))
    return configs


def _select_frozen(summary: pd.DataFrame) -> pd.Series | None:
    """MDD≤20% 硬界内，优先 MDD≤18%（缓冲上界 guardrail，不惩罚更低的 MDD），再取 CAGR 最高。

    15–18% 是缓冲「目标」而非硬区间：MDD 低于 15% 同样可取；只有贴近 20% 边界
    （>18%）才降权，避免「把历史回撤调到 19.99%」。参照行不参与冻结。
    """
    qual = summary[summary.mdd_magnitude.le(MDD_HARD_BOUND)
                   & summary.get('is_reference', False).eq(False)].copy()
    if qual.empty:
        return None
    qual['_under_buffer'] = qual.mdd_magnitude.le(MDD_BUFFER[1])
    qual = qual.sort_values(['_under_buffer', 'CAGR'], ascending=[False, False])
    return qual.iloc[0].drop(labels=['_under_buffer'])


def run(output_dir: Path, *, etf_path=ETF_RAW, actions_path=ACTIONS, quality_path=QUALITY,
        panels=PANELS, stress: bool = True) -> dict:
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f'RUN_ALREADY_EXISTS:{output_dir}')
    prices, panel_paths = load_panels(panels=panels)
    quality = pd.read_csv(quality_path)
    actions = pd.read_csv(actions_path)
    actions['security_id'] = actions.security_id.astype(str)
    qqq_prices = _load_qqq_prices(etf_path)
    qqq_dividends = _load_qqq_dividends(QQQ_DIVIDENDS)
    audits, blocked = _build_shared_audits(prices, actions)
    print(f'[RR] action gate: verified={len(audits)} '
          f'unexplained={sum(len(a["unexplained_dates"]) for a in audits.values())} '
          f'suspects={[s for s, a in audits.items() if a["verdict"] != "ok"]}', flush=True)

    p1_matrix, p1_funnel = _build_p1_matrix(prices, quality, actions, blocked)
    print(f'[RR] P1 prepared={len(p1_matrix)}', flush=True)
    p2_matrices, p2_funnels = _build_p2_matrices(prices, quality, actions, blocked, etf_path)
    for s, m in p2_matrices.items():
        print(f'[RR] {s} prepared={len(m)}', flush=True)
    matrices = {'P1': p1_matrix, **p2_matrices}
    funnels = {'P1': p1_funnel, **p2_funnels}
    for s, m in matrices.items():
        m['portfolio_accepted'] = True  # 统一走运行时五仓拒绝，避免 P1/P2 口径不一

    common_start = min(m.entry_session.min() for m in matrices.values())
    common_end = max(m.exit_session.max() for m in matrices.values())
    window_prices = prices[(prices.session >= common_start) & (prices.session <= common_end)]
    benchmark = build_qqq_benchmark(qqq_prices, qqq_dividends, common_start, common_end)
    print(f'[RR] common window {common_start.date()}..{common_end.date()} '
          f'P1_rows={len(p1_matrix)} B2_rows={len(p2_matrices["B2"])} '
          f'B3_rows={len(p2_matrices["B3"])}', flush=True)

    rows, equity_all, trades_all, rejected_all = [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    for strategy, horizon, rf_label, rf, is_reference in _grid():
        matrix = matrices[strategy]
        group = matrix[matrix.holding_sessions.eq(horizon)].copy()
        if group.empty:
            continue
        row, equity, trades, rejected = _run_config(
            strategy, horizon, rf_label, rf, group, window_prices, actions, benchmark,
            common_start, common_end)
        row['is_reference'] = bool(is_reference)
        rows.append(row)
        equity_all = pd.concat([equity_all, equity.assign(
            strategy=strategy, holding_sessions=horizon, risk_fraction_label=rf_label,
            risk_fraction=rf, is_reference=is_reference)], ignore_index=True)
        trades_all = pd.concat([trades_all, trades.assign(
            strategy=strategy, holding_sessions=horizon, risk_fraction_label=rf_label,
            risk_fraction=rf)], ignore_index=True)
        rejected_all = pd.concat([rejected_all, rejected.assign(
            strategy=strategy, holding_sessions=horizon, risk_fraction_label=rf_label,
            risk_fraction=rf)], ignore_index=True)

    summary = pd.DataFrame(rows)
    pareto = _pareto_frontier(summary)
    frozen = _select_frozen(summary)
    frozen_key = None
    stress_rows = []
    if frozen is not None and stress:
        frozen_key = {'strategy': frozen['strategy'], 'holding_sessions': int(frozen['holding_sessions']),
                      'risk_fraction': float(frozen['risk_fraction'])}
        group = matrices[frozen['strategy']][
            matrices[frozen['strategy']].holding_sessions.eq(frozen['holding_sessions'])].copy()
        for cost in STRESS_ROUND_TRIP:
            row, _, _, _ = _run_config(
                frozen['strategy'], int(frozen['holding_sessions']), f'stress_{cost}',
                float(frozen['risk_fraction']), group, window_prices, actions, benchmark,
                common_start, common_end, round_trip_cost=cost)
            stress_rows.append({'round_trip_cost': cost, 'CAGR': row['CAGR'],
                                'max_drawdown': row['max_drawdown'],
                                'mdd_magnitude': row['mdd_magnitude'],
                                'final_equity': row['final_equity']})
    stress_summary = pd.DataFrame(stress_rows)

    if frozen is not None:
        fkey = f'{frozen["strategy"]}_{int(frozen["holding_sessions"])}_{frozen["risk_fraction_label"]}'
        frozen_state = {'status': 'frozen', 'key': fkey, **{k: (None if pd.isna(v) else v)
                        for k, v in frozen.to_dict().items()}}
        stress_mdd = float(stress_summary.mdd_magnitude.max()) if len(stress_summary) else None
        if stress_mdd is not None and stress_mdd > MDD_HARD_BOUND:
            frozen_state['stress_warning'] = (f'MDD at stress cost exceeds {MDD_HARD_BOUND:.0%}: '
                                              f'{stress_mdd:.2%}')
    else:
        frozen_state = {'status': 'no_qualified_baseline',
                        'note': f'无配置 MDD ≤ {MDD_HARD_BOUND:.0%}；继续调整风险设计，不强行冻结'}

    manifest = {
        'status': 'risk_rule_experiment',
        'run_id': output_dir.name,
        'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'git_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()),
        'strategies': list(STRATEGIES), 'risk_fractions': [rf for _, rf in RISK_FRACTIONS],
        'grid_horizons': list(GRID_HORIZONS), 'reference_horizon': REFERENCE_HORIZON,
        'round_trip_cost': COST, 'initial_cash_usd': INITIAL_CASH, 'max_weight': MAX_WEIGHT,
        'max_positions': MAX_POSITIONS,
        'stop_rule': 'max(8% of entry, 2.5 x signal ATR14 raw)',
        'sizing_rule': 'risk_fraction = single-position account initial risk (0.5/0.75/1.0%)',
        'common_window': {'start': str(common_start.date()), 'end': str(common_end.date())},
        'universe': list(TECH), 'benchmark_basis': 'QQQ_raw_open_entry_official_dividends_cash_per_leg_0.1pct',
        'mdd_hard_bound': MDD_HARD_BOUND, 'mdd_buffer': MDD_BUFFER,
        'funnel': funnels,
        'frozen': frozen_state,
        'pareto_frontier': pareto[['strategy', 'holding_sessions', 'risk_fraction_label',
                                   'CAGR', 'max_drawdown', 'mdd_magnitude']].to_dict('records'),
        'stress': stress_summary.to_dict('records'),
        'action_coverage': {
            'verified_securities': len(audits),
            'total_unexplained_dates': sum(len(a['unexplained_dates']) for a in audits.values()),
            'suspect_by_security': {s: {'verdict': a['verdict'],
                                        'unexplained_dates': a['unexplained_dates']}
                                    for s, a in audits.items() if a['verdict'] != 'ok'}},
        'notes': ['探索性；历史存续样本，非盲测', '统一口径：同 13 只科技股、union 窗口（空闲持现金）、'
                  '0.2% round-trip、$100k、入场日硬止损 + 五仓运行时拒绝',
                  '单笔风险 X% 是账户初始风险（risk_sized_shares），非仓位比例',
                  'P1@20 仅为低回撤参照行，不参与冻结',
                  '盈利集中度基于已实现交易（不含未平仓与分红）'],
        'input_sha256': {str(p): _hash(p) for p in
                         [quality_path, actions_path, etf_path, QQQ_DIVIDENDS, P1_ENTRIES, *panel_paths]},
    }
    manifest['code_sha256'] = {str(p.relative_to(ROOT)): _hash(p)
                               for p in sorted((ROOT / 'scripts/medium_term').glob('*.py'))}
    output_dir.mkdir(parents=True)
    summary.to_csv(output_dir / 'summary.csv', index=False)
    equity_all.to_csv(output_dir / 'equity.csv.gz', index=False, compression='gzip')
    trades_all.to_csv(output_dir / 'trades.csv.gz', index=False, compression='gzip')
    rejected_all.to_csv(output_dir / 'rejected.csv', index=False)
    pareto.to_csv(output_dir / 'pareto.csv', index=False)
    if len(stress_summary):
        stress_summary.to_csv(output_dir / 'stress.csv', index=False)
    (output_dir / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + '\n')
    lines = ['# P1 同风险规则实验 · 统一口径规则基线', '',
             f"共同评价窗 {common_start.date()} → {common_end.date()}；"
             f"股票池 {len(TECH)} 只科技股；成本 {COST:.1%} round-trip；$100k；五仓。", '',
             '| 策略 | 持有日 | 单笔风险 | CAGR | 最大回撤 | Calmar | 仓位利用率 | 最差年 | 下跌年占比 | top1占比 |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in summary.itertuples(index=False):
        label = f'{row.risk_fraction_label}' + (' (参照)' if row.is_reference else '')
        lines.append(f'| {row.strategy} | {row.holding_sessions} | {label} | {row.CAGR:.2%} | '
                     f'{row.max_drawdown:.2%} | {row.Calmar:.2f} | '
                     f'{row.average_gross_exposure:.1%} | {row.worst_year_return:.2%} | '
                     f'{row.down_year_ratio:.1%} | {row.top1_winner_share:.2%} |')
    lines.extend(['', f'冻结结论：{json.dumps(frozen_state, ensure_ascii=False)}', '',
                  '统一口径：同 13 只科技股、union 窗口、0.2% round-trip、$100k、入场日硬止损 + 五仓。',
                  '单笔风险是账户初始风险（非仓位比例）。样本为历史存续股，非盲测；冻结仅代表获得前瞻验证资格。'])
    (output_dir / 'report.md').write_text('\n'.join(lines) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--no-stress', action='store_true')
    args = parser.parse_args()
    run(args.output_dir, stress=not args.no_stress)
    print('done')
