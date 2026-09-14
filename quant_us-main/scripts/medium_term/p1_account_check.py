#!/usr/bin/env python3
"""P1：原始价 + 硬止损 + 五仓资金约束的账户回测（探索性，非未见样本验证）。

同一批 A 组入场，五个固定持有期（20/40/60/90/120 交易日）共用同一初始硬止损、
成本与资金约束；输出各期限账户 CAGR、最大回撤、仓位利用率与相对同口径 QQQ 超额。
样本筛选只依据入场时预定的 120 日完整观察窗是否落在质量区间内，与止损是否提前
触发无关，保证五组可比且不因结果决定样本去留。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

from .entry_risk import medium_initial_stop
from .action_coverage import audit_action_coverage, blocked_sessions
from .exit_matrix import (MAX_HOLD, apply_five_position_limit,
                          simulate_fixed_horizon_exits)
from .performance import performance_metrics
from .portfolio_engine import simulate_multi_asset_portfolio

HORIZONS = (20, 40, 60, 90, 120)
COST = .002
INITIAL_CASH = 100_000.
RISK_FRACTION = .01
MAX_WEIGHT = .20
MAX_POSITIONS = 5
# 资金规则：risk_1pct 为手册基准（1% 风险，因 8% 止损实际压到 ~12.5%/仓）；
# equal_weight_20pct 把风险约束放开，使 20% 市值上限生效，即五仓满仓等权。
SIZING = {'risk_1pct': .01, 'equal_weight_20pct': 1.0}
QQQ_ID = 'SEC-US-QQQ'
# 15 只放行存续股里，BAC、HD 明确非科技；其余 13 只作为科技子集（AMZN/NFLX
# 边界存疑，此处按手册“说明原 15 只含 BAC、HD”的提示做显式假设并写入报告）。
NON_TECH = {'SEC-US-BAC', 'SEC-US-HD'}

ROOT = Path(__file__).resolve().parents[2]
ENTRIES = ROOT / 'data/m2_raw_audit/M2-RAW-AUDIT-20260912-003/entries_abc.csv.gz'
QUALITY = ROOT / 'data/survivor_sample_audit/research_quality_intervals-v3.csv'
RAW_DAILY = ROOT / 'data/m2_raw_audit/M2-RAW-AUDIT-20260912-002/raw_daily_verified_v3.csv.gz'
ACTIONS = ROOT / 'data/corporate_actions_runs/futu-survivor39-20260912/corporate_actions.csv'
ETF_RAW = ROOT / 'data/medium_term/US-MT-MOM-BASELINE-001/prepared/etf_raw_daily.csv.gz'
QFQ_ROOT = ROOT / 'data/market_history/raw/day/qfq'


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _load_bars(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path, usecols=['security_id', 'date', 'open', 'high', 'low', 'close'])
    d = d.rename(columns={'date': 'session', 'open': 'raw_open', 'high': 'raw_high',
                          'low': 'raw_low', 'close': 'raw_close'})
    d['security_id'] = d.security_id.astype(str)
    d['session'] = pd.to_datetime(d.session).dt.tz_localize(None).dt.normalize()
    if d.duplicated(['security_id', 'session']).any():
        raise ValueError('RAW_BAR_DUPLICATE')
    for column in ('raw_open', 'raw_high', 'raw_low', 'raw_close'):
        d[column] = pd.to_numeric(d[column], errors='coerce')
        if (~np.isfinite(d[column]) | (d[column] <= 0)).any():
            raise ValueError(f'RAW_BAR_INVALID:{column}')
    return d.sort_values(['security_id', 'session']).reset_index(drop=True)


def _load_actions(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path, usecols=['security_id', 'ex_date', 'action_type', 'ratio', 'cash_amount'])
    d['security_id'] = d.security_id.astype(str)
    d['ex_date'] = pd.to_datetime(d.ex_date).dt.tz_localize(None).dt.normalize()
    unknown = set(d.action_type.astype(str).str.lower()) - {'split', 'reverse_split', 'cash_dividend'}
    if unknown:
        raise ValueError(f'ACTION_TYPE_UNSUPPORTED:{",".join(sorted(unknown))}')
    return d


def _load_adjusted(code: str, root: Path = None) -> pd.DataFrame:
    root = QFQ_ROOT if root is None else root
    paths = sorted(Path(root).glob(f'year=*/{code.replace(".", "_")}.csv.gz'))
    if not paths:
        raise ValueError(f'QFQ_PRICE_MISSING:{code}')
    d = pd.concat([pd.read_csv(p, usecols=['time_key', 'close']) for p in paths],
                  ignore_index=True)
    d = d.rename(columns={'time_key': 'session'})
    return d[['session', 'close']]


def prepare_entries(entries: pd.DataFrame, quality: pd.DataFrame,
                    bars: pd.DataFrame,
                    blocked_dates: dict | None = None,
                    *, common_window: bool = True) -> tuple[pd.DataFrame, dict]:
    """按“预定 120 日完整窗是否落在质量区间内”筛选并生成止损；产出排除漏斗。

    `blocked_dates` 为按证券的行动缺口日；落在入场→退出窗内则排除该入场。
    `common_window=True`（默认）只保留 120 日完整窗落在质量区间内的入场，使五个
    期限严格配对；`False` 则保留全部合格入场，各期限按自身可用性分别取用。
    """
    intervals = {str(r.security_id): (pd.Timestamp(r.from_session).normalize(),
                                      pd.Timestamp(r.to_session).normalize())
                 for r in quality.loc[quality.quality_status.eq('verified')].itertuples()}
    source = entries.loc[entries.experiment.eq('A')].copy()
    source['entry_day'] = pd.to_datetime(source.entry_time, utc=True).dt.tz_convert(
        'America/New_York').dt.tz_localize(None).dt.normalize()
    source = source.sort_values(['entry_day', 'security_id', 'entry_id'])
    index = {sid: group.reset_index(drop=True) for sid, group in bars.groupby('security_id')}
    rows, excluded = [], {}

    def drop(reason):
        excluded[reason] = excluded.get(reason, 0) + 1

    for entry in source.itertuples(index=False):
        sid = str(entry.security_id)
        if sid not in intervals:
            drop('SECURITY_NOT_VERIFIED'); continue
        start, end = intervals[sid]
        if entry.entry_day < start:
            drop('ENTRY_BEFORE_QUALITY_START'); continue
        if entry.entry_day > end:
            drop('ENTRY_AFTER_QUALITY_END'); continue
        stock = index.get(sid)
        if stock is None:
            drop('RAW_BARS_MISSING'); continue
        loc = stock.index[stock.session.eq(entry.entry_day)]
        if len(loc) != 1:
            drop('ENTRY_SESSION_MISSING'); continue
        i = int(loc[0])
        # 预定的 120 日完整观察窗（入场日为第 1 日）必须全部落在质量区间内。
        if common_window:
            if i + max(HORIZONS) - 1 >= len(stock):
                drop('INSUFFICIENT_FORWARD_BARS'); continue
            planned_exit = stock.session.iloc[i + max(HORIZONS) - 1]
            if planned_exit > end:
                drop('WINDOW_BEYOND_QUALITY_END'); continue
        else:
            if i >= len(stock):  # 连入场日的 bar 都没有
                drop('ENTRY_BAR_MISSING'); continue
            planned_exit = min(end, stock.session.iloc[min(i + max(HORIZONS) - 1, len(stock) - 1)])
        blocked = blocked_dates.get(sid, ()) if blocked_dates else ()
        if any(entry.entry_day <= day <= planned_exit for day in blocked):
            drop('ACTION_COVERAGE_UNEXPLAINED'); continue
        entry_price = float(stock.raw_open.iloc[i])
        atr_raw = float(entry.atr14) * float(entry.scale_to_next)
        try:
            stop = medium_initial_stop(entry_price, atr_raw)
        except ValueError:
            drop('INVALID_INITIAL_STOP'); continue
        rows.append({'entry_id': entry.entry_id, 'security_id': sid,
                     'entry_session': entry.entry_day, 'entry_price': entry_price,
                     'initial_stop': stop, 'atr14_raw': atr_raw,
                     'planned_exit_session': planned_exit})
    prepared = pd.DataFrame(rows)
    if not prepared.empty:
        prepared['rank'] = range(len(prepared))
    funnel = {'candidate_a_entries': int(len(source)), 'prepared_entries': int(len(prepared)),
              'excluded': excluded}
    return prepared, funnel


def build_matrix(prepared: pd.DataFrame, bars: pd.DataFrame,
                 actions: pd.DataFrame, quality_end: dict | None = None) -> pd.DataFrame:
    index = {sid: group.reset_index(drop=True) for sid, group in bars.groupby('security_id')}
    actions_by_security = {sid: group for sid, group in actions.groupby('security_id')}
    records = []
    for row in prepared.itertuples(index=False):
        sid = str(row.security_id)
        stock = index[sid]
        entry_day = pd.Timestamp(row.entry_session)
        future = stock[stock.session.ge(entry_day)].head(MAX_HOLD).reset_index(drop=True)
        security_actions = actions_by_security.get(sid)
        limit = quality_end.get(sid) if quality_end else None
        if limit is not None:
            future = future[future.session.le(limit)]
        base = {'entry_id': row.entry_id, 'security_id': sid,
                'entry_session': entry_day, 'entry_price': float(row.entry_price),
                'initial_stop': float(row.initial_stop), 'rank': int(row.rank)}
        outcomes = simulate_fixed_horizon_exits(base, future, HORIZONS,
                                                actions=security_actions)
        for horizon in HORIZONS:
            result = outcomes[horizon]
            records.append({**base, 'holding_sessions': horizon,
                            'exit_method': result['exit_method'],
                            'exit_session': result['exit_session'],
                            'exit_price': result['exit_price'],
                            'exit_reason': result['exit_reason'],
                            'exit_phase': result['exit_phase'],
                            'data_quality': result['data_quality'],
                            'mark_price': result.get('mark_price'),
                            'gross_pnl_pct': result['gross_pnl_pct'],
                            'net_pnl_pct': (result['gross_pnl_pct'] - COST
                                            if result['gross_pnl_pct'] is not None else None)})
    if not records:
        raise ValueError('P1_MATRIX_EMPTY')
    matrix = pd.DataFrame(records)
    # 仓位由实际现金账户逐日决定；预删候选会让未成交交易占用虚假仓位。
    matrix['portfolio_accepted'] = True
    return matrix


def _window(matrix: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp]:
    return (matrix.entry_session.min(), matrix.exit_session.max())


def _qqq_curve(etf_path: Path, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    d = pd.read_csv(etf_path, usecols=['security_id', 'session', 'close'])
    d = d[d.security_id.astype(str).eq(QQQ_ID)]
    d['session'] = pd.to_datetime(d.session).dt.tz_localize(None).dt.normalize()
    d = d[(d.session >= start) & (d.session <= end)].sort_values('session')
    if d.empty:
        raise ValueError('QQQ_WINDOW_EMPTY')
    first = float(d.close.iloc[0])
    return pd.DataFrame({'session': d.session.values,
                         'equity': (d.close.astype(float) / first).values})


def _run_window(label, matrix, bars, actions, etf_path, window, risk_fraction):
    start, end = window
    subset = matrix[(matrix.entry_session >= start) & (matrix.exit_session <= end)]
    prices = bars[(bars.session >= start) & (bars.session <= end)]
    rows, equity_out, trades_out, rejected_out = [], pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    for horizon, group in subset.groupby('holding_sessions', sort=True):
        group = group.copy()
        group['portfolio_accepted'] = True
        portfolio = simulate_multi_asset_portfolio(
            prices, group, initial_cash=INITIAL_CASH, risk_fraction=risk_fraction,
            max_weight=MAX_WEIGHT, round_trip_cost=COST, actions=actions,
            allow_fractional=False, max_positions=MAX_POSITIONS, evaluation_start=start)
        benchmark = _qqq_curve(etf_path, start, end)
        metrics = performance_metrics(portfolio.equity, benchmark=benchmark)
        bench = performance_metrics(benchmark)
        trades = portfolio.trades
        rejected = portfolio.rejected
        buys = trades[trades.side.eq('BUY')]
        sells = trades[trades.side.eq('SELL')]
        filled = group[group.entry_id.isin(buys.entry_id)]
        rows.append({'scope': label, 'holding_sessions': int(horizon),
                     'candidate_entries': len(group),
                     'accepted_entries': len(buys),
                     'rejected_entries': len(rejected),
                     'round_trips': len(sells),
                     'right_censored': len(buys) - len(sells),
                     'rejected_at_execution': int(len(rejected)),
                     'mean_net_pnl_pct': float(filled.net_pnl_pct.mean()),
                     'start_date': metrics['start_date'], 'end_date': metrics['end_date'],
                     'final_equity': metrics['final_equity'],
                     'max_drawdown_start': metrics['max_drawdown_start'],
                     'max_drawdown_end': metrics['max_drawdown_end'],
                     **{k: metrics.get(k) for k in
                        ('CAGR', 'max_drawdown', 'Calmar', 'annualized_volatility', 'Sharpe',
                         'average_gross_exposure', 'turnover', 'benchmark_CAGR', 'excess_CAGR',
                         'rolling_12m_win_rate_vs_benchmark')},
                     'benchmark_max_drawdown': bench.get('max_drawdown'),
                     'benchmark_Sharpe': bench.get('Sharpe'),
                     'benchmark_Calmar': bench.get('Calmar')})
        equity_out = pd.concat([equity_out, portfolio.equity.assign(
            scope=label, holding_sessions=int(horizon))], ignore_index=True)
        trades_out = pd.concat([trades_out, trades.assign(
            scope=label, holding_sessions=int(horizon))], ignore_index=True)
        rejected_out = pd.concat([rejected_out, rejected.assign(
            scope=label, holding_sessions=int(horizon))], ignore_index=True)
    return pd.DataFrame(rows), equity_out, trades_out, rejected_out


def run(output_dir: Path, *, entries_path=ENTRIES, quality_path=QUALITY,
        raw_path=RAW_DAILY, actions_path=ACTIONS, etf_path=ETF_RAW,
        sizing='risk_1pct', denominator='paired', matrix_cache=None,
        build_only=False) -> dict:
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f'RUN_ALREADY_EXISTS:{output_dir}')
    if sizing not in SIZING:
        raise ValueError(f'UNKNOWN_SIZING:{sizing}')
    if denominator not in ('paired', 'all'):
        raise ValueError(f'UNKNOWN_DENOMINATOR:{denominator}')
    if matrix_cache is not None:
        raise ValueError('UNVERIFIED_MATRIX_CACHE_DISABLED')
    risk_fraction = SIZING[sizing]
    entries = pd.read_csv(entries_path)
    quality = pd.read_csv(quality_path)
    quality_end = {str(r.security_id): pd.Timestamp(r.to_session).normalize()
                   for r in quality.loc[quality.quality_status.eq('verified')].itertuples()}
    bars = _load_bars(raw_path)
    actions = _load_actions(actions_path)
    audits, blocked = {}, {}
    for row in quality.loc[quality.quality_status.eq('verified')].itertuples(index=False):
        sid = str(row.security_id)
        raw_window = (bars.loc[bars.security_id.eq(sid), ['session', 'raw_close']]
                      .rename(columns={'raw_close': 'close'}))
        audit = audit_action_coverage(raw_window, _load_adjusted(str(row.code)), actions, sid)
        audits[sid] = audit
        blocked[sid] = blocked_sessions(audit)
    total_gaps = sum(len(a['unexplained_dates']) for a in audits.values())
    suspects = [sid for sid, a in audits.items() if a['verdict'] != 'ok']
    print(f'[P1] action gate: verified={len(audits)} unexplained_dates={total_gaps} '
          f'suspects={suspects}', flush=True)
    prepared, funnel = prepare_entries(entries, quality, bars, blocked_dates=blocked,
                                       common_window=denominator == 'paired')
    if prepared.empty:
        raise ValueError('NO_PREPARED_ENTRIES')
    print(f'[P1] prepared={len(prepared)} excluded={funnel["excluded"]}', flush=True)
    cache = Path(matrix_cache) if matrix_cache else None
    if cache is not None and cache.exists():
        matrix = pd.read_pickle(cache)
        print(f'[P1] matrix cache loaded:{cache}', flush=True)
    else:
        matrix = build_matrix(prepared, bars, actions,
                              quality_end=None if denominator == 'paired' else quality_end)
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            pd.to_pickle(matrix, cache)
            print(f'[P1] matrix cache written:{cache}', flush=True)
    if build_only:
        return {'funnel': funnel, 'matrix_rows': int(len(matrix))}
    window = _window(matrix)
    if denominator == 'all':
        per_h = matrix.groupby('holding_sessions').entry_id.nunique().to_dict()
        print(f'[P1] all-denominator entries per horizon={per_h}', flush=True)
    print(f'[P1] matrix_rows={len(matrix)} window={window[0].date()}..{window[1].date()}', flush=True)

    summary, equity, trades, rejected = _run_window(
        'all15', matrix, bars, actions, etf_path, window, risk_fraction)
    print('[P1] all15 done', flush=True)
    tech = matrix[~matrix.security_id.isin(NON_TECH)]
    tech_summary, tech_equity, tech_trades, tech_rejected = _run_window(
        'tech13', tech, bars, actions, etf_path, window, risk_fraction)
    summary = pd.concat([summary, tech_summary], ignore_index=True)
    equity = pd.concat([equity, tech_equity], ignore_index=True)
    trades = pd.concat([trades, tech_trades], ignore_index=True)
    rejected = pd.concat([rejected, tech_rejected], ignore_index=True)

    position_rejects = {}
    excluded_final = {f'position_limit_{str(k).lower()}': int(v)
                      for k, v in sorted(position_rejects.items())}
    if len(rejected):
        for reason, count in rejected.reason.value_counts().items():
            excluded_final[f'execution_{str(reason).lower()}'] = int(count)
    manifest = {'status': 'exploratory_raw_hard_stop_five_position',
                'run_id': output_dir.name,
                'git_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'git_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()),
                'p2_gate': 'blocked_pending_same_basis_benchmark_and_review',
                'benchmark_basis': 'QQQ_raw_close_price_only_no_dividends_no_costs',
                'denominator': denominator,
                'horizons': HORIZONS, 'round_trip_cost': COST,
                'initial_cash_usd': INITIAL_CASH, 'sizing_rule': sizing,
                'risk_fraction_used': risk_fraction,
                'max_weight': MAX_WEIGHT, 'max_positions': MAX_POSITIONS,
                'stop_rule': 'max(8% of entry, 2.5 x signal ATR14 raw)',
                'common_window': {'start': str(window[0].date()), 'end': str(window[1].date())},
                'sample': f'verified_survivor15_A_entries_{denominator}',
                'price_basis': 'raw_execution_asof_features',
                'tech_subset_excluded': sorted(NON_TECH),
                'funnel': {**funnel, 'manual_exclusions': excluded_final},
                'action_coverage': {
                    'tolerance': .005, 'verified_securities': len(audits),
                    'total_unexplained_dates': total_gaps,
                    'suspect_by_security': {
                        sid: {'verdict': a['verdict'],
                              'unexplained_dates': a['unexplained_dates'],
                              'invalid_records': a['invalid_records'],
                              'duplicate_records': a['duplicate_records']}
                        for sid, a in audits.items() if a['verdict'] != 'ok'}},
                'notes': ['shares are not resumed across horizons; five horizons share identical '
                          'rejected/right-censored policy', 'not a blind out-of-sample test'],
                'input_sha256': {str(p): _hash(p) for p in
                                 (entries_path, quality_path, raw_path, actions_path, etf_path)}}
    qfq_paths = sorted({p for r in quality.loc[quality.quality_status.eq('verified')].itertuples()
                       for p in QFQ_ROOT.glob(f'year=*/{str(r.code).replace(".", "_")}.csv.gz')})
    manifest['input_sha256'].update({str(p): _hash(p) for p in qfq_paths})
    manifest['code_sha256'] = {str(p.relative_to(ROOT)): _hash(p)
                              for p in sorted((ROOT / 'scripts/medium_term').glob('*.py'))}
    manifest['action_audits'] = audits
    output_dir.mkdir(parents=True)
    equity.to_csv(output_dir / 'equity.csv.gz', index=False, compression='gzip')
    trades.to_csv(output_dir / 'trades.csv.gz', index=False, compression='gzip')
    rejected.to_csv(output_dir / 'rejected.csv', index=False)
    summary.to_csv(output_dir / 'summary.csv', index=False)
    prepared.to_csv(output_dir / 'prepared_entries.csv.gz', index=False, compression='gzip')
    matrix.to_csv(output_dir / 'exit_matrix.csv.gz', index=False, compression='gzip')
    (output_dir / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + '\n')
    lines = ['# P1 原始价 + 硬止损 + 五仓账户回测', '',
             f"共同评价窗：{window[0].date()} → {window[1].date()}；"
             f"入场 {funnel['candidate_a_entries']} → 放行 {funnel['prepared_entries']}；"
             f"排除 {funnel['excluded']}。", '',
             'QQQ 仅为原始收盘价格参考，未核实分红且未扣成本；差值不是同口径超额收益，P2 暂不放行。', '',
             '| 口径 | 持有日 | 已退出笔 | 平均单笔净收益近似 | CAGR | 最大回撤 | Calmar | 仓位利用率 | 与QQQ价格CAGR差 |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in summary.itertuples(index=False):
        lines.append(f"| {row.scope} | {row.holding_sessions} | {row.round_trips} | "
                     f"{row.mean_net_pnl_pct:.2%} | {row.CAGR:.2%} | {row.max_drawdown:.2%} | "
                     f"{row.Calmar:.2f} | {row.average_gross_exposure:.1%} | {row.excess_CAGR:.2%} |")
    lines.extend(['', f'资金规则：{sizing}（risk_fraction={risk_fraction}，'
                     f'单票上限 {MAX_WEIGHT:.0%}，最多 {MAX_POSITIONS} 仓）。原始价成交、'
                  '入场日生效硬止损、整股、完整回合成本 0.2%。',
                  '样本为已知历史存续股，且初始止损按手册口径重算；这是探索性比较，非未见样本验证。'])
    (output_dir / 'report.md').write_text('\n'.join(lines) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--sizing', default='risk_1pct', choices=sorted(SIZING))
    parser.add_argument('--denominator', default='paired', choices=('paired', 'all'))
    parser.add_argument('--matrix-cache', default=None)
    parser.add_argument('--build-only', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir, sizing=args.sizing, denominator=args.denominator,
                         matrix_cache=args.matrix_cache, build_only=args.build_only)['funnel'],
                     ensure_ascii=False))
