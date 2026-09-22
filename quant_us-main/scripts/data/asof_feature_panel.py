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


def _affects_segment(ex: pd.Timestamp, first: pd.Timestamp, last: pd.Timestamp) -> bool:
    """该行动是否作用于本段的**任何一根 bar**。

    必须**严格晚于**段首日：`ex == first` 时因子只会被乘到空切片 `raw[:0]`
    （`build_asof_panel` 里 `factor[:i] *= f` 而 `i=0`），`scale_to_next` 也只会读
    `by_ex[nxt]`、而 `nxt` 不可能是段首日 ⇒ **对本段的每一行都没有影响**。
    但旧写法会去要一段根本不存在的"除权前收盘"而抛 `ACTION_FACTOR_UNRESOLVED`。

    实测（2026-09-22）：`SEC-US-JPM` 的 2015-01-02 股息正好落在段首日，整批 32 只的
    前向面板刷新因此中止；而库里已有的 JPM 面板本就是**没有应用该因子**算出来的
    （剔除该条后重建，历史段逐格差 ≤5.7e-14，远小于 `HISTORY_TOL=1e-9`）⇒ 旧行为
    唯一的效果是让这个面板**永远无法重建**（三臂前向时钟随之停摆）。

    另注：对股息而言 `ACTION_FACTOR_UNRESOLVED` 只可能在 `ex == first` 时触发
    （窗口判据已保证 `ex >= first`），即只会在本函数认定的这种无影响情形下触发。

    `generate_historical_setups._raw_asof_snapshots` 早有同一条规则（它把
    `build_price_view` 的输入按 `ex > 段首日` 过滤），此处是把它收敛成一份定义。
    """
    return first < ex <= last


def _factors_by_ex_date(actions: pd.DataFrame, raw: pd.DataFrame) -> dict:
    """每个 ex_date 对应 (作用于该日之前 bar 的因子)。

    只保留**作用于本段 bar** 的行动（判据见 `_affects_segment`）。段内缺前收的股息
    仍然阻断该面板（`DIVIDEND_INVALID` 等），不默默保留错误特征。
    """
    out = {}
    if actions is None or actions.empty:
        return out
    first = pd.Timestamp(raw['session'].min())
    last = pd.Timestamp(raw['session'].max())
    unsupported = actions[actions['action_type'].astype(str).str.lower().eq('merger')]
    for record in unsupported.to_dict('records'):
        ex = pd.Timestamp(record['ex_date']).normalize()
        if _affects_segment(ex, first, last):
            raise ValueError(f'ACTION_TYPE_UNSUPPORTED:{record.get("security_id")}:{ex.date()}')
    use = actions[actions['action_type'].astype(str).str.lower().isin(ADJUSTABLE_ACTIONS)]
    for record in use.to_dict('records'):
        ex = pd.Timestamp(record['ex_date']).normalize()
        if not _affects_segment(ex, first, last):
            continue                                   # 窗口外/段首日的行动不影响本段
        try:
            factor = action_factor(record, raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(f'ACTION_FACTOR_UNRESOLVED:{record.get("security_id")}:{ex.date()}') from exc
        if not np.isfinite(factor) or factor <= 0:
            raise ValueError(f'ACTION_FACTOR_INVALID:{record.get("security_id")}:{ex.date()}')
        out.setdefault(ex, []).append(factor)
    return out


def build_asof_panel(raw: pd.DataFrame, actions: pd.DataFrame, *,
                     ma_windows=MA_WINDOWS, atr_period=ATR_PERIOD) -> pd.DataFrame:
    """输入单只标的的不复权日线，输出逐日面板（原始价 + as-of 特征 + 跨日因子）。

    raw 需含 session/open/high/low/close/volume，按 session 升序。
    """
    d = raw.copy()
    if 'security_id' in d.columns:
        ids = d['security_id'].dropna().astype(str).unique()
        if len(ids) != 1:
            raise ValueError('ASOF_PANEL_REQUIRES_ONE_SECURITY')
        if actions is not None and not actions.empty:
            if 'security_id' not in actions.columns:
                raise ValueError('ACTION_SECURITY_ID_MISSING')
            actions = actions[actions['security_id'].astype(str) == ids[0]]
    elif actions is not None and not actions.empty and 'security_id' in actions.columns:
        raise ValueError('RAW_SECURITY_ID_MISSING')
    d['session'] = pd.to_datetime(d['session']).dt.normalize()
    d = d.sort_values('session').reset_index(drop=True)
    if d.empty:
        raise ValueError('RAW_BARS_EMPTY')
    if d['session'].duplicated().any():
        raise ValueError('DUPLICATE_RAW_SESSION')
    prices = d[['open', 'high', 'low', 'close']].astype(float).to_numpy()
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise ValueError('INVALID_RAW_PRICE')
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
