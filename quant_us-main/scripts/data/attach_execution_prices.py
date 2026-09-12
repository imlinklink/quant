#!/usr/bin/env python3
"""把 QFQ 口径的 setup 价格水平换算到**执行日原始价尺度**（交接方案 §5 M2）。

原理：同一标的在交易日 t 的 QFQ 与原始价只差一个尺度因子
    `conv(t) = raw_open(t) / qfq_open(t)`。换算在执行日开盘即可确定，不依赖当日收盘。因此：

- 部分同日尺度比值（如 ma/close、ATR/close）在一致换算下不变；这**不能证明**
  历史状态机整体不受全快照复权影响，尤其不能跳过逐日 as-of 特征重建；
- **价格水平**（初始止损、高开门 `signal_close + k·ATR`）必须换算到执行日原始尺度：
  `level_raw(执行日) = level_qfq(决策日) × conv(执行日)`。

拆股例：决策日 qfq 收盘 50、止损 45；执行日为拆股日（raw=qfq=50 → conv=1）→ raw 止损 45，
对应拆股前 90、与 qfq 口径的 90% 一致。
"""
from __future__ import annotations

import pandas as pd

LEVEL_FIELDS = ('signal_close', 'initial_stop', 'atr14')


def attach_execution_prices(setups: pd.DataFrame, qfq_daily: pd.DataFrame,
                            raw_daily: pd.DataFrame, *, entry_session_col='entry_session',
                            code_col='stock') -> pd.DataFrame:
    """为每个 setup 计算执行日尺度因子，并给出原始价口径的价格水平与入场价。

    需要的列：setup 的 `stock`、`entry_session`（执行交易日，通常 = next_open_time 的自然日），
    以及 qfq/raw 日线（stock/date/open）。本工具只为旧 QFQ setup 作迁移/差异诊断；
    新实验优先直接从逐日 as-of 面板生成 setup，不得把其水平再乘 QFQ 换算因子。
    """
    required = {'stock', entry_session_col, *LEVEL_FIELDS}
    missing = required - set(setups.columns)
    if missing:
        raise ValueError('setups 缺字段: ' + ','.join(sorted(missing)))
    for name, frame in (('QFQ', qfq_daily), ('RAW', raw_daily)):
        if not {code_col, 'date', 'open'}.issubset(frame.columns):
            raise ValueError(f'{name}_DAILY_MISSING_COLUMNS')
    qfq = qfq_daily.copy(); qfq['_d'] = pd.to_datetime(qfq['date']).dt.normalize()
    raw = raw_daily.copy(); raw['_d'] = pd.to_datetime(raw['date']).dt.normalize()
    if qfq.duplicated([code_col, '_d']).any() or raw.duplicated([code_col, '_d']).any():
        raise ValueError('DUPLICATE_DAILY_PRICE_KEY')
    qmap = qfq.set_index([code_col, '_d'])['open']
    rmap = raw.set_index([code_col, '_d'])['open']
    omap = rmap

    out = setups.copy()
    out['_entry'] = pd.to_datetime(out[entry_session_col]).dt.normalize()
    keys = list(zip(out[code_col], out['_entry']))

    def _factor(key):
        q = qmap.get(key); r = rmap.get(key)
        if q is None or r is None or pd.isna(q) or pd.isna(r):
            return None
        if float(q) <= 0 or float(r) <= 0:
            raise ValueError(f'INVALID_EXECUTION_OPEN:{key}')
        return float(r) / float(q)

    out['exec_conv'] = [_factor(k) for k in keys]
    for field in LEVEL_FIELDS:
        out[f'{field}_raw'] = [None if conv is None else float(v) * conv
                               for v, conv in zip(out[field], out['exec_conv'])]
    out['entry_price_raw'] = [omap.get(k) if omap is not None else None for k in keys]
    return out.drop(columns=['_entry'])


def main():
    import argparse, json, sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[2]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.data.io_utils import read_frame, write_frame
    p = argparse.ArgumentParser(description='为 setups 附加执行日原始价口径的价格水平')
    p.add_argument('--setups', required=True); p.add_argument('--qfq-daily', required=True)
    p.add_argument('--raw-daily', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    setups = read_frame(args.setups)
    if 'entry_session' not in setups.columns:
        setups['entry_session'] = pd.to_datetime(setups['next_open_time'], utc=True).dt.tz_convert(
            'America/New_York').dt.tz_localize(None).dt.normalize()
    out = attach_execution_prices(setups, read_frame(args.qfq_daily), read_frame(args.raw_daily))
    write_frame(out, args.output)
    conv = out['exec_conv'].dropna()
    print(json.dumps({'rows': int(len(out)), 'conv_ne_1': int((conv != 1).sum()),
                      'conv_missing': int(out['exec_conv'].isna().sum()), 'conv_max': float(conv.max()) if len(conv) else None},
                     ensure_ascii=False))


if __name__ == '__main__':
    raise SystemExit(main())
