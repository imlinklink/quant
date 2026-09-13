#!/usr/bin/env python3
"""同一批 A 组入场的固定持有期配对价格检验；不计算组合 CAGR。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

HORIZONS = (20, 40, 60, 90, 120)
COST = .002
ROOT = Path(__file__).resolve().parents[2]
ENTRIES = ROOT / 'data/m2_raw_audit/M2-RAW-AUDIT-20260912-003/entries_abc.csv.gz'
QUALITY = ROOT / 'data/survivor_sample_audit/research_quality_intervals-v3.csv'
STOCK_QFQ = ROOT / 'data/market_history/raw/day/qfq'
ETF_QFQ = ROOT / 'data/medium_term/US-MT-MOM-BASELINE-001/market_history/day/qfq'


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _load_price(root: Path, code: str) -> tuple[pd.DataFrame, list[Path]]:
    paths = sorted(root.glob(f'year=*/{code.replace(".", "_")}.csv.gz'))
    if not paths:
        raise ValueError(f'QFQ_PRICE_MISSING:{code}')
    d = pd.concat([pd.read_csv(path, usecols=['time_key', 'open', 'close'])
                   for path in paths], ignore_index=True)
    d['session'] = pd.to_datetime(d.time_key).dt.normalize()
    if d.session.duplicated().any():
        raise ValueError(f'QFQ_DUPLICATE_SESSION:{code}')
    d = d[['session', 'open', 'close']].sort_values('session').reset_index(drop=True)
    if d[['open', 'close']].isna().any(axis=None) or (d[['open', 'close']] <= 0).any(axis=None):
        raise ValueError(f'QFQ_PRICE_INVALID:{code}')
    return d, paths


def paired_horizons(entries: pd.DataFrame, quality: pd.DataFrame,
                    prices: dict[str, pd.DataFrame], benchmark: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    verified = set(quality.loc[quality.quality_status.eq('verified'), 'security_id'])
    source = entries.loc[entries.experiment.eq('A')].copy()
    source['entry_day'] = pd.to_datetime(source.entry_time, utc=True).dt.tz_convert(
        'America/New_York').dt.tz_localize(None).dt.normalize()
    source = source[source.security_id.isin(verified)].sort_values(
        ['entry_day', 'security_id', 'entry_id'])
    if source.entry_id.duplicated().any():
        raise ValueError('DUPLICATE_A_ENTRY_ID')
    benchmark = benchmark.set_index('session')
    rows, excluded = [], {}
    for entry in source.itertuples(index=False):
        stock = prices.get(str(entry.security_id))
        reason = ''
        if stock is None:
            reason = 'STOCK_PRICE_MISSING'
        else:
            loc = stock.index[stock.session.eq(entry.entry_day)]
            if len(loc) != 1:
                reason = 'ENTRY_DATE_MISSING'
            elif int(loc[0]) + max(HORIZONS) > len(stock):
                reason = 'RIGHT_CENSORED_120'
            elif entry.entry_day not in benchmark.index:
                reason = 'BENCHMARK_ENTRY_MISSING'
            else:
                i = int(loc[0]); entry_price = float(stock.open.iloc[i])
                if entry_price <= 0:
                    reason = 'ENTRY_PRICE_INVALID'
                else:
                    for horizon in HORIZONS:
                        exit_row = stock.iloc[i + horizon - 1]
                        exit_day = exit_row.session
                        if exit_day not in benchmark.index:
                            reason = 'BENCHMARK_EXIT_MISSING'
                            break
                        rows.append({'entry_id': entry.entry_id, 'security_id': entry.security_id,
                                     'entry_day': entry.entry_day, 'exit_day': exit_day,
                                     'holding_sessions': horizon, 'entry_price': entry_price,
                                     'exit_price': float(exit_row.close),
                                     'net_return': float(exit_row.close) / entry_price - 1 - COST,
                                     'qqq_return': float(benchmark.loc[exit_day, 'close']) /
                                     float(benchmark.loc[entry.entry_day, 'open']) - 1 - COST})
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            rows = [row for row in rows if row['entry_id'] != entry.entry_id]
    paired = pd.DataFrame(rows)
    if paired.empty:
        raise ValueError('NO_FULLY_PAIRED_ENTRIES')
    counts = paired.groupby('holding_sessions').entry_id.nunique()
    if len(counts) != len(HORIZONS) or counts.nunique() != 1:
        raise ValueError('HORIZON_PAIRING_INCOMPLETE')
    return paired, {'candidate_entries': len(source), 'paired_entries': int(counts.iloc[0]),
                    'excluded': excluded}


def summarize(paired: pd.DataFrame) -> pd.DataFrame:
    base = paired[paired.holding_sessions.eq(40)].set_index('entry_id').net_return
    rows = []
    for horizon, group in paired.groupby('holding_sessions', sort=True):
        delta = group.set_index('entry_id').net_return - base
        rows.append({'holding_sessions': int(horizon), 'trades': len(group),
                     'mean_net_return': float(group.net_return.mean()),
                     'median_net_return': float(group.net_return.median()),
                     'win_rate': float(group.net_return.gt(0).mean()),
                     'mean_qqq_return': float(group.qqq_return.mean()),
                     'mean_excess_vs_qqq': float((group.net_return - group.qqq_return).mean()),
                     'paired_mean_difference_vs_40': float(delta.mean()),
                     'paired_median_difference_vs_40': float(delta.median())})
    return pd.DataFrame(rows)


def run(output_dir: Path, *, entries_path=ENTRIES, quality_path=QUALITY,
        stock_root=STOCK_QFQ, etf_root=ETF_QFQ) -> dict:
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f'RUN_ALREADY_EXISTS:{output_dir}')
    entries = pd.read_csv(entries_path)
    quality = pd.read_csv(quality_path)
    codes = sorted(quality.loc[quality.quality_status.eq('verified'), 'code'].unique())
    prices, paths = {}, [Path(entries_path), Path(quality_path)]
    for code in codes:
        frame, found = _load_price(Path(stock_root), code)
        prices['SEC-' + code.replace('.', '-')] = frame
        paths.extend(found)
    benchmark, found = _load_price(Path(etf_root), 'US.QQQ')
    paths.extend(found)
    paired, funnel = paired_horizons(entries, quality, prices, benchmark)
    summary = summarize(paired)
    manifest = {'status': 'exploratory_price_only', 'horizons': HORIZONS,
                'round_trip_cost': COST, 'sample': 'verified_survivor15_A_entries',
                'price_basis': 'Futu_QFQ_no_dividend_accounting',
                'input_sha256': {str(p): _hash(p) for p in paths}, 'funnel': funnel}
    output_dir.mkdir(parents=True)
    paired.to_csv(output_dir / 'paired_trades.csv.gz', index=False, compression='gzip')
    summary.to_csv(output_dir / 'summary.csv', index=False)
    (output_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False,
                                                        indent=2) + '\n')
    lines = ['# 固定持有期配对价格检验', '', f'同一批完整 120 日入场：{funnel["paired_entries"]} 笔。',
             f'排除：{funnel["excluded"]}。', '',
             '| 持有日 | 平均净收益 | 中位数 | 胜率 | 平均相对QQQ | 相对40日配对均值差 |',
             '|---:|---:|---:|---:|---:|---:|']
    for row in summary.itertuples(index=False):
        lines.append(f'| {row.holding_sessions} | {row.mean_net_return:.2%} | '
                     f'{row.median_net_return:.2%} | {row.win_rate:.1%} | '
                     f'{row.mean_excess_vs_qqq:.2%} | '
                     f'{row.paired_mean_difference_vs_40:+.2%} |')
    lines.extend(['', '价格收益近似；无硬止损、公司行动现金流、仓位和组合净值。'
                  '入场之间可能重叠，不能把平均收益或逐笔收益相加解释为账户 CAGR。'])
    (output_dir / 'report.md').write_text('\n'.join(lines) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.output_dir)['funnel'], ensure_ascii=False))
