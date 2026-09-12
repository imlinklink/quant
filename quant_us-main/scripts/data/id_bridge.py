#!/usr/bin/env python3
"""把 symbol 键的行情产物桥接到 security_id 键（技术设计 §2.2/§2.5）。

现有下载/标准化产物以 `stock`/`code`（symbol）为主键；新链路（双价格视图、universe v2、
正式 manifest）以 `security_id` 为主键。本模块按 `symbol_history` 的**当时有效区间**做映射，
不覆盖行情本身；映射不到或歧义的记录单独输出，不静默丢弃。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

LIQUIDITY_RENAME = {'previous_close': 'previous_raw_close', 'adv20': 'adv20_usd'}


def attach_security_id(frame: pd.DataFrame, symbols: pd.DataFrame, *,
                       symbol_col: str, date_col: str) -> tuple:
    """按 (symbol, 日期) 在 symbol_history 中定位 security_id。

    返回 (mapped, unmapped, ambiguous)。`ambiguous` 指同一行同时命中两个 security_id
    （ticker 复用时间窗重叠），必须人工处理，不得任选。
    """
    if symbol_col not in frame.columns:
        raise ValueError(f'缺 symbol 列: {symbol_col}')
    if not {'security_id', 'symbol', 'valid_from', 'valid_to'}.issubset(symbols.columns):
        raise ValueError('symbol_history 缺 security_id/symbol/valid_from/valid_to')
    d = frame.copy().reset_index(drop=True)
    d['_row'] = d.index
    d['_date'] = pd.to_datetime(d[date_col], errors='coerce').dt.normalize()
    s = symbols.copy()
    s['_from'] = pd.to_datetime(s['valid_from'], errors='coerce').dt.normalize()
    s['_to'] = pd.to_datetime(s['valid_to'], errors='coerce').dt.normalize()
    merged = d.merge(s[['security_id', 'symbol', '_from', '_to']],
                     left_on=symbol_col, right_on='symbol', how='left')
    hit = merged[merged['_from'].notna() & (merged['_from'] <= merged['_date']) &
                 (merged['_to'].isna() | (merged['_date'] < merged['_to']))]
    counts = hit.groupby('_row')['security_id'].nunique()
    ambiguous_rows = set(counts[counts > 1].index)
    resolved = hit[~hit['_row'].isin(ambiguous_rows)][['_row', 'security_id']]
    out = d.merge(resolved, on='_row', how='left')
    ambiguous = out[out['_row'].isin(ambiguous_rows)].drop(columns=['_date'])
    unmapped = out[out['security_id'].isna() & ~out['_row'].isin(ambiguous_rows)].drop(columns=['_date'])
    mapped = out[out['security_id'].notna()].drop(columns=['_row', '_date'])
    return mapped, unmapped, ambiguous


def bridge_run(run_dir, symbols_path, output_dir) -> dict:
    """把某 run 的 daily.csv.gz / daily_liquidity.csv.gz 重键为 security_id 并写出。"""
    run_dir = Path(run_dir)
    symbols = pd.read_csv(symbols_path)
    out = Path(output_dir)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f'输出目录非空，禁止覆盖: {out}')
    out.mkdir(parents=True, exist_ok=True)

    daily = pd.read_csv(run_dir / 'daily.csv.gz')
    daily_v2, daily_unmapped, daily_amb = attach_security_id(
        daily, symbols, symbol_col='stock', date_col='date')
    daily_v2 = daily_v2.rename(columns={'date': 'session'}).sort_values(['security_id', 'session'])
    daily_v2.to_csv(out / 'daily_v2.csv.gz', index=False)

    liquidity = pd.read_csv(run_dir / 'daily_liquidity.csv.gz')
    liq_v2, liq_unmapped, liq_amb = attach_security_id(
        liquidity, symbols, symbol_col='code', date_col='date')
    liq_v2 = liq_v2.rename(columns=LIQUIDITY_RENAME).sort_values(['security_id', 'date'])
    liq_v2.to_csv(out / 'daily_liquidity_v2.csv.gz', index=False)

    report = {
        'daily_rows': int(len(daily)), 'daily_mapped': int(len(daily_v2)),
        'daily_unmapped': int(len(daily_unmapped)), 'daily_ambiguous': int(len(daily_amb)),
        'liquidity_rows': int(len(liquidity)), 'liquidity_mapped': int(len(liq_v2)),
        'liquidity_unmapped': int(len(liq_unmapped)), 'liquidity_ambiguous': int(len(liq_amb)),
        'securities': int(daily_v2['security_id'].nunique()) if not daily_v2.empty else 0,
    }
    (out / 'bridge_report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for name, frame in (('daily_unmapped.csv', daily_unmapped),
                        ('liquidity_unmapped.csv', liq_unmapped)):
        if not frame.empty:
            frame.drop(columns=['_row'], errors='ignore').to_csv(out / name, index=False)
    return report
