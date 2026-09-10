#!/usr/bin/env python3
"""从 security master 和当时流动性生成 point-in-time universe。"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

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
                                 allowed_types=('stock', 'leveraged_etf')) -> pd.DataFrame:
    errors = validate_security_master(master)
    if errors:
        raise ValueError(';'.join(errors))
    m = master.copy()
    m['listing_date'] = pd.to_datetime(m['listing_date']).dt.normalize()
    m['delisting_date'] = pd.to_datetime(m['delisting_date'], errors='coerce').dt.normalize()
    liq = liquidity.copy()
    required = {'date', 'code', 'close', 'dollar_volume'}
    if not required.issubset(liq):
        raise ValueError('liquidity 缺字段: ' + ','.join(sorted(required - set(liq))))
    liq['date'] = pd.to_datetime(liq['date']).dt.normalize()
    liq = liq.sort_values(['code', 'date']).drop_duplicates(['code', 'date'])
    rows = []
    for session in pd.to_datetime(list(sessions)).normalize():
        active = m[(m['listing_date'] <= session) &
                   (m['delisting_date'].isna() | (m['delisting_date'] >= session)) &
                   m['asset_type'].isin(allowed_types)]
        day = liq[liq['date'] == session]
        joined = active.merge(day, on='code', how='left')
        joined['eligible'] = (joined['close'].ge(min_price) &
                              joined['dollar_volume'].ge(min_dollar_volume))
        joined['quality'] = np.where(joined[['close', 'dollar_volume']].isna().any(axis=1),
                                     'missing_liquidity', 'good')
        joined['reason'] = np.select(
            [joined['quality'].ne('good'), joined['close'].lt(min_price),
             joined['dollar_volume'].lt(min_dollar_volume)],
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
    args = p.parse_args()
    out = build_point_in_time_universe(pd.read_csv(args.master), pd.read_csv(args.liquidity),
                                       pd.bdate_range(args.start, args.end), args.min_price,
                                       args.min_dollar_volume)
    target=Path(args.output)
    if target.exists(): raise FileExistsError(f'禁止覆盖实验产物: {target}')
    target.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(target, index=False)
    print(f'wrote {len(out)} universe rows to {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
