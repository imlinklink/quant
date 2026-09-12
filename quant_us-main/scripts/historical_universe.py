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
        missing_data = joined[[price_col, volume_col]].isna().any(axis=1)
        joined['quality'] = np.where(missing_data, 'missing_liquidity', 'good')
        lq = joined['liquidity_quality'] if 'liquidity_quality' in joined else None
        if lq is not None:
            joined['quality'] = np.where(lq.eq('good'), joined['quality'], 'quality_fail')
        # 数据质量不通过的区间不得进入可交易集合（设计 §9 / 操作手册 §5.5）。
        joined['eligible'] = joined['eligible'] & joined['quality'].eq('good')
        # 拒绝原因：先区分「上市初期流动性历史不足」，再区分「当日无数据」，
        # 再区分「有数据但未过质量门」，最后才是价格/成交额门槛（设计 §9 原因枚举）。
        insufficient = (lq.eq('insufficient_history') if lq is not None
                        else pd.Series(False, index=joined.index))
        joined['reason'] = np.select(
            [insufficient,
             missing_data,
             joined['quality'].eq('quality_fail'),
             joined[price_col].lt(min_price),
             joined[volume_col].lt(min_dollar_volume)],
            ['INSUFFICIENT_LIQUIDITY_HISTORY', 'MISSING_LIQUIDITY', 'DATA_QUALITY_FAIL',
             'PRICE_TOO_LOW', 'DOLLAR_VOLUME_TOO_LOW'],
            default='ELIGIBLE')
        joined['universe_date'] = session
        rows.append(joined)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


# ---------------------------------------------------------------------------
# v2：按 security_id 的历史时点 universe（技术设计 §2.5）。v1 接口保留供试点复现。
# ---------------------------------------------------------------------------

V2_UNIVERSE_COLUMNS = ('universe_date', 'security_id', 'symbol_as_of', 'asset_type',
                       'listed', 'tradable', 'previous_raw_close', 'adv20_usd',
                       'liquidity_as_of', 'eligible', 'reason',
                       'master_version', 'price_version', 'quality_status')

V2_REASONS = ('NOT_LISTED', 'DELISTED', 'MASTER_CONFLICT', 'SYMBOL_UNAVAILABLE',
              'MISSING_BARS', 'ACTION_UNRESOLVED', 'PRICE_TOO_LOW',
              'DOLLAR_VOLUME_TOO_LOW', 'ELIGIBLE')


def _symbol_as_of(symbols: pd.DataFrame, session) -> dict:
    """某交易日各 security_id 当时有效的 symbol。"""
    if symbols is None or symbols.empty:
        return {}
    s = symbols.copy()
    s['vfrom'] = pd.to_datetime(s['valid_from'], errors='coerce')
    s['vto'] = pd.to_datetime(s['valid_to'], errors='coerce')
    hit = s[(s['vfrom'] <= session) & (s['vto'].isna() | (session < s['vto']))]
    return dict(zip(hit['security_id'].astype(str), hit['symbol'].astype(str)))


