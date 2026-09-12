#!/usr/bin/env python3
"""富途财报 → 诊断层证据（技术设计 §3.1/§3.3）。

富途只提供财报**发布日期**与盘前/盘后标记，**不提供 `observed_at`**（历史首次可查询时间）。
因此本适配器产出的证据 `observed_at` 一律留空，只能进**诊断层**（严格层会记
`OBSERVED_AT_UNPROVEN`），不得当作"当时已获得"。发布时间取保守口径（盘前/盘中=09:30 ET，
盘后/未知=16:00 ET），保证不会早于真实可见时刻。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.data.import_security_master_v2_from_futu import security_id_for
from scripts.evidence.evidence_store import market_time

SOURCE_ID = 'futu_earnings_price_move'
BEFORE_TYPES = ('BEFORE', 'DURING_MARKET', 'PRE_MARKET')


def _published_at(pub_date, pub_type):
    """保守发布时间：盘前/盘中取开盘，盘后/未知取收盘（宁晚不早）。"""
    hour, minute = (9, 30) if str(pub_type) in BEFORE_TYPES else (16, 0)
    return market_time(pub_date, hour, minute).isoformat()


def evidence_rows_from_price_move(code, frame: pd.DataFrame) -> list:
    """把某标的的财报-价格表转成诊断层证据行（每期一条）。"""
    if frame is None or frame.empty or 'pub_trading_day_str' not in frame:
        return []
    unique = frame.drop_duplicates('pub_trading_day_str')
    rows = []
    for item in unique.to_dict('records'):
        pub_date = str(item['pub_trading_day_str'])[:10]
        if not pub_date or pub_date == 'nan':
            continue
        rows.append({
            'security_id': security_id_for(code),
            'symbol_as_published': code,
            'kind': 'earnings',
            'source_id': SOURCE_ID,
            'source_record_id': f"{code}|{pub_date}",
            'source_url_or_archive_path': 'futu://earnings_price_move',
            'event_at': pub_date,
            'published_at': _published_at(pub_date, item.get('pub_type')),
            'observed_at': None,                       # 不可证 → 诊断层
            'ingested_at': datetime.now(timezone.utc).isoformat(),
            'version_id': 'v1',
            'supersedes_id': None,
            'content_hash': f"{code}|{pub_date}|{item.get('pub_type')}|{item.get('period_text')}",
            'summary_hash': f"{item.get('period_text')}",
            'quality_status': 'unverified',
            'availability_proof': 'futu earnings calendar: publication date only, no first-observed timestamp',
            'license_tag': 'research-use-only',
        })
    return rows


def main():
    p = argparse.ArgumentParser(description='富途财报 -> 诊断层证据 evidence.csv')
    p.add_argument('--codes', nargs='+', required=True)
    p.add_argument('--host', default='127.0.0.1'); p.add_argument('--port', type=int, default=11111)
    p.add_argument('--period-count', type=int, default=50)
    p.add_argument('--output-dir', required=True)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    if args.dry_run:
        print(json.dumps({'dry_run': True, 'codes': len(args.codes)}, ensure_ascii=False)); return 0
    import futu
    ctx = futu.OpenQuoteContext(host=args.host, port=args.port)
    rows = []; per_code = {}
    try:
        for code in args.codes:
            ret, data = ctx.get_financials_earnings_price_move(code, period_count=args.period_count)
            got = evidence_rows_from_price_move(code, data) if ret == 0 else []
            per_code[code] = len(got); rows += got
    finally:
        ctx.close()
    out = Path(args.output_dir)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'输出目录非空，禁止覆盖: {out}')
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out / 'evidence.csv', index=False)
    summary = {'rows': len(rows), 'codes': len(args.codes), 'per_code': per_code,
               'observed_at': 'all empty (OBSERVED_AT_UNPROVEN) -> diagnostic layer only'}
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n',
                                      encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ('rows', 'codes')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
