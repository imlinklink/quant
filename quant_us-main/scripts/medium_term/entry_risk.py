"""中期科技股初始止损和风险定仓。"""
from __future__ import annotations

import math

import pandas as pd


def medium_initial_stop(entry_price: float, atr14: float, *,
                        minimum_distance_pct=.08, atr_multiple=2.5) -> float:
    """止损距离取8%和2.5 ATR中较宽者；无法得到正止损时拒绝。"""
    price, atr = float(entry_price), float(atr14)
    if not math.isfinite(price) or not math.isfinite(atr) or price <= 0 or atr <= 0:
        raise ValueError('INVALID_STOP_INPUT')
    distance = max(minimum_distance_pct * price, atr_multiple * atr)
    stop = price - distance
    if stop <= 0:
        raise ValueError('INITIAL_STOP_NON_POSITIVE')
    return stop


def risk_sized_shares(entry_price: float, initial_stop: float, equity: float, cash: float, *,
                      risk_fraction=.01, max_weight=.20, fee_rate=0.,
                      allow_fractional=False) -> float:
    """同时受1%风险、20%市值和现金约束。"""
    price, stop, nav, available = map(float, (entry_price, initial_stop, equity, cash))
    if not (price > stop > 0 and nav > 0 and available >= 0):
        raise ValueError('INVALID_POSITION_SIZE_INPUT')
    if not (0 < risk_fraction <= 1 and 0 < max_weight <= 1 and 0 <= fee_rate < 1):
        raise ValueError('INVALID_POSITION_SIZE_CONFIG')
    distance = price - stop
    quantity = min(nav * risk_fraction / distance,
                   nav * max_weight / price,
                   available / (price * (1 + fee_rate)))
    return float(quantity if allow_fractional else math.floor(quantity))


def attach_initial_stops(candidates: pd.DataFrame, features: pd.DataFrame,
                         raw_bars: pd.DataFrame) -> pd.DataFrame:
    """用信号日已知 ATR 和次日原始开盘价生成止损，不读取次日收盘。"""
    specs = ((candidates, {'security_id', 'decision_session', 'execution_session'}, 'CANDIDATE'),
             (features, {'security_id', 'session', 'asof_atr', 'scale_to_next'}, 'FEATURE'),
             (raw_bars, {'security_id', 'session', 'raw_open'}, 'RAW'))
    for frame, required, label in specs:
        if missing := required - set(frame.columns):
            raise ValueError(f'{label}_STOP_COLUMNS_MISSING:{",".join(sorted(missing))}')
    c, f, r = candidates.copy(), features.copy(), raw_bars.copy()
    c['decision_session'] = pd.to_datetime(c.decision_session).dt.tz_localize(None).dt.normalize()
    c['execution_session'] = pd.to_datetime(c.execution_session).dt.tz_localize(None).dt.normalize()
    if (c.execution_session <= c.decision_session).any():
        raise ValueError('STOP_EXECUTION_NOT_AFTER_DECISION')
    for frame in (f, r):
        frame['session'] = pd.to_datetime(frame.session).dt.tz_localize(None).dt.normalize()
    f = f[['security_id', 'session', 'asof_atr', 'scale_to_next']].rename(
        columns={'session': 'decision_session'})
    r = r[['security_id', 'session', 'raw_open']].rename(
        columns={'session': 'execution_session', 'raw_open': 'entry_price'})
    out = c.merge(f, on=['security_id', 'decision_session'], how='left', validate='many_to_one')
    out = out.merge(r, on=['security_id', 'execution_session'], how='left', validate='many_to_one')
    numeric = out[['asof_atr', 'scale_to_next', 'entry_price']].apply(pd.to_numeric,
                                                                      errors='coerce')
    if numeric.isna().any(axis=None) or (numeric <= 0).any(axis=None):
        raise ValueError('STOP_INPUT_LOOKUP_MISSING_OR_INVALID')
    out['atr14_raw_at_execution'] = numeric.asof_atr * numeric.scale_to_next
    out['initial_stop'] = [medium_initial_stop(price, atr) for price, atr in zip(
        numeric.entry_price, out.atr14_raw_at_execution)]
    out['stop_feature_session'] = out.decision_session
    return out
