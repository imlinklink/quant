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
import time
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


def _fetch(call, code, retries, pause, sleep=time.sleep):
    """调一次接口，非 0 返回码按退避重试。返回最后一次的 `(ret, data)`。

    **为什么必须重试而不是直接当空**：富途这个接口会限流。2026-09-20 实测，连着打
    39 只（78 次调用）时从第 23 只起全部返回空，而把同样的 16 只单独查（间隔 1 秒）
    全部拿得到数据（XOM 100 条分红、SHW 2 条拆股）。所以"返回空"既可能是真的没有
    行动，也可能是一次被限流的失败 —— 两者的**下游表现完全一样**，必须在这里分开。
    """
    ret, data = None, None
    for attempt in range(retries + 1):
        ret, data = call(code)
        if ret == 0:
            return ret, data
        if attempt < retries:
            sleep(pause * (attempt + 2))
    return ret, data


def collect_actions(ctx, codes, retries=3, pause=1.0, sleep=time.sleep):
    """逐只取拆股与分红。返回 `(splits, dividends, raw, failures)`。

    单独抽出来是为了**可测**：这段的全部风险都在"取数失败"这一条路径上，而它原先埋在
    `main()` 里、只有真连富途才走得到 —— 于是非 0 返回码被静默写成"没有公司行动"，
    一次限流产出只有 23/39 只的表，而 `summary.json` 里只有一个正常的行数。
    """
    splits, dividends, raw, failures = {}, {}, {}, {}
    for code in codes:
        got = {}
        for kind, call, key in (
                ('splits', ctx.get_corporate_actions_stock_splits, 'split_list'),
                ('dividends', ctx.get_corporate_actions_dividends, 'dividend_list')):
            ret, data = _fetch(call, code, retries, pause, sleep=sleep)
            if ret != 0:
                # **失败绝不写成"没有公司行动"**：那会让一次限流看起来像
                # "这家公司本来就无分红无拆股"，产出一份看起来正常的错表。
                failures.setdefault(code, {})[kind] = f'{ret}: {str(data)[:200]}'
                got[kind] = []
            else:
                got[kind] = (data or {}).get(key, []) or []
            sleep(pause)
        splits[code], dividends[code] = got['splits'], got['dividends']
        raw[code] = {'splits': got['splits'], 'dividends': got['dividends']}
    return splits, dividends, raw, failures


def main():
    p = argparse.ArgumentParser(description='从富途直接导入公司行动')
    p.add_argument('--codes', nargs='+', required=True, help='如 US.NVDA US.AAPL')
    p.add_argument('--host', default='127.0.0.1'); p.add_argument('--port', type=int, default=11111)
    p.add_argument('--run-id', default='RUN-001'); p.add_argument('--archive-root', default='data/source_archive')
    p.add_argument('--output-dir', required=True)
    p.add_argument('--pause', type=float, default=1.0, help='每次调用之间的间隔秒数（规避限流）')
    p.add_argument('--retries', type=int, default=3)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    if args.dry_run:
        print(json.dumps({'dry_run': True, 'codes': args.codes}, ensure_ascii=False))
        return 0

    import futu
    from scripts.data.io_utils import write_frame
    ctx = futu.OpenQuoteContext(host=args.host, port=args.port)
    try:
        splits, dividends, raw, failures = collect_actions(
            ctx, args.codes, retries=args.retries, pause=args.pause)
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
    empty = sorted(c for c, v in raw.items() if not v['splits'] and not v['dividends'])
    summary = {'rows': int(len(actions)), 'codes': len(args.codes),
               'by_type': actions['action_type'].value_counts().to_dict() if not actions.empty else {},
               # 零行动**必须列名**：它既可能是真的，也可能是没抓到 —— 把名单摊开，
               # 读的人才能去核对，而不是相信一个总数。
               'codes_with_no_actions': empty,
               'fetch_failures': failures,
               'note': 'quality_status=unverified：富途无 observed_at'}
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n',
                                      encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    if failures:
        print(f'❌ {len(failures)} 只取数失败，结果不完整 —— 不要把它当成"这些公司没有行动"',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
