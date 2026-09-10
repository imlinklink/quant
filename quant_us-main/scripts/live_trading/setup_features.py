"""中期买入 setup 的日线特征。纯函数，回测和实时扫描共用。"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

FEATURE_VERSION = 'daily-setup-v1'
REQUIRED_COLUMNS = {'date', 'open', 'high', 'low', 'close', 'volume'}


def _utc(value) -> pd.Timestamp:
    out = pd.Timestamp(value)
    return out.tz_localize('UTC') if out.tzinfo is None else out.tz_convert('UTC')


def completed_daily_bars(frame: pd.DataFrame, as_of) -> pd.DataFrame:
    """仅保留 as_of 之前已经收盘的日线；date 按交易日标签处理。"""
    if frame is None or frame.empty or not REQUIRED_COLUMNS.issubset(frame.columns):
        return pd.DataFrame(columns=sorted(REQUIRED_COLUMNS))
    d = frame.copy()
    # 数据源日线时间是交易日标签。用纽约 16:00 构造真实 bar_end，正确处理夏令时。
    labels = pd.to_datetime(d['date']).dt.date
    local_midnight = pd.DatetimeIndex(pd.to_datetime(labels)).tz_localize('America/New_York')
    bar_end = local_midnight + pd.Timedelta(hours=16)
    mask = bar_end.tz_convert('UTC') <= _utc(as_of)
    d = d[mask].copy()
    d['date'] = pd.to_datetime(labels[mask], utc=True)
    return d.sort_values('date').drop_duplicates('date').reset_index(drop=True)


def _atr(d: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = d['close'].shift(1)
    tr = pd.concat((d['high'] - d['low'], (d['high'] - prev).abs(),
                    (d['low'] - prev).abs()), axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _confirmed_pivots(d: pd.DataFrame, right_bars: int = 2):
    lows = []
    for i in range(2, len(d) - right_bars):
        value = float(d['low'].iloc[i])
        if value < float(d['low'].iloc[i - 2:i].min()) and value < float(
                d['low'].iloc[i + 1:i + 1 + right_bars].min()):
            lows.append(i)
    return lows


def compute_setup_features(stock_bars: pd.DataFrame, sector_bars: Optional[pd.DataFrame],
                           market_bars: Optional[pd.DataFrame], as_of,
                           config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """生成可冻结的 SetupFeatures。数据不足时 fail closed，不填造指标。"""
    cfg = config or {}
    minimum = int(cfg.get('min_daily_bars', 250))
    d = completed_daily_bars(stock_bars, as_of)
    if len(d) < minimum:
        return {'feature_version': FEATURE_VERSION, 'as_of': _utc(as_of).isoformat(),
                'quality': {'status': 'fail', 'reasons': ['INSUFFICIENT_DAILY_BARS'],
                            'bar_count': len(d), 'required': minimum}}
    s = completed_daily_bars(sector_bars, as_of) if sector_bars is not None else pd.DataFrame()
    m = completed_daily_bars(market_bars, as_of) if market_bars is not None else pd.DataFrame()
    close = d['close'].astype(float)
    ma20, ma50, ma200 = close.rolling(20).mean(), close.rolling(50).mean(), close.rolling(200).mean()
    atr14 = _atr(d, 14)
    pivots = _confirmed_pivots(d)
    last_pivots = pivots[-2:]
    swing_low = float(d['low'].iloc[last_pivots[-1]]) if last_pivots else None
    prior_swing_low = float(d['low'].iloc[last_pivots[-2]]) if len(last_pivots) > 1 else None
    higher_low = bool(swing_low is not None and prior_swing_low is not None and
                      swing_low > prior_swing_low)
    rs20 = None
    if len(s) >= 21:
        pair = pd.merge(d[['date', 'close']], s[['date', 'close']], on='date',
                        suffixes=('_stock', '_sector')).tail(21)
        if len(pair) == 21 and pair['date'].iloc[-1] == d['date'].iloc[-1]:
            rs20 = (float(pair['close_stock'].iloc[-1] / pair['close_stock'].iloc[0]) -
                    float(pair['close_sector'].iloc[-1] / pair['close_sector'].iloc[0]))
    market_above_ma200 = None
    if len(m) >= 200:
        market_above_ma200 = bool(float(m['close'].iloc[-1]) > float(m['close'].tail(200).mean()))
    lookback = int(cfg.get('drawdown_window', 60))
    recent = d.tail(lookback)
    rolling_high = float(recent['high'].max())
    low_idx = int(np.argmin(recent['low'].to_numpy()))
    days_since_low = len(recent) - 1 - low_idx
    stabilization_days = int(cfg.get('stabilization_days', 5))
    no_new_low = days_since_low >= stabilization_days
    reversal_level = float(d['high'].iloc[-6:-1].max()) if len(d) >= 6 else None
    features = {
        'close': float(close.iloc[-1]), 'ma20': float(ma20.iloc[-1]),
        'ma50': float(ma50.iloc[-1]), 'ma200': float(ma200.iloc[-1]),
        'ma20_slope_5d': float(ma20.iloc[-1] / ma20.iloc[-6] - 1),
        'ma50_slope_20d': float(ma50.iloc[-1] / ma50.iloc[-21] - 1),
        'atr14': float(atr14.iloc[-1]),
        'drawdown_60d': float(close.iloc[-1] / rolling_high - 1),
        'days_since_low': int(days_since_low), 'no_new_low': bool(no_new_low),
        'relative_strength_20d': rs20, 'market_above_ma200': market_above_ma200,
        'volume_ratio_20d': float(d['volume'].iloc[-1] /
                                  d['volume'].iloc[-21:-1].mean()),
        'one_day_return': float(close.iloc[-1] / close.iloc[-2] - 1),
    }
    finite = all(np.isfinite(features[k]) for k in
                 ('close', 'ma20', 'ma50', 'ma200', 'atr14'))
    quality_reasons = [] if finite else ['NON_FINITE_REQUIRED_FEATURE']
    if sector_bars is not None and rs20 is None:
        quality_reasons.append('SECTOR_ALIGNMENT_FAILED')
    structure = {
        'swing_low': swing_low, 'prior_swing_low': prior_swing_low,
        'higher_low': higher_low, 'reversal_level': reversal_level,
        'invalidation_price': swing_low,
    }
    return {'feature_version': FEATURE_VERSION, 'as_of': _utc(as_of).isoformat(),
            'session': str(d['date'].iloc[-1].date()), 'bar_count': len(d),
            'features': features, 'structure': structure,
            'quality': {'status': 'pass' if not quality_reasons else 'fail',
                        'reasons': quality_reasons}}