def build_point_in_time_universe_v2(master: pd.DataFrame, symbols: pd.DataFrame,
                                    liquidity: pd.DataFrame, sessions, *,
                                    min_price=5.0, min_dollar_volume=5_000_000.0,
                                    main_asset_type='stock', master_version='',
                                    price_version='', unresolved_actions=()):
    """按 security_id 构建历史时点 universe；只用 T−1 已知价格与流动性。

    - 退市日之后不得 eligible；ticker 改名不产生两只“新股票”（键为 security_id）。
    - 无法证明交易状态（无有效 symbol）的日期不进入主样本，原因记为 SYMBOL_UNAVAILABLE。
    - 被拒绝记录全部保留并标注 reason；`liquidity_as_of < universe_date` 强制成立。
    - 主样本 = `eligible & asset_type == main_asset_type`；ETF/杠杆 ETF 输出独立切片。
    """
    for column in ('security_id', 'asset_type', 'listed_at', 'delisted_at', 'quality_status'):
        if column not in master.columns:
            raise ValueError(f'security master 缺字段: {column}')
    required_liq = {'security_id', 'date', 'previous_raw_close', 'adv20_usd', 'liquidity_as_of'}
    if not required_liq.issubset(liquidity.columns):
        raise ValueError('liquidity 缺字段: ' + ','.join(sorted(required_liq - set(liquidity.columns))))
    liq = liquidity.copy()
    liq['date'] = pd.to_datetime(liq['date']).dt.normalize()
    liq['liquidity_as_of'] = pd.to_datetime(liq['liquidity_as_of'], errors='coerce').dt.normalize()
    if (liq['liquidity_as_of'] >= liq['date']).any():
        raise ValueError('LOOKAHEAD_LIQUIDITY: liquidity_as_of 必须早于 universe date')
    liq = liq[list(required_liq)].sort_values(['security_id', 'date']).drop_duplicates(
        ['security_id', 'date'])

    m = master.copy()
    m['vfrom'] = pd.to_datetime(m['valid_from'], errors='coerce') if 'valid_from' in m else pd.NaT
    m['vto'] = pd.to_datetime(m['valid_to'], errors='coerce') if 'valid_to' in m else pd.NaT
    m['listed_at'] = pd.to_datetime(m['listed_at'], errors='coerce')
    m['delisted_at'] = pd.to_datetime(m['delisted_at'], errors='coerce')
    unresolved = set(map(str, unresolved_actions))

    rows = []
    for session in pd.to_datetime(list(sessions)).normalize():
        if 'valid_from' in m:
            active = m[(m['vfrom'] <= session) & (m['vto'].isna() | (session < m['vto']))]
            active = active.sort_values('vfrom').drop_duplicates('security_id', keep='last')
        else:
            active = m
        symbols_today = _symbol_as_of(symbols, session)
        day = liq[liq['date'] == session]
        joined = active.merge(day, on='security_id', how='left')

        listed = (joined['listed_at'].notna() & (joined['listed_at'] <= session) &
                  (joined['delisted_at'].isna() | (joined['delisted_at'] >= session)))
        symbol_ok = joined['security_id'].astype(str).isin(symbols_today)
        joined['symbol_as_of'] = joined['security_id'].astype(str).map(symbols_today)
        tradable = listed & symbol_ok & joined['quality_status'].ne('conflict')
        has_bars = joined['previous_raw_close'].notna() & joined['adv20_usd'].notna()
        price_ok = joined['previous_raw_close'].ge(min_price)
        volume_ok = joined['adv20_usd'].ge(min_dollar_volume)
        joined['listed'] = listed
        joined['tradable'] = tradable
        joined['eligible'] = tradable & has_bars & price_ok & volume_ok
        joined['reason'] = np.select(
            [~listed & joined['listed_at'].notna() & (joined['listed_at'] > session),
             joined['delisted_at'].notna() & (joined['delisted_at'] < session),
             joined['quality_status'].eq('conflict'),
             ~symbol_ok,
             joined['security_id'].astype(str).isin(unresolved),
             ~has_bars,
             tradable & ~price_ok,
             tradable & ~volume_ok],
            ['NOT_LISTED', 'DELISTED', 'MASTER_CONFLICT', 'SYMBOL_UNAVAILABLE',
             'ACTION_UNRESOLVED', 'MISSING_BARS', 'PRICE_TOO_LOW', 'DOLLAR_VOLUME_TOO_LOW'],
            default='ELIGIBLE')
        joined['reason'] = np.where(joined['eligible'], 'ELIGIBLE', joined['reason'])
        joined['universe_date'] = session
        joined['master_version'] = master_version
        joined['price_version'] = price_version
        rows.append(joined)
    if not rows:
        return pd.DataFrame(columns=list(V2_UNIVERSE_COLUMNS))
    out = pd.concat(rows, ignore_index=True)
    return out[list(V2_UNIVERSE_COLUMNS)].sort_values(
        ['universe_date', 'security_id']).reset_index(drop=True)


def universe_reason_summary(universe: pd.DataFrame) -> pd.DataFrame:
    """拒绝原因计数（含被拒绝记录），作为数据质量报告的一部分。"""
    cols = ['asset_type', 'reason', 'eligible']
    if universe.empty:
        return pd.DataFrame(columns=['asset_type', 'reason', 'eligible', 'rows'])
    return (universe.groupby(['asset_type', 'reason', 'eligible'], dropna=False)
            .size().reset_index(name='rows'))


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
