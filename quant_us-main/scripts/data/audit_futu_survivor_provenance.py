#!/usr/bin/env python3
"""核对存续股票的本地 Futu 来源链；不把当前目录回填当作历史时点证明。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd


def audit_provenance(quality, basic, master, symbols, bars, actions, action_checks):
    selected = quality.loc[quality.quality_status.eq('verified'), ['code', 'security_id']].copy()
    if selected.empty or selected.code.duplicated().any() or selected.security_id.duplicated().any():
        raise ValueError('SELECTED_SECURITY_NOT_UNIQUE')
    for frame, column in ((basic, 'code'), (master, 'security_id'),
                          (symbols, 'security_id'), (action_checks, 'code')):
        if column not in frame or frame[column].duplicated().any():
            raise ValueError(f'SOURCE_KEY_NOT_UNIQUE:{column}')
    if bars.duplicated(['security_id', 'date']).any():
        raise ValueError('DUPLICATE_DAILY_BAR')
    dates = bars.assign(date=pd.to_datetime(bars.date).dt.normalize()).groupby('security_id').date
    first, last = dates.min(), dates.max()
    b = basic.set_index('code'); m = master.set_index('security_id')
    s = symbols.set_index('security_id'); c = action_checks.set_index('code')
    rows = []
    for item in selected.itertuples(index=False):
        code, sec = item.code, item.security_id
        problems = []
        if code not in b.index or sec not in m.index or sec not in s.index or code not in c.index or sec not in first:
            problems.append('MISSING_SOURCE_ROW')
            rows.append({'code': code, 'security_id': sec, 'source_consistent': False,
                         'issues': ';'.join(problems)})
            continue
        raw, current, symbol, check = b.loc[code], m.loc[sec], s.loc[sec], c.loc[code]
        listing = pd.to_datetime(raw.listing_date, errors='coerce')
        master_listing = pd.to_datetime(current.listed_at, errors='coerce')
        symbol_start = pd.to_datetime(symbol.valid_from, errors='coerce')
        first_bar = first.loc[sec]
        if str(symbol.symbol) != code or str(current.source_record_id) != code or str(symbol.source_record_id) != code:
            problems.append('SYMBOL_SOURCE_MISMATCH')
        if pd.isna(listing) or pd.isna(master_listing) or pd.isna(symbol_start):
            problems.append('LISTING_DATE_MISSING')
        elif listing != master_listing or listing != symbol_start or listing > first_bar:
            problems.append('LISTING_DATE_CONFLICT')
        if str(raw.stock_type) != 'STOCK' or str(current.asset_type) != 'stock':
            problems.append('ASSET_TYPE_CONFLICT')
        selected_actions = actions.loc[actions.security_id.eq(sec)].copy()
        selected_actions['ex_date'] = pd.to_datetime(selected_actions.ex_date, errors='coerce')
        outside_future = int(selected_actions.ex_date.gt(last.loc[sec]).sum())
        unexplained_missing_sides = max(0, int(check.no_both_sides) - outside_future)
        if int(check.mismatched) or unexplained_missing_sides:
            problems.append('ACTION_PRICE_CHECK_INCOMPLETE')
        rows.append({'code': code, 'security_id': sec, 'futu_stock_id': str(raw.stock_id),
                     'futu_listing_date': '' if pd.isna(listing) else listing.date().isoformat(),
                     'first_raw_bar': first_bar.date().isoformat(),
                     'action_rows': len(selected_actions),
                     'action_check_matched': int(check.matched),
                     'action_check_missing_sides': int(check.no_both_sides),
                     'future_actions_without_bars': outside_future,
                     'unexplained_missing_sides': unexplained_missing_sides,
                     'master_observed_at_missing': pd.isna(current.source_observed_at) or not str(current.source_observed_at).strip(),
                     'action_observed_at_missing': int(selected_actions.source_observed_at.isna().sum()),
                     'source_consistent': not problems, 'issues': ';'.join(problems)})
    return pd.DataFrame(rows).sort_values('code').reset_index(drop=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('quality', 'basic', 'master', 'symbols', 'bars', 'actions', 'action-checks'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--output-dir', required=True)
    args = p.parse_args()
    out = Path(args.output_dir)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'输出目录非空，禁止覆盖: {out}')
    names = ('quality', 'basic', 'master', 'symbols', 'bars', 'actions', 'action-checks')
    paths = {name: Path(getattr(args, name.replace('-', '_'))) for name in names}
    inputs = [pd.read_csv(paths[name]) for name in names]
    report = audit_provenance(*inputs)
    out.mkdir(parents=True, exist_ok=True)
    report.to_csv(out / 'security_provenance.csv', index=False)
    summary = {'securities': len(report), 'source_consistent': int(report.source_consistent.sum()),
               'issues': report.loc[~report.source_consistent, ['code', 'issues']].to_dict('records'),
               'listing_historically_verified': 0,
               'reason': 'current Futu basicinfo has no historical source_observed_at',
               'input_sha256': {name: hashlib.sha256(path.read_bytes()).hexdigest()
                                for name, path in paths.items()}}
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
