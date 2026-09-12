#!/usr/bin/env python3
"""从富途公司行动接口直接导入 corporate_actions（技术设计 §2.2/§2.3）。

优于「QFQ 与不复权价差反推」：直接给出拆合股比率与分红金额，并带发布日期。
仍不提供 `observed_at`（历史首次可见时间），故一律标 `quality_status=unverified`。
真实抓取需本机 OpenD；纯转换函数用本地 fixture 测试。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.import_security_master_v2_from_futu import security_id_for
from scripts.data.security_master_v2 import ACTION_COLUMNS, normalize_actions

SPLIT_SOURCE = 'futu_corporate_actions_splits'
DIVIDEND_SOURCE = 'futu_corporate_actions_dividends'


def _date(value):
    """把 unix 秒或 'MM/DD/YYYY' 统一成 'YYYY-MM-DD'。"""
    if value is None or str(value).strip() == '':
        return None
    text = str(value).strip()
    if re.fullmatch(r'\d{10}', text):
        return datetime.fromtimestamp(int(text), tz=timezone.utc).strftime('%Y-%m-%d')
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.strptime(text, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


def ratio_from_rate(rate):
    """'1→10' -> 10.0；'2→3' -> 1.5。"""
    match = re.match(r'\s*([0-9.]+)\s*[→\-:]\s*([0-9.]+)\s*', str(rate))
    if not match:
        return None
    old, new = float(match.group(1)), float(match.group(2))
    return new / old if old else None


def cash_from_statement(statement):
    """'Cash Dividend: 0.27 USD Per Share' -> 0.27。"""
    match = re.search(r'([0-9]*\.?[0-9]+)\s*USD', str(statement))
    return float(match.group(1)) if match else None


def actions_from_splits(code, split_list) -> list:
    rows = []
    for item in split_list or []:
        ex_date = _date(item.get('dir_deci_pub_date_str') or item.get('dir_deci_pub_date'))
        ratio = ratio_from_rate(item.get('rate'))
        if ex_date is None or ratio is None:
            continue
        rows.append({'security_id': security_id_for(code), 'action_type': 'split',
                     'ex_date': ex_date, 'effective_at': None, 'ratio': ratio, 'cash_amount': 0.0,
                     'source_id': SPLIT_SOURCE, 'source_record_id': f"{code}|{ex_date}|{item.get('rate')}",
                     'source_published_at': None, 'source_observed_at': None})
    return rows


def actions_from_dividends(code, dividend_list) -> list:
    rows = []
    for item in dividend_list or []:
        ex_date = _date(item.get('ex_date'))
        cash = cash_from_statement(item.get('statement'))
        if ex_date is None or cash is None:
            continue
        rows.append({'security_id': security_id_for(code), 'action_type': 'cash_dividend',
                     'ex_date': ex_date, 'effective_at': None, 'ratio': 0.0, 'cash_amount': cash,
                     'source_id': DIVIDEND_SOURCE, 'source_record_id': f"{code}|{ex_date}|{cash}",
                     'source_published_at': _date(item.get('pub_date')), 'source_observed_at': None})
    return rows


def _normalize(rows, source_id) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=list(ACTION_COLUMNS))
    # 富途不提供 observed_at；不写 source_observed_at，也不伪造"当时已获得"。
    return normalize_actions(pd.DataFrame(rows), source_id)


def build_actions(split_frames: dict, dividend_frames: dict) -> pd.DataFrame:
    """split_frames/dividend_frames: {code: api_list}。两类来源分别规范化，避免 source_id 串号。"""
    split_rows, dividend_rows = [], []
    for code, lst in (split_frames or {}).items():
        split_rows += actions_from_splits(code, lst)
    for code, lst in (dividend_frames or {}).items():
        dividend_rows += actions_from_dividends(code, lst)
    frames = [f for f in (_normalize(split_rows, SPLIT_SOURCE),
                          _normalize(dividend_rows, DIVIDEND_SOURCE)) if not f.empty]
    if not frames:
        return pd.DataFrame(columns=list(ACTION_COLUMNS))
    return (pd.concat(frames, ignore_index=True)[list(ACTION_COLUMNS)]
            .sort_values(['security_id', 'ex_date']).reset_index(drop=True))


def main():
    p = argparse.ArgumentParser(description='从富途直接导入公司行动')
    p.add_argument('--codes', nargs='+', required=True, help='如 US.NVDA US.AAPL')
    p.add_argument('--host', default='127.0.0.1'); p.add_argument('--port', type=int, default=11111)
    p.add_argument('--run-id', default='RUN-001'); p.add_argument('--archive-root', default='data/source_archive')
    p.add_argument('--output-dir', required=True)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    if args.dry_run:
        print(json.dumps({'dry_run': True, 'codes': args.codes}, ensure_ascii=False))
        return 0

    import futu
    from scripts.data.io_utils import write_frame
    ctx = futu.OpenQuoteContext(host=args.host, port=args.port)
    splits, dividends, raw = {}, {}, {}
    try:
        for code in args.codes:
            ret, data = ctx.get_corporate_actions_stock_splits(code)
            splits[code] = (data or {}).get('split_list', []) if ret == 0 else []
            ret, data = ctx.get_corporate_actions_dividends(code)
            dividends[code] = (data or {}).get('dividend_list', []) if ret == 0 else []
            raw[code] = {'splits': splits[code], 'dividends': dividends[code]}
    finally:
        ctx.close()

    out = Path(args.output_dir)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'输出目录非空，禁止覆盖: {out}')
    raw_dir = out / '_raw'; raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / 'corporate_actions_raw.json').write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    out.mkdir(parents=True, exist_ok=True)
    actions = build_actions(splits, dividends)
    write_frame(actions, out / 'corporate_actions.csv')
    summary = {'rows': int(len(actions)), 'codes': len(args.codes),
               'by_type': actions['action_type'].value_counts().to_dict() if not actions.empty else {},
               'note': 'quality_status=unverified：富途无 observed_at'}
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n',
                                      encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
