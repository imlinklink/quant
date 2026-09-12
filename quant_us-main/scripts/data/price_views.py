#!/usr/bin/env python3
"""价格视图：原始可交易价与 as-of 特征价。

对应技术设计 §2.4：
- **原始可交易价**（`raw`）：用于 T+1 开盘成交、止损、价格门与费用；不得用未来拆股后的
  前复权标价冒充当时可成交价格。
- **研究特征价**（`asof_adjusted`）：用于均线/回撤/ATR 等；只应用 `ex_date <= as_of`
  的公司行动，特征计算不得应用未来生效行动。
每条记录写入 `price_basis`、`adjustment_as_of`、`action_version`。
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

PRICE_BASES = ('raw', 'asof_adjusted')
# 会改变价格连续性的行动，可用 ratio/cash 直接复权。
ADJUSTABLE_ACTIONS = ('split', 'reverse_split', 'cash_dividend', 'spinoff')
# 并购/分拆若无法核实价格衔接，标 corporate_action_unresolved 并排除该证券绩效。
UNRESOLVED_ACTIONS = ('merger', 'spinoff')
BAR_COLUMNS = ('security_id', 'session', 'open', 'high', 'low', 'close', 'volume')


def action_version(actions: pd.DataFrame) -> str:
    """公司行动集合的稳定版本哈希；集合变化即变化。"""
    if actions is None or actions.empty:
        return hashlib.sha256(b'[]').hexdigest()
    records = actions.copy()
    records['ex_date'] = pd.to_datetime(records['ex_date']).dt.strftime('%Y-%m-%d')
    keys = ['security_id', 'action_type', 'ex_date', 'ratio', 'cash_amount']
    rows = sorted(tuple(str(r.get(k, '')) for k in keys) for r in records.to_dict('records'))
    blob = json.dumps(rows, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def _prev_close(bars: pd.DataFrame, ex_date) -> float:
    prior = bars[pd.to_datetime(bars['session']) < pd.Timestamp(ex_date)]
    if prior.empty:
        raise ValueError(f'MISSING_PREV_CLOSE_FOR_DIVIDEND:{pd.Timestamp(ex_date).date()}')
    return float(prior.sort_values('session')['close'].iloc[-1])


def action_factor(action: dict, bars: pd.DataFrame) -> float:
    """价格在 ex_date 之前应乘的因子（正向复权）。"""
    kind = str(action['action_type']).lower()
    if kind in ('split', 'reverse_split'):
        ratio = float(action['ratio'])
        if ratio <= 0:
            raise ValueError('SPLIT_RATIO_INVALID')
        return 1.0 / ratio
    if kind == 'cash_dividend':
        cash = float(action['cash_amount'])
        prev = _prev_close(bars, action['ex_date'])
        if cash < 0 or cash >= prev:
            raise ValueError('DIVIDEND_INVALID')
        return 1.0 - cash / prev
    if kind == 'spinoff':
        ratio = float(action['ratio'])
        if not 0.0 <= ratio < 1.0:
            raise ValueError('SPINOFF_RATIO_INVALID')
        return 1.0 - ratio
    raise ValueError(f'NOT_ADJUSTABLE_ACTION:{kind}')


def _prepare_bars(bars: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in BAR_COLUMNS if c not in bars.columns]
    if missing:
        raise ValueError('日线缺字段: ' + ','.join(sorted(missing)))
    d = bars.copy()
    d['session'] = pd.to_datetime(d['session']).dt.normalize()
    return d.sort_values(['security_id', 'session']).reset_index(drop=True)


def build_price_view(bars: pd.DataFrame, actions: pd.DataFrame, *, price_basis: str,
                     as_of=None, security_id=None) -> pd.DataFrame:
    """构造一条价格视图。raw 不做调整；asof_adjusted 只应用 ex_date<=as_of 的行动。"""
    if price_basis not in PRICE_BASES:
        raise ValueError(f'未知 price_basis: {price_basis}')
    d = _prepare_bars(bars)
    if security_id is not None:
        d = d[d['security_id'] == security_id].reset_index(drop=True)
    actions = actions if actions is not None else pd.DataFrame(
        columns=['security_id', 'action_type', 'ex_date', 'ratio', 'cash_amount'])
    if price_basis == 'raw':
        view = d.copy()
        view['price_basis'] = 'raw'
        view['adjustment_as_of'] = None
        view['action_version'] = action_version(pd.DataFrame())
        return view

    as_of = pd.Timestamp(as_of).normalize() if as_of is not None else None
    applied = actions[actions['action_type'].astype(str).str.lower()
                      .isin(ADJUSTABLE_ACTIONS)].copy()
    if as_of is not None:
        applied = applied[pd.to_datetime(applied['ex_date']) <= as_of]
    version = action_version(applied)

    factors = pd.Series(1.0, index=d.index)
    for sec, group in d.groupby('security_id'):
        sec_actions = applied[applied['security_id'] == sec]
        for action in sec_actions.to_dict('records'):
            factor = action_factor(action, group)
            ex = pd.Timestamp(action['ex_date'])
            factors.loc[group.index[group['session'] < ex]] *= factor
    view = d.copy()
    for column in ('open', 'high', 'low', 'close'):
        view[column] = view[column].astype(float) * factors
    view['volume'] = view['volume'].astype(float) / factors
    view['price_basis'] = 'asof_adjusted'
    view['adjustment_as_of'] = None if as_of is None else as_of.strftime('%Y-%m-%d')
    view['action_version'] = version
    return view


def build_price_views(bars: pd.DataFrame, actions: pd.DataFrame, *, as_of=None) -> pd.DataFrame:
    """同时产出 raw 与 asof_adjusted 两条视图。"""
    return pd.concat([build_price_view(bars, actions, price_basis='raw'),
                      build_price_view(bars, actions, price_basis='asof_adjusted', as_of=as_of)],
                     ignore_index=True)


def corporate_action_flags(bars: pd.DataFrame, master: pd.DataFrame, actions: pd.DataFrame,
                           unresolved=()) -> pd.DataFrame:
    """标记样本期内价格无法正确衔接的证券（并购/分拆未核实），供排除绩效使用。

    固定存续样本不开发退市结算；但不得把无法衔接的最后一根 K 线当作正常退出。
    """
    d = _prepare_bars(bars)
    unresolved = set(map(str, unresolved))
    if actions is not None and not actions.empty:
        special = actions[actions['action_type'].astype(str).str.lower().isin(UNRESOLVED_ACTIONS)]
        for record in special.to_dict('records'):
            ratio = pd.to_numeric(record.get('ratio'), errors='coerce')
            cash = pd.to_numeric(record.get('cash_amount'), errors='coerce')
            if not ((pd.notna(ratio) and float(ratio) > 0) or (pd.notna(cash) and float(cash) > 0)):
                unresolved.add(str(record.get('security_id')))
    rows = []
    for sec in master['security_id']:
        sec = str(sec)
        last_bar = d.loc[d['security_id'] == sec, 'session'].max()
        problems = []
        if pd.isna(last_bar):
            problems.append('NO_BARS')
        if sec in unresolved:
            problems.append('corporate_action_unresolved')
        rows.append({'security_id': sec,
                     'last_bar': None if pd.isna(last_bar) else str(pd.Timestamp(last_bar).date()),
                     'problems': ';'.join(problems)})
    return pd.DataFrame(rows, columns=['security_id', 'last_bar', 'problems'])
