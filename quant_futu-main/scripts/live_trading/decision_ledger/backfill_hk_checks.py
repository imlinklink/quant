"""港股扫描回填（评估闭环）：按检查时间补 1/3/5 个交易日后收益。

用法：
    python scripts/live_trading/decision_ledger/backfill_hk_checks.py
    python scripts/live_trading/decision_ledger/backfill_hk_checks.py --days 7 --codes HK.00700
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import yaml

BASE_DIR = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BASE_DIR))

from scripts.live_trading.decision_ledger import scan_ledger  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('backfill_hk_checks')


def _parse_dt(text) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(text))
    except Exception:
        return None


def _fetch_daily(code: str, start: str, end: str, cfg: dict) -> List[Dict]:
    from futu import OpenQuoteContext, KLType, RET_OK
    futu_cfg = cfg.get('futu') or (cfg.get('trading') or {}).get('futu') or {}
    with OpenQuoteContext(host=str(futu_cfg.get('host', '127.0.0.1')),
                          port=int(futu_cfg.get('port', 11111))) as ctx:
        ret, data, _ = ctx.request_history_kline(
            code=code, start=start, end=end, ktype=KLType.K_DAY,
            extended_time=False, max_count=120,
        )
    if ret != RET_OK or data is None or len(data) == 0:
        return []
    rows = []
    for _, r in data.iterrows():
        try:
            rows.append({
                'date': str(r['time_key'])[:10],
                'close': float(r['close']),
                'volume': float(r.get('volume') or 0),
            })
        except Exception:
            continue
    rows.sort(key=lambda x: x['date'])
    return rows


def _compute_outcome(check: Dict, rows: List[Dict]) -> Dict:
    base_price = float(check.get('price') or 0)
    out = {'bars_after': 0, 'complete': False}
    if base_price <= 0:
        return out
    dt = _parse_dt(check.get('ts'))
    day = dt.strftime('%Y-%m-%d') if dt else str(check.get('ts', ''))[:10]
    # 排除检查日当天与停牌日（volume=0），保证“N 个交易日”口径与 backfill_outcomes 一致
    future = [r for r in rows if r['date'] > day and float(r.get('volume') or 0) > 0]
    closes = [r['close'] for r in future]
    out['bars_after'] = len(closes)
    for n, idx in ((1, 0), (3, 2), (5, 4)):
        if len(closes) > idx:
            out[f'r{n}'] = round((closes[idx] / base_price - 1.0) * 100, 3)
    if len(closes) >= 5:
        out['complete'] = True
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--codes', default=None)
    args = ap.parse_args()

    cfg = yaml.safe_load((BASE_DIR / 'config.yaml').read_text(encoding='utf-8'))
    codes_filter = {c.strip() for c in args.codes.split(',') if c.strip()} if args.codes else None
    checks = scan_ledger.load_checks()
    done = {o['scan_id'] for o in scan_ledger.load_outcomes()
            if o.get('scan_id') and o.get('complete')}
    todo = []
    for c in checks:
        if c.get('scan_id') in done:
            continue
        dt = _parse_dt(c.get('ts'))
        if dt is None or dt < datetime.now() - timedelta(days=args.days):
            continue
        if codes_filter and c.get('stock_code') not in codes_filter:
            continue
        todo.append(c)
    logger.info(f'待回填检查: {len(todo)} 条')
    if not todo:
        return

    by_code: Dict[str, List] = {}
    for c in todo:
        by_code.setdefault(c.get('stock_code', ''), []).append(c)
    n = 0
    for code, items in by_code.items():
        dts = [_parse_dt(x.get('ts')) for x in items]
        dts = [d for d in dts if d]
        if not dts:
            continue
        start = (min(dts) - timedelta(days=30)).strftime('%Y-%m-%d')
        end = datetime.now().strftime('%Y-%m-%d')
        rows = _fetch_daily(code, start, end, cfg)
        if not rows:
            logger.warning(f'{code} 无日K，跳过')
            continue
        for c in items:
            out = _compute_outcome(c, rows)
            scan_ledger.record_outcome(c['scan_id'], stock_code=code, **out)
            n += 1
    logger.info(f'完成回填: {n} 条')


if __name__ == '__main__':
    main()
