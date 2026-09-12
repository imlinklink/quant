#!/usr/bin/env python3
"""逐日 as-of 特征价（技术设计 §2.4；交接方案 §4.2）。

问题：用 2026 年的一张最终复权快照给所有历史决策算均线/ATR，会把**未来**的拆股/分红
提前反映到过去，属于前视。正确做法是：对每个决策日 `d`，只用 `ex_date <= d` 的公司行动
构造 as-of 序列，再在它上面算特征。

本模块提供：
- `asof_features(bars, actions, decision_day, ...)` —— 决策日 d 的正确特征；
- `full_snapshot_features(bars, actions, ...)` —— 对照用的**错法**（as_of=最新），仅用于
  验证与量化差异，**不得**用于正式回测。
"""
from __future__ import annotations

import pandas as pd

from scripts.data.price_views import build_price_view

MA_WINDOWS = (20, 50, 200)
ATR_PERIOD = 14


def _features_on(view: pd.DataFrame, ma_windows, atr_period) -> dict:
    view = view.sort_values('session').reset_index(drop=True)
    close = view['close'].astype(float)
    hi = view['high'].astype(float); lo = view['low'].astype(float)
    prev = close.shift(1)
    tr = pd.concat([hi - lo, (hi - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
    out = {'sessions': int(len(view)), 'close': float(close.iloc[-1]) if len(close) else None,
           'action_version': str(view['action_version'].iloc[-1]) if len(view) else ''}
    for window in ma_windows:
        out[f'ma{window}'] = float(close.tail(window).mean()) if len(close) >= window else None
    out['atr'] = float(tr.tail(atr_period).mean()) if len(view) >= atr_period else None
    return out


def asof_features(bars: pd.DataFrame, actions: pd.DataFrame, decision_day, *,
                  ma_windows=MA_WINDOWS, atr_period=ATR_PERIOD) -> dict:
    """决策日 `decision_day` 的正确特征：只应用 `ex_date <= decision_day` 的行动。"""
    view = build_price_view(bars, actions, price_basis='asof_adjusted', as_of=decision_day)
    return _features_on(view, ma_windows, atr_period)


def full_snapshot_features(bars: pd.DataFrame, actions: pd.DataFrame, decision_day=None, *,
                           ma_windows=MA_WINDOWS, atr_period=ATR_PERIOD) -> dict:
    """同一决策日的错法对照：行情截到当日，却套用最终快照中的未来行动。"""
    last = pd.to_datetime(bars['session']).max()
    view = build_price_view(bars, actions, price_basis='asof_adjusted', as_of=last)
    if decision_day is not None:
        view = view[pd.to_datetime(view['session']) <= pd.Timestamp(decision_day).normalize()]
    return _features_on(view, ma_windows, atr_period)


def feature_drift(bars: pd.DataFrame, actions: pd.DataFrame, decision_day, **kw) -> dict:
    """返回 as-of 与"错法"在决策日的相对差异，用于量化前视影响。"""
    good = asof_features(bars, actions, decision_day, **kw)
    naive = full_snapshot_features(bars, actions, decision_day, **kw)
    drift = {}
    for key, value in good.items():
        other = naive.get(key)
        if isinstance(value, float) and isinstance(other, float) and other:
            drift[key] = round(value / other - 1.0, 6)
    return {'decision_day': str(pd.Timestamp(decision_day).date()),
            'asof': good, 'full_snapshot': naive, 'relative_drift': drift}


if __name__ == '__main__':
    import argparse, json, sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[2]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.data.io_utils import read_frame
    p = argparse.ArgumentParser(description='逐日 as-of 特征（单日）')
    p.add_argument('--bars', required=True); p.add_argument('--actions', required=True)
    p.add_argument('--security-id', required=True)
    p.add_argument('--decision-day', required=True, help='YYYY-MM-DD')
    args = p.parse_args()
    bars = read_frame(args.bars); actions = read_frame(args.actions)
    if 'session' not in bars.columns and 'date' in bars.columns:
        bars = bars.rename(columns={'date': 'session'})
    print(json.dumps(feature_drift(bars[bars.security_id == args.security_id],
                                   actions[actions.security_id == args.security_id],
                                   args.decision_day), ensure_ascii=False, indent=2))
