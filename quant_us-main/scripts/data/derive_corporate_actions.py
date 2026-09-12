#!/usr/bin/env python3
"""从「前复权 vs 不复权」价格差反推公司行动（富途路线的补充）。

富途不提供公司行动表，但 `AuType.QFQ` 前复权序列对拆股与现金股息都做了调整；
用同一标的的 QFQ 与不复权收盘之比（adjustment factor）在相邻交易日的跳变，
可以反推出行动日期与因子。拆分因子可靠；股息为近似值，且全部标记 `unverified`
——必须人工复核后才能作为正式 `corporate_actions`（技术设计 §2.3/§2.4）。
"""
from __future__ import annotations

from fractions import Fraction

import numpy as np
import pandas as pd

SOURCE_ID = 'derived_qfq_vs_raw'


def _classify(step, prev_close, tolerance, max_ratio_term):
    """把因子跳变 step 归类为拆股/反向拆股或现金股息。"""
    for q in range(1, max_ratio_term + 1):
        p = round(step * q)
        if p < 1:
            continue
        if abs(step - p / q) / step <= tolerance:
            ratio = Fraction(p, q).numerator / Fraction(p, q).denominator
            return ('split' if ratio > 1 else 'reverse_split'), ratio, 0.0
    return 'cash_dividend', 0.0, round(prev_close * (1.0 - 1.0 / step), 6)


def derive_actions(raw_bars: pd.DataFrame, adjusted_bars: pd.DataFrame, security_id: str,
                   *, tolerance=0.005, max_ratio_term=12) -> pd.DataFrame:
    """对单一标的反推公司行动。raw_bars/adjusted_bars 需含 session,close。"""
    a = raw_bars[['session', 'close']].rename(columns={'close': 'raw'})
    b = adjusted_bars[['session', 'close']].rename(columns={'close': 'adj'})
    d = a.merge(b, on='session').sort_values('session').reset_index(drop=True)
    if d.empty:
        return pd.DataFrame(columns=['security_id', 'action_type', 'ex_date', 'ratio',
                                     'cash_amount', 'source_id', 'quality_status'])
    d['session'] = pd.to_datetime(d['session'])
    d['factor'] = d['adj'].astype(float) / d['raw'].astype(float)
    d['step'] = d['factor'] / d['factor'].shift(1)
    rows = []
    for i in range(1, len(d)):
        step = d['step'].iloc[i]
        if not np.isfinite(step) or abs(step - 1.0) <= tolerance:
            continue
        kind, ratio, cash = _classify(step, float(d['raw'].iloc[i - 1]), tolerance, max_ratio_term)
        rows.append({'security_id': security_id, 'action_type': kind,
                     'ex_date': d['session'].iloc[i].strftime('%Y-%m-%d'),
                     'ratio': ratio, 'cash_amount': cash,
                     'source_id': SOURCE_ID, 'quality_status': 'unverified'})
    return pd.DataFrame(rows, columns=['security_id', 'action_type', 'ex_date', 'ratio',
                                       'cash_amount', 'source_id', 'quality_status'])


def apply_adjustments(raw_bars: pd.DataFrame, actions: pd.DataFrame) -> pd.DataFrame:
    """用反推出的行动重算 as-of 复权价；用于与富途 QFQ 交叉验证反推质量。"""
    d = raw_bars.copy()
    d['session'] = pd.to_datetime(d['session'])
    d = d.sort_values('session').reset_index(drop=True)
    factors = pd.Series(1.0, index=d.index)
    for action in actions.sort_values('ex_date').to_dict('records'):
        kind = action['action_type']; ex = pd.Timestamp(action['ex_date'])
        if kind == 'split':
            f = 1.0 / float(action['ratio'])
        elif kind == 'reverse_split':
            f = 1.0 / float(action['ratio'])
        else:
            prev = d.loc[d['session'] < ex, 'close'].iloc[-1]
            f = 1.0 - float(action['cash_amount']) / float(prev)
        factors.loc[d['session'] < ex] *= f
    for column in ('open', 'high', 'low', 'close'):
        if column in d:
            d[column] = d[column].astype(float) * factors
    if 'volume' in d:
        d['volume'] = d['volume'].astype(float) / factors
    return d


def main():
    import argparse, json, sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[2]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.data.io_utils import read_frame, write_frame
    p = argparse.ArgumentParser(description='从 QFQ 与不复权价差反推公司行动')
    p.add_argument('--raw', required=True, help='不复权日线（security_id/date/close）')
    p.add_argument('--adjusted', required=True, help='前复权日线')
    p.add_argument('--security-id', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    raw = read_frame(args.raw); adj = read_frame(args.adjusted)
    for frame in (raw, adj):
        if 'session' not in frame and 'date' in frame:
            frame.rename(columns={'date': 'session'}, inplace=True)
    actions = derive_actions(raw, adj, args.security_id)
    write_frame(actions, args.output)
    print(json.dumps({'security_id': args.security_id, 'actions': len(actions)},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
