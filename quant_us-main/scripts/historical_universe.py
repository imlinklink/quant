#!/usr/bin/env python3
"""从 security master 和当时流动性生成 point-in-time universe。"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.data.io_utils import read_frame, write_frame
from scripts.data.trading_calendar import sessions as market_sessions

MASTER_COLUMNS = {'code', 'listing_date', 'delisting_date', 'asset_type'}


def validate_security_master(master: pd.DataFrame) -> list:
    errors = []
    missing = MASTER_COLUMNS - set(master.columns)
    if missing:
        return ['MISSING_COLUMNS:' + ','.join(sorted(missing))]
    if master['code'].duplicated().any():
        errors.append('DUPLICATE_CODE')
    listing = pd.to_datetime(master['listing_date'], errors='coerce')
    delisting = pd.to_datetime(master['delisting_date'], errors='coerce')
    if listing.isna().any(): errors.append('INVALID_LISTING_DATE')
    if ((delisting.notna()) & (delisting < listing)).any(): errors.append('DELIST_BEFORE_LISTING')
    return errors


def build_point_in_time_universe(master: pd.DataFrame, liquidity: pd.DataFrame,
                                 sessions, min_price=5.0,
                                 min_dollar_volume=5_000_000.0,
                                 allowed_types=('stock', 'leveraged_etf'),
                                 allow_legacy_liquidity=False) -> pd.DataFrame:
    errors = validate_security_master(master)
    if errors:
        raise ValueError(';'.join(errors))
    m = master.copy()
    m['listing_date'] = pd.to_datetime(m['listing_date']).dt.normalize()
    m['delisting_date'] = pd.to_datetime(m['delisting_date'], errors='coerce').dt.normalize()
    liq = liquidity.copy()
    point_in_time = {'date', 'code', 'previous_close', 'adv20', 'liquidity_as_of'}
    legacy = {'date', 'code', 'close', 'dollar_volume'}
    if point_in_time.issubset(liq):
        required = point_in_time
    elif allow_legacy_liquidity and legacy.issubset(liq):
        required = legacy
    else:
        raise ValueError('liquidity 必须包含 point-in-time 字段: ' + ','.join(sorted(point_in_time)))
    if not required.issubset(liq):
        raise ValueError('liquidity 缺字段: ' + ','.join(sorted(required - set(liq))))
    liq['date'] = pd.to_datetime(liq['date']).dt.normalize()
    price_col = 'previous_close' if 'previous_close' in liq else 'close'
    volume_col = 'adv20' if 'adv20' in liq else 'dollar_volume'
    if 'quality' in liq:
        liq = liq.rename(columns={'quality': 'liquidity_quality'})
    if 'liquidity_as_of' in liq:
        liq['liquidity_as_of'] = pd.to_datetime(liq['liquidity_as_of'], errors='coerce').dt.normalize()
        if (liq['liquidity_as_of'] >= liq['date']).any():
            raise ValueError('LOOKAHEAD_LIQUIDITY: liquidity_as_of 必须早于 universe date')
    liq = liq.sort_values(['code', 'date']).drop_duplicates(['code', 'date'])
    rows = []
    for session in pd.to_datetime(list(sessions)).normalize():
        active = m[(m['listing_date'] <= session) &
                   (m['delisting_date'].isna() | (m['delisting_date'] >= session)) &
                   m['asset_type'].isin(allowed_types)]
        day = liq[liq['date'] == session]
        joined = active.merge(day, on='code', how='left')
        joined['eligible'] = (joined[price_col].ge(min_price) &
                              joined[volume_col].ge(min_dollar_volume))
        joined['quality'] = np.where(joined[[price_col, volume_col]].isna().any(axis=1),
                                     'missing_liquidity', 'good')
        if 'liquidity_quality' in joined:
            joined['quality'] = np.where(joined['liquidity_quality'].eq('good'),
                                         joined['quality'], 'quality_fail')
        joined['reason'] = np.select(
            [joined['quality'].ne('good'), joined[price_col].lt(min_price),
             joined[volume_col].lt(min_dollar_volume)],
            ['MISSING_LIQUIDITY', 'PRICE_TOO_LOW', 'DOLLAR_VOLUME_TOO_LOW'],
            default='ELIGIBLE')
        joined['universe_date'] = session
        rows.append(joined)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def main():
    p = argparse.ArgumentParser(description='构建历史时点股票池')
    p.add_argument('--master', required=True); p.add_argument('--liquidity', required=True)
    p.add_argument('--start', required=True); p.add_argument('--end', required=True)
    p.add_argument('--output', required=True); p.add_argument('--min-price', type=float, default=5)
    p.add_argument('--min-dollar-volume', type=float, default=5_000_000)
    p.add_argument('--calendar'); p.add_argument('--reference-daily')
    p.add_argument('--allow-legacy-liquidity', action='store_true',
                   help='仅用于复现旧实验；正式实验禁止使用')
    args = p.parse_args()
    if args.calendar:
        cal = read_frame(args.calendar)
    else:
        cal = market_sessions(args.start, args.end, args.reference_daily)
    out = build_point_in_time_universe(read_frame(args.master), read_frame(args.liquidity),
                                       pd.to_datetime(cal.session_date), args.min_price,
                                       args.min_dollar_volume,
                                       allow_legacy_liquidity=args.allow_legacy_liquidity)
    write_frame(out, args.output)
    print(f'wrote {len(out)} universe rows to {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
