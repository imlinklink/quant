#!/usr/bin/env python3
"""从富途当前证券目录生成 v2 证券主数据与 ticker 历史。

范围（使用者决定 2026-09-12）：**仅当前存续证券，不含退市样本**。因此
`delisted_at` 一律留空，结论须标注幸存者偏差。公司行动可由 QFQ 与不复权价差反推
（`derive_corporate_actions`），标记 `unverified`。真实抓取需本机 OpenD；
纯转换函数用本地 fixture 测试。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.build_security_master_from_futu import (DEFAULT_LEVERAGED, _asset_type,
                                                          codes_from_config, normalize_code)
from scripts.data.security_master_v2 import normalize_master, normalize_symbols

SOURCE_ID = 'futu_basicinfo'


def security_id_for(code) -> str:
    """确定性证券 ID。富途无 ticker 变更映射，故按代码生成；改名只会表现为新证券。"""
    return 'SEC-' + normalize_code(code).replace('.', '-')


def master_from_basicinfo(frame: pd.DataFrame, codes, *, as_of=None, leveraged=(),
                          ingested_at=None, allow_missing=False):
    """把富途基本资料转成 (security_master_v2, symbol_history) 两张规范表。"""
    if 'code' not in frame:
        raise ValueError('Futu basicinfo 缺少 code 字段')
    d = frame.copy(); d['code'] = d.code.map(normalize_code)
    wanted = set(map(normalize_code, codes))
    d = d[d.code.isin(wanted)].sort_values('code').drop_duplicates('code', keep='last')
    missing = sorted(wanted - set(d.code))
    if missing and not allow_missing:
        raise ValueError('Futu未返回证券: ' + ','.join(missing))
    lev = {normalize_code(c) for c in set(leveraged) | DEFAULT_LEVERAGED}
    as_of = as_of or date.today().isoformat()
    ingested = ingested_at or datetime.now(timezone.utc).isoformat()
    listing_src = (d['listing_date'] if 'listing_date' in d
                   else d.get('list_time', pd.Series(index=d.index, dtype=object)))
    listing = pd.to_datetime(listing_src, errors='coerce')

    master_rows, symbol_rows = [], []
    for row, listed in zip(d.to_dict('records'), listing):
        code = row['code']
        valid = '' if (pd.isna(listed) or listed.year <= 1970) else listed.strftime('%Y-%m-%d')
        quality = 'missing_history' if not valid else 'unverified'  # observed_at 不可证 → unverified
        sec = security_id_for(code)
        exchange = str(row.get('exchange_type', row.get('market', '')) or '')
        master_rows.append({'security_id': sec, 'issuer_id': sec,
                            'asset_type': _asset_type(row, lev), 'exchange': exchange,
                            'currency': 'USD', 'valid_from': valid or as_of, 'valid_to': '',
                            'listed_at': valid, 'source_record_id': code,
                            'source_observed_at': '', 'quality_status': quality})
        symbol_rows.append({'security_id': sec, 'symbol': code, 'exchange': exchange,
                            'valid_from': valid or as_of, 'valid_to': '',
                            'source_record_id': code, 'quality_status': quality})
    master = normalize_master(pd.DataFrame(master_rows), SOURCE_ID, ingested_at=ingested)
    symbols = normalize_symbols(pd.DataFrame(symbol_rows), SOURCE_ID)
    return master, symbols


def actions_from_klines(pairs) -> pd.DataFrame:
    """pairs: [(security_id, raw_bars, adjusted_bars)] → 反推的 corporate_actions。"""
    from scripts.data.derive_corporate_actions import derive_actions
    frames = [derive_actions(raw, adj, security_id) for security_id, raw, adj in pairs]
    frames = [f for f in frames if not f.empty]
    return (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=['security_id', 'action_type', 'ex_date', 'ratio', 'cash_amount',
                 'source_id', 'quality_status']))


def main():
    p = argparse.ArgumentParser(description='从富途生成 v2 证券主数据与 ticker 历史')
    p.add_argument('--config', default=str(ROOT / 'config.yaml'))
    p.add_argument('--include', nargs='*', default=[])
    p.add_argument('--leveraged', nargs='*', default=[])
    p.add_argument('--host', default='127.0.0.1'); p.add_argument('--port', type=int, default=11111)
    p.add_argument('--run-id', default='RUN-001')
    p.add_argument('--master-version', default='futu-2026-09-12')
    p.add_argument('--archive-root', default='data/source_archive')
    p.add_argument('--output-dir', required=True)
    p.add_argument('--allow-missing', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    import yaml
    with open(args.config, encoding='utf-8') as fh:
        config = yaml.safe_load(fh) or {}
    codes = codes_from_config(config, args.include)
    if args.dry_run:
        print(json.dumps({'dry_run': True, 'codes': len(codes)}, ensure_ascii=False))
        return 0

    from scripts.data.build_security_master_from_futu import fetch_basicinfo
    from scripts.data.source_archive import archive_source
    from scripts.data.io_utils import write_frame
    from futu import OpenQuoteContext

    ctx = OpenQuoteContext(host=args.host, port=args.port)
    try:
        basic = fetch_basicinfo(ctx)
    finally:
        ctx.close()

    out = Path(args.output_dir)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'输出目录非空，禁止覆盖: {out}')
    out.mkdir(parents=True, exist_ok=True)
    # 归档原始响应，保证可追溯（§2.3）
    raw_dir = out / '_raw'; raw_dir.mkdir(exist_ok=True)
    basic.to_csv(raw_dir / 'security_master.csv', index=False)
    archive_source(raw_dir, SOURCE_ID, args.run_id, args.archive_root)

    master, symbols = master_from_basicinfo(basic, codes, leveraged=args.leveraged,
                                            allow_missing=args.allow_missing)
    write_frame(master, out / 'security_master_v2.csv')
    write_frame(symbols, out / 'symbol_history.csv')
    summary = {'master_rows': int(len(master)), 'symbol_rows': int(len(symbols)),
               'master_version': args.master_version, 'scope': 'current_survivors_only',
               'survivorship_bias': True}
    (out / 'import_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
