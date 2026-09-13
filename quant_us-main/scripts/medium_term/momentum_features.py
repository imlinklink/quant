"""6个月、12-1个月动量和可复现截面排名。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.data.price_views import build_price_view


REQUIRED = {'security_id', 'session'}


def _normalise(prices: pd.DataFrame, price_col: str) -> pd.DataFrame:
    missing = (REQUIRED | {price_col}) - set(prices.columns)
    if missing:
        raise ValueError(f'MOMENTUM_COLUMNS_MISSING:{",".join(sorted(missing))}')
    d = prices[['security_id', 'session', price_col]].copy()
    d['security_id'] = d.security_id.astype(str)
    d['session'] = pd.to_datetime(d.session).dt.tz_localize(None).dt.normalize()
    if d.duplicated(['security_id', 'session']).any():
        raise ValueError('DUPLICATE_SECURITY_SESSION')
    d[price_col] = pd.to_numeric(d[price_col], errors='coerce')
    if (~np.isfinite(d[price_col]) | (d[price_col] <= 0)).any():
        raise ValueError('INVALID_MOMENTUM_PRICE')
    return d.sort_values(['security_id', 'session'])


def momentum_snapshot(prices: pd.DataFrame, decision_session, *,
                      price_col='asof_close', lookback_6m=126,
                      skip_1m=21, lookback_12m=252) -> pd.DataFrame:
    """计算决策日截面；所有位置均以该证券已完成的 session 计数。

    6m 使用 p[t] / p[t-126]；12-1 使用 p[t-21] / p[t-252]。
    缺少完整窗口时保留证券并写明拒绝原因，便于审计分母。
    """
    if not (0 < skip_1m < lookback_12m) or lookback_6m <= 0:
        raise ValueError('INVALID_MOMENTUM_WINDOWS')
    d = _normalise(prices, price_col)
    decision = pd.Timestamp(decision_session)
    if decision.tzinfo is not None:
        decision = decision.tz_convert('UTC').tz_localize(None)
    decision = decision.normalize()
    d = d[d.session <= decision]
    rows = []
    for security_id, group in d.groupby('security_id', sort=True):
        g = group.sort_values('session').reset_index(drop=True)
        exact = bool(len(g) and g.session.iloc[-1] == decision)
        enough = exact and len(g) >= lookback_12m + 1
        row = {'security_id': security_id, 'decision_session': decision,
               'observation_session': g.session.iloc[-1] if len(g) else pd.NaT,
               'eligible': enough, 'reject_reason': ''}
        if not exact:
            row['reject_reason'] = 'DECISION_PRICE_MISSING'
        elif not enough:
            row['reject_reason'] = 'INSUFFICIENT_LOOKBACK'
        else:
            values = g[price_col].to_numpy(float)
            row['mom_6m'] = values[-1] / values[-(lookback_6m + 1)] - 1
            row['mom_12_1'] = values[-(skip_1m + 1)] / values[-(lookback_12m + 1)] - 1
        rows.append(row)
    columns = ['security_id', 'decision_session', 'observation_session',
               'eligible', 'reject_reason', 'mom_6m', 'mom_12_1']
    return pd.DataFrame(rows).reindex(columns=columns)


def rank_cross_section(snapshot: pd.DataFrame) -> pd.DataFrame:
    """分别计算动量百分位并等权；security_id 是稳定的并列排序键。"""
    required = {'security_id', 'eligible', 'mom_6m', 'mom_12_1'}
    missing = required - set(snapshot.columns)
    if missing:
        raise ValueError(f'RANK_COLUMNS_MISSING:{",".join(sorted(missing))}')
    out = snapshot.copy()
    valid = (out.eligible.astype(bool) & out.mom_6m.notna() & out.mom_12_1.notna())
    ranked = out.loc[valid].sort_values('security_id')
    for source, target in (('mom_6m', 'rank_6m'), ('mom_12_1', 'rank_12_1')):
        out.loc[ranked.index, target] = ranked[source].rank(method='average', pct=True)
    out['momentum_score'] = .5 * out.get('rank_6m', np.nan) + .5 * out.get('rank_12_1', np.nan)
    out['rank'] = pd.Series(pd.NA, index=out.index, dtype='Int64')
    order = out.loc[valid].sort_values(
        ['momentum_score', 'mom_6m', 'security_id'], ascending=[False, False, True])
    out.loc[order.index, 'rank'] = range(1, len(order) + 1)
    return out.sort_values(['eligible', 'rank', 'security_id'], ascending=[False, True, True])


def point_in_time_momentum_snapshot(raw_bars: pd.DataFrame, actions: pd.DataFrame,
                                    decision_session, **lookbacks) -> pd.DataFrame:
    """逐决策日复权整段回看序列，避免逐日锚定价跨拆股计算虚假动量。"""
    view = build_price_view(raw_bars, actions, price_basis='asof_adjusted',
                            as_of=decision_session)
    return momentum_snapshot(view, decision_session, price_col='close', **lookbacks)
