#!/usr/bin/env python3
"""P2：B2（动量选股）与 B3（周线门+日线择时）的五仓账户比较。

13 只验证科技股；月度截面动量（6m 与 12-1 等权百分位）取前 5，仅当 QQQ 收盘高于其
200 日均线时开新仓。B2 在 T+1 原始开盘入场；B3 从同一批候选待周线门开启后按日线
突破/回踩入场、最多等 20 个交易日。两者共用同一止损、退出、成本与资金约束，
故 B3−B2 即择时增量。探索性，非未见样本验证。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .action_coverage import audit_action_coverage, blocked_sessions
from .entry_risk import medium_initial_stop
from .exit_matrix import apply_five_position_limit, simulate_fixed_horizon_exits
from .performance import performance_metrics
from .p1_account_check import (COST, ETF_RAW, INITIAL_CASH, MAX_POSITIONS, MAX_WEIGHT,
                               QQQ_DIVIDENDS, _load_adjusted, _load_qqq_dividends,
                               _load_qqq_prices)
from .portfolio_engine import simulate_multi_asset_portfolio
from .qqq_benchmark import build_qqq_benchmark
from .stock_cross_section import generate_monthly_candidates
from .timed_entries import build_timed_entries

HORIZONS = (20, 40, 60, 90, 120)
TOP_N = 5
RISK_FRACTION = 1.0  # 满仓：让 20% 市值上限生效，等价五仓等权
STRATEGIES = ('B2', 'B3')
TECH = ('SEC-US-AAPL', 'SEC-US-AMD', 'SEC-US-AMZN', 'SEC-US-ARM', 'SEC-US-CRWV',
        'SEC-US-GOOGL', 'SEC-US-INTC', 'SEC-US-LITE', 'SEC-US-META', 'SEC-US-MSFT',
        'SEC-US-MU', 'SEC-US-NFLX', 'SEC-US-NVDA')
ROOT = Path(__file__).resolve().parents[2]
PANELS = ROOT / 'data/survivor_sample_audit/asof_panels'
QUALITY = ROOT / 'data/survivor_sample_audit/research_quality_intervals-v3.csv'
ACTIONS = ROOT / 'data/corporate_actions_runs/futu-survivor39-20260912/corporate_actions.csv'


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _cache_signature(quality_path, actions_path, etf_path, panel_paths, top_n, strategies) -> str:
    """缓存版本签名：输入文件 + 参数 + 代码；任一变化即缓存失效，避免复用旧产物。"""
    digest = hashlib.sha256()
    digest.update(str(top_n).encode())
    digest.update(','.join(strategies).encode())
    for p in [quality_path, actions_path, etf_path, QQQ_DIVIDENDS, *panel_paths]:
        digest.update(_hash(Path(p)).encode())
    for p in sorted((ROOT / 'scripts/medium_term').glob('*.py')):
        digest.update(_hash(p).encode())
    return digest.hexdigest()


def load_panels(tech=TECH, panels=PANELS) -> tuple[pd.DataFrame, list]:
    frames, paths = [], []
    for sid in tech:
        path = Path(panels) / f'{sid.replace("SEC-US-", "US_")}.csv.gz'
        if not path.exists():
            raise ValueError(f'ASOF_PANEL_MISSING:{sid}')
        d = pd.read_csv(path)
        d['security_id'] = sid
        d['session'] = pd.to_datetime(d.session).dt.tz_localize(None).dt.normalize()
        frames.append(d)
        paths.append(path)
    prices = pd.concat(frames, ignore_index=True)
    if prices.duplicated(['security_id', 'session']).any():
        raise ValueError('ASOF_PANEL_DUPLICATE')
    return prices.sort_values(['security_id', 'session']).reset_index(drop=True), paths


def market_frame(etf_path=ETF_RAW) -> pd.DataFrame:
    d = pd.read_csv(etf_path, usecols=['security_id', 'session', 'close'])
    d = d[d.security_id.astype(str).eq('SEC-US-QQQ')][['session', 'close']].copy()
    d['session'] = pd.to_datetime(d.session).dt.tz_localize(None).dt.normalize()
    d = d.sort_values('session')
    d['asof_close'] = pd.to_numeric(d.close, errors='coerce')
    d['asof_ma200'] = d.asof_close.rolling(200, min_periods=200).mean()
    return d[['session', 'asof_close', 'asof_ma200']].reset_index(drop=True)


def trading_calendar(etf_path=ETF_RAW) -> pd.DatetimeIndex:
    d = pd.read_csv(etf_path, usecols=['security_id', 'session'])
    d = d[d.security_id.astype(str).eq('SEC-US-QQQ')]
    return pd.DatetimeIndex(pd.to_datetime(d.session).dt.normalize().unique()).sort_values()


def build_entries(spec: pd.DataFrame, prices: pd.DataFrame, quality: pd.DataFrame,
                  blocked: dict | None = None, *,
                  atr_session_col: str = 'decision_session') -> tuple[pd.DataFrame, dict]:
    intervals = {str(r.security_id): (pd.Timestamp(r.from_session).normalize(),
                                      pd.Timestamp(r.to_session).normalize())
                 for r in quality.loc[quality.quality_status.eq('verified')].itertuples()}
    key = prices.set_index(['security_id', 'session'])
    rows, excluded = [], {}

    def drop(reason):
        excluded[reason] = excluded.get(reason, 0) + 1

    sel = spec.sort_values(['decision_session', 'rank', 'security_id'])
    for row in sel.itertuples(index=False):
        sid = str(row.security_id)
        if sid not in intervals:
            drop('SECURITY_NOT_VERIFIED'); continue
        start, end = intervals[sid]
        exec_day = pd.Timestamp(row.execution_session).normalize()
        decision = pd.Timestamp(row.decision_session).normalize()
        if not (start <= decision <= end):
            drop('DECISION_OUTSIDE_QUALITY'); continue
        stock = prices[prices.security_id.eq(sid)]
        loc = stock.index[stock.session.eq(exec_day)]
        if len(loc) != 1:
            drop('EXECUTION_SESSION_MISSING'); continue
        i = stock.index.get_loc(loc[0])
        if i + max(HORIZONS) - 1 >= len(stock):
            drop('INSUFFICIENT_FORWARD_BARS'); continue
        planned_exit = stock.session.iloc[i + max(HORIZONS) - 1]
        if planned_exit > end:
            drop('WINDOW_BEYOND_QUALITY_END'); continue
        blocked_days = blocked.get(sid, ()) if blocked else ()
        if any(exec_day <= day <= planned_exit for day in blocked_days):
            drop('ACTION_COVERAGE_UNEXPLAINED'); continue
        atr_day = pd.Timestamp(getattr(row, atr_session_col)).normalize()
        entry_price = float(stock.raw_open.iloc[i])
        try:
            atr_raw = float(key.loc[(sid, atr_day), 'asof_atr']) * float(
                key.loc[(sid, atr_day), 'scale_to_next'])
            stop = medium_initial_stop(entry_price, atr_raw)
        except KeyError:
            drop('ASOF_ATR_MISSING'); continue
        except ValueError:
            drop('INVALID_INITIAL_STOP'); continue
        rows.append({'entry_id': f'{sid}-{decision.date()}', 'security_id': sid,
                     'entry_session': exec_day, 'entry_price': entry_price,
                     'initial_stop': stop, 'decision_session': decision})
    prepared = pd.DataFrame(rows)
    if not prepared.empty:
        prepared['rank'] = range(len(prepared))
    total_dropped = int(sum(excluded.values()))
    if int(len(spec)) != int(len(prepared)) + total_dropped:
        raise ValueError('FUNNEL_DENOMINATOR_MISMATCH')
    return prepared, {'candidates': int(len(spec)), 'prepared_entries': int(len(prepared)),
                      'excluded': excluded}


def build_matrix(prepared: pd.DataFrame, prices: pd.DataFrame,
                 actions: pd.DataFrame) -> pd.DataFrame:
    index = {sid: g.reset_index(drop=True) for sid, g in prices.groupby('security_id')}
    by_security = {sid: g for sid, g in actions.groupby('security_id')}
    records = []
    for row in prepared.itertuples(index=False):
        sid = str(row.security_id)
        stock = index[sid]
        entry_day = pd.Timestamp(row.entry_session)
        future = stock[stock.session.ge(entry_day)].head(max(HORIZONS)).reset_index(drop=True)
        base = {'entry_id': row.entry_id, 'security_id': sid, 'entry_session': entry_day,
                'entry_price': float(row.entry_price), 'initial_stop': float(row.initial_stop),
                'rank': int(row.rank)}
        outcomes = simulate_fixed_horizon_exits(base, future, HORIZONS, actions=by_security.get(sid))
        for horizon in HORIZONS:
            result = outcomes[horizon]
            if result['gross_pnl_pct'] is None:
                continue
            records.append({**base, 'holding_sessions': horizon,
                            'exit_method': result['exit_method'],
                            'exit_session': result['exit_session'],
                            'exit_price': result['exit_price'],
                            'exit_reason': result['exit_reason'],
                            'exit_phase': result['exit_phase'],
                            'gross_pnl_pct': result['gross_pnl_pct'],
                            'net_pnl_pct': result['gross_pnl_pct'] - COST})
    if not records:
        raise ValueError('P2_MATRIX_EMPTY')
    matrix = pd.DataFrame(records)
    accepted = [apply_five_position_limit(g, max_positions=MAX_POSITIONS)
                for _, g in matrix.groupby('holding_sessions', sort=True)]
    return pd.concat(accepted, ignore_index=True)


def _account(strategy, matrix, prices, actions, qqq_prices, qqq_dividends, start, end):
    window = prices[(prices.session >= start) & (prices.session <= end)][
        ['security_id', 'session', 'raw_open', 'raw_close']]
    rows, equity, trades, rejected = [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    for horizon, group in matrix.groupby('holding_sessions', sort=True):
        portfolio = simulate_multi_asset_portfolio(
            window, group, initial_cash=INITIAL_CASH, risk_fraction=RISK_FRACTION,
            max_weight=MAX_WEIGHT, round_trip_cost=COST, actions=actions,
            allow_fractional=False, max_positions=MAX_POSITIONS,
            evaluation_start=start)
        benchmark = build_qqq_benchmark(qqq_prices, qqq_dividends, start, end)
        metrics = performance_metrics(portfolio.equity, benchmark=benchmark)
        bench = performance_metrics(benchmark)
        rows.append({'strategy': strategy, 'holding_sessions': int(horizon),
                     'accepted_entries': int(group.portfolio_accepted.sum()),
                     'round_trips': int((portfolio.trades.side == 'BUY').sum()),
                     'mean_net_pnl_pct': float(group.loc[group.portfolio_accepted, 'net_pnl_pct'].mean()),
                     **{k: metrics.get(k) for k in
                        ('CAGR', 'max_drawdown', 'Calmar', 'Sharpe', 'average_gross_exposure',
                         'turnover', 'benchmark_CAGR', 'excess_CAGR',
                         'rolling_12m_win_rate_vs_benchmark')},
                     'benchmark_max_drawdown': bench.get('max_drawdown'),
                     'benchmark_Sharpe': bench.get('Sharpe'),
                     'benchmark_Calmar': bench.get('Calmar')})
        equity = pd.concat([equity, portfolio.equity.assign(
            strategy=strategy, holding_sessions=int(horizon))], ignore_index=True)
        trades = pd.concat([trades, portfolio.trades.assign(
            strategy=strategy, holding_sessions=int(horizon))], ignore_index=True)
        rejected = pd.concat([rejected, portfolio.rejected.assign(
            strategy=strategy, holding_sessions=int(horizon))], ignore_index=True)
    return pd.DataFrame(rows), equity, trades, rejected


def run(output_dir: Path, *, quality_path=QUALITY, actions_path=ACTIONS,
        etf_path=ETF_RAW, panels=PANELS, strategies=STRATEGIES,
        prep_cache=None, matrix_cache=None, build_only=False) -> dict:
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f'RUN_ALREADY_EXISTS:{output_dir}')
    strategies = tuple(strategies)
    if not strategies or set(strategies) - set(STRATEGIES):
        raise ValueError('UNKNOWN_STRATEGIES')
    quality = pd.read_csv(quality_path)
    prices, panel_paths = load_panels(panels=panels)
    actions = pd.read_csv(actions_path)
    actions['security_id'] = actions.security_id.astype(str)
    qqq_prices = _load_qqq_prices(etf_path)
    qqq_dividends = _load_qqq_dividends(QQQ_DIVIDENDS)
    sig = _cache_signature(quality_path, actions_path, etf_path, panel_paths, TOP_N, strategies)
    # 前置（行动门、月度候选、B3 择时）与策略无关；可缓存后分策略运行以控制单次耗时。
    cache = Path(prep_cache) if prep_cache else None
    loaded = False
    if cache is not None and cache.exists():
        blob = pd.read_pickle(cache)
        if blob.get('_signature') == sig:
            audits, blocked, candidates, selected, timed = (
                blob['audits'], blob['blocked'], blob['candidates'],
                blob['selected'], blob['timed'])
            loaded = True
            print(f'[P2] prep cache loaded:{cache}', flush=True)
        else:
            print(f'[P2] prep cache stale，忽略并重算:{cache}', flush=True)
    if not loaded:
        audits, blocked = {}, {}
        for sid in TECH:
            raw_window = (prices.loc[prices.security_id.eq(sid), ['session', 'raw_close']]
                          .rename(columns={'raw_close': 'close'}))
            audit = audit_action_coverage(raw_window, _load_adjusted(sid.replace('SEC-US-', 'US_')),
                                          actions, sid)
            audits[sid] = audit
            blocked[sid] = blocked_sessions(audit)
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
        if cache is not None:
            pd.to_pickle({'audits': audits, 'blocked': blocked, 'candidates': candidates,
                          'selected': selected, 'timed': timed, '_signature': sig}, cache)
            print(f'[P2] prep cache written:{cache}', flush=True)
    candidate_funnel = {
        'universe_rows': int(len(candidates)),
        'selected': int(len(selected)),
        'selection_reason_counts': {
            str(k): int(v) for k, v in
            candidates.selection_reason.fillna('').value_counts().items()},
        'timed': {'entered': int(timed.entry_type.isin(['breakout', 'pullback']).sum()),
                  'expired': int((timed.entry_type == 'EXPIRED').sum()),
                  'data_blocked': int((timed.entry_type == 'DATA_BLOCKED').sum()),
                  'no_next_open': int((timed.entry_type == 'NO_NEXT_OPEN').sum())},
    }
    specs = {'B2': (selected, 'decision_session'),
             'B3': (timed[timed.entry_type.ne('EXPIRED') & timed.execution_session.notna()],
                    'signal_session')}
    all_summary, all_equity, all_trades, all_rejected = [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    funnels, matrices = {}, {}
    for strategy in strategies:
        spec, atr_col = specs[strategy]
        cache_file = Path(matrix_cache) / f'{strategy}.pkl' if matrix_cache else None
        loaded = False
        if cache_file is not None and cache_file.exists():
            blob = pd.read_pickle(cache_file)
            if blob.get('_signature') == sig:
                matrix, funnel = blob['matrix'], blob['funnel']
                loaded = True
                print(f'[P2] {strategy}: matrix cache loaded', flush=True)
            else:
                print(f'[P2] {strategy}: matrix cache stale，忽略并重算', flush=True)
        if not loaded:
            prepared, funnel = build_entries(spec, prices, quality, blocked,
                                             atr_session_col=atr_col)
            if prepared.empty:
                raise ValueError(f'NO_PREPARED_ENTRIES:{strategy}')
            matrix = build_matrix(prepared, prices, actions)
            funnel = {**funnel, 'position_limit': {
                str(k): int(v) for k, v in matrix.loc[
                    ~matrix.portfolio_accepted.astype(bool), 'portfolio_reject_reason'
                ].value_counts().items()}}
            if cache_file is not None:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                pd.to_pickle({'matrix': matrix, 'funnel': funnel, '_signature': sig}, cache_file)
            print(f'[P2] {strategy}: prepared={funnel["prepared_entries"]} '
                  f'excluded={funnel["excluded"]}', flush=True)
        matrices[strategy] = matrix
        funnels[strategy] = funnel
    if build_only:
        return {'funnel': funnels, 'candidate_funnel': candidate_funnel}
    # 共同评价窗：B2/B3 同一窗比较，择时增量才可加；晚入场策略在窗首保留现金。
    common_start = min(m.entry_session.min() for m in matrices.values())
    common_end = max(m.exit_session.max() for m in matrices.values())
    for strategy in strategies:
        summary, equity, trades, rejected = _account(strategy, matrices[strategy], prices,
                                                     actions, qqq_prices, qqq_dividends,
                                                     common_start, common_end)
        all_summary.append(summary)
        all_equity = pd.concat([all_equity, equity], ignore_index=True)
        all_trades = pd.concat([all_trades, trades], ignore_index=True)
        all_rejected = pd.concat([all_rejected, rejected], ignore_index=True)
    summary = pd.concat(all_summary, ignore_index=True)
    manifest = {'status': 'exploratory_p2_b2_b3', 'strategies': list(strategies),
                'universe': list(TECH), 'top_n': TOP_N, 'horizons': HORIZONS,
                'round_trip_cost': COST, 'sizing_rule': 'equal_weight_20pct',
                'risk_fraction_used': RISK_FRACTION, 'max_weight': MAX_WEIGHT,
                'max_positions': MAX_POSITIONS, 'market_gate': 'QQQ_raw_close_above_raw_MA200',
                'stop_rule': 'max(8% of entry, 2.5 x signal ATR14 raw)',
                'b3_rules': {'weekly_gate': 'complete-week close>MA20w & MA20w not declining',
                             'wait_sessions': 20, 'breakout': 'close>prior-20d high',
                             'pullback': 'low<=MA20/50*(1+tol) & close>prior-day high',
                             'priority': 'pullback_before_breakout', 'tol': .01},
                'funnel': funnels,
                'candidate_funnel': candidate_funnel,
                'action_coverage': {
                    'verified_securities': len(audits),
                    'total_unexplained_dates': sum(len(a['unexplained_dates']) for a in audits.values()),
                    'suspect_by_security': {s: {'verdict': a['verdict'],
                                                'unexplained_dates': a['unexplained_dates'],
                                                'duplicate_records': a['duplicate_records']}
                                            for s, a in audits.items() if a['verdict'] != 'ok'}},
                'notes': ['探索性；已知历史存续样本，非盲测', 'B1 因 ETF 行动未核验而暂缓',
                          'benchmark: QQQ 同口径（原始价开盘+官方分红进现金+0.1%单边成本）',
                          'benchmark: 分红按除息日记现金（研究近似，非支付日入账）；分数股不模拟、'
                          '期末未平仓、滑点与现金收益未计，全部比较组一致',
                          'B2动量/B3择时用拆股复权特征；B2/B3 共同评价窗，晚入场策略窗首保留现金'],
                'input_sha256': {str(p): _hash(p) for p in
                                 [quality_path, actions_path, etf_path, QQQ_DIVIDENDS, *panel_paths]}}
    output_dir.mkdir(parents=True)
    all_equity.to_csv(output_dir / 'equity.csv.gz', index=False, compression='gzip')
    all_trades.to_csv(output_dir / 'trades.csv.gz', index=False, compression='gzip')
    all_rejected.to_csv(output_dir / 'rejected.csv', index=False)
    for strategy, matrix in matrices.items():
        matrix.to_csv(output_dir / f'matrix_{strategy}.csv.gz', index=False, compression='gzip')
    summary.to_csv(output_dir / 'summary.csv', index=False)
    (output_dir / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + '\n')
    lines = ['# P2 · B2 选股 / B3 择时 五仓账户', '',
             f'13 只验证科技股；月度动量前 {TOP_N}、QQQ>MA200 才开新仓；满仓 5×20% 等权、成本 0.2%。', '',
             '| 策略 | 持有日 | 成交 | 单笔净收益 | CAGR | 最大回撤 | Calmar | Sharpe | 仓位利用率 | 相对QQQ超额 |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in summary.itertuples(index=False):
        lines.append(f'| {row.strategy} | {row.holding_sessions} | {row.round_trips} | '
                     f'{row.mean_net_pnl_pct:.2%} | {row.CAGR:.2%} | {row.max_drawdown:.2%} | '
                     f'{row.Calmar:.2f} | {row.Sharpe:.2f} | {row.average_gross_exposure:.1%} | '
                     f'{row.excess_CAGR:.2%} |')
    lines.extend(['', 'B2/B3 共用同一止损、退出、成本与资金约束；B3−B2 即择时增量。'
                  '原始价成交、入场日生效硬止损、整股。',
                  'QQQ 市场门用原始价 MA200 近似（已披露）。样本为已知历史存续股，非未见样本验证。'])
    (output_dir / 'report.md').write_text('\n'.join(lines) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--strategies', default=','.join(STRATEGIES))
    parser.add_argument('--prep-cache', default=None)
    parser.add_argument('--matrix-cache', default=None)
    parser.add_argument('--build-only', action='store_true')
    args = parser.parse_args()
    chosen = tuple(s.strip() for s in args.strategies.split(',') if s.strip())
    result = run(args.output_dir, strategies=chosen, prep_cache=args.prep_cache,
                 matrix_cache=args.matrix_cache, build_only=args.build_only)
    print(json.dumps(result if args.build_only else result['funnel'],
                     ensure_ascii=False))
