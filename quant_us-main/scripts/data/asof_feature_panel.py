#!/usr/bin/env python3
"""逐日 as-of 特征面板 + 原始执行价（技术设计 §2.4；交接方案 §5 M2）。

要点：
- **特征价**：对每个交易日 d，只用 `ex_date <= d` 的公司行动构造 as-of 序列，再算均线/ATR。
  as-of 序列锚定在 d，故 `asof_close(d) == raw_close(d)`——由它导出的止损/门槛**本身就是原始价尺度**。
- **执行价**：一律取**不复权**原始价（entry=T+1 原始开盘，exit=原始止损/开盘/收盘）。
- **跨行动日**：若行动 `ex_date` 恰好是下一交易日，则 d 日定义的价格水平须乘以 `scale_to_next`
  才能与 T+1 的原始价比较（否则除权跳空会被误判为高开/跌破止损）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.data.price_views import ADJUSTABLE_ACTIONS, action_factor

MA_WINDOWS = (20, 50, 200)
ATR_PERIOD = 14


def _factors_by_ex_date(actions: pd.DataFrame, raw: pd.DataFrame) -> dict:
    """每个 ex_date 对应 (作用于该日之前 bar 的因子)。

    只保留**落在价格窗口内、且有前收可算**的行动：窗口外的行动不影响本段序列；
    缺少除权前收盘的股息无法换算，直接跳过（由调用方/审计另行标注）。
    """
    out = {}
    if actions is None or actions.empty:
        return out
    first = pd.Timestamp(raw['session'].min())
    last = pd.Timestamp(raw['session'].max())
    use = actions[actions['action_type'].astype(str).str.lower().isin(ADJUSTABLE_ACTIONS)]
    for record in use.to_dict('records'):
        ex = pd.Timestamp(record['ex_date']).normalize()
        if not (first <= ex <= last):
            continue                                   # 窗口外的行动与本段无关
        try:
            factor = action_factor(record, raw)
        except ValueError:
            continue                                   # 缺前收（如股息）→ 跳过，另行标注
        out.setdefault(ex, []).append(factor)
    return out


def build_asof_panel(raw: pd.DataFrame, actions: pd.DataFrame, *,
                     ma_windows=MA_WINDOWS, atr_period=ATR_PERIOD) -> pd.DataFrame:
    """输入单只标的的不复权日线，输出逐日面板（原始价 + as-of 特征 + 跨日因子）。

    raw 需含 session/open/high/low/close/volume，按 session 升序。
    """
    d = raw.copy()
    d['session'] = pd.to_datetime(d['session']).dt.normalize()
    d = d.sort_values('session').reset_index(drop=True)
    n = len(d)
    raw_close = d['close'].astype(float).to_numpy()
    raw_high = d['high'].astype(float).to_numpy()
    raw_low = d['low'].astype(float).to_numpy()
    factor = np.ones(n)                                   # as-of 复权因子（锚定当日=1）
    by_ex = _factors_by_ex_date(actions, d)

    rows = []
    for i in range(n):
        session = d['session'].iloc[i]
        for f in by_ex.get(session, []):                  # 该日生效的行动：缩放"该日之前"的 bar
            factor[:i] *= f
        view_close = raw_close[:i + 1] * factor[:i + 1]
        view_high = raw_high[:i + 1] * factor[:i + 1]
        view_low = raw_low[:i + 1] * factor[:i + 1]
        row = {'session': session, 'raw_open': d['open'].iloc[i], 'raw_high': raw_high[i],
               'raw_low': raw_low[i], 'raw_close': raw_close[i], 'volume': d['volume'].iloc[i],
               'asof_close': view_close[-1]}
        for w in ma_windows:
            row[f'asof_ma{w}'] = float(view_close[-w:].mean()) if len(view_close) >= w else np.nan
        if len(view_close) >= atr_period + 1:
            prev = view_close[:-1]
            tr = np.maximum(view_high[1:] - view_low[1:],
                            np.maximum(np.abs(view_high[1:] - prev), np.abs(view_low[1:] - prev)))
            row['asof_atr'] = float(tr[-atr_period:].mean())
        else:
            row['asof_atr'] = np.nan
        # 下一交易日的行动因子：把 d 日水平换算到 T+1 原始价尺度
        nxt = d['session'].iloc[i + 1] if i + 1 < n else None
        scale = 1.0
        for f in by_ex.get(nxt, []) if nxt is not None else []:
            scale *= f
        row['scale_to_next'] = scale
        rows.append(row)
    return pd.DataFrame(rows)


if __name__ == '__main__':
    import argparse, json, sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[2]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.data.io_utils import read_frame, write_frame
    p = argparse.ArgumentParser(description='构造逐日 as-of 特征面板（单只）')
    p.add_argument('--raw', required=True, help='不复权日线（security_id/session/OHLCV）')
    p.add_argument('--actions', help='公司行动表')
    p.add_argument('--security-id', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    raw = read_frame(args.raw); raw = raw[raw.security_id == args.security_id]
    actions = read_frame(args.actions) if args.actions else pd.DataFrame()
    actions = actions[actions.security_id == args.security_id] if not actions.empty else actions
    panel = build_asof_panel(raw, actions)
    write_frame(panel, args.output)
    print(json.dumps({'security_id': args.security_id, 'rows': int(len(panel)),
                      'anomalies_scale_to_next': int((panel.scale_to_next != 1).sum())},
                     ensure_ascii=False))
