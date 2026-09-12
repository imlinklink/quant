#!/usr/bin/env python3
"""富途（OpenD）历史数据能力探针：逐条实测，输出 source_assessment.json。

只读，不需要交易解锁，不产生任何订单。需在本机 OpenD 已启动、已登录且具备美股
历史行情权限时运行：

    python3 scripts/data/probe_futu_source.py \
      --delisted-codes US.XXXX US.YYYY \
      --output-dir data/source_archive/futu_probe/2026-09-12

`--delisted-codes` 由使用者提供（用于验证是否覆盖已退市证券）；不提供时该项记为 untested。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _status(ok):
    return 'provided' if ok else 'unavailable'


def probe_basicinfo(ctx, codes):
    from futu import Market, SecurityType
    frames = []
    for kind in (SecurityType.STOCK, SecurityType.ETF):
        ret, data = ctx.get_stock_basicinfo(market=Market.US, stock_type=kind)
        if ret != 0:
            raise RuntimeError(f'get_stock_basicinfo({kind}) 失败: {data}')
        if data is not None and len(data):
            frames.append(data)
    import pandas as pd
    basic = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    has_listing = any(c in basic.columns for c in ('listing_date', 'list_time'))
    return {'status': _status(not basic.empty), 'securities': int(len(basic)),
            'listing_date_present': bool(has_listing),
            'columns': sorted(map(str, basic.columns))[:20]}


def _fetch_kline(ctx, code, start, end, autype):
    from futu import KLType
    ret, data, _ = ctx.request_history_kline(code=code, start=start, end=end,
                                             ktype=KLType.K_DAY, autype=autype, max_count=1000)
    return data if ret == 0 else None


def probe_delisted(ctx, codes, start, end):
    import pandas as pd
    from scripts.data.derive_corporate_actions import derive_actions
    if not codes:
        return {'status': 'untested', 'reason': '未提供 --delisted-codes'}
    found = []
    for code in codes:
        from futu import AuType
        data = _fetch_kline(ctx, code, start, end, AuType.NONE)
        found.append({'code': code, 'history_rows': 0 if data is None else int(len(data)),
                      'last_date': None if data is None or data.empty else str(data['time_key'].iloc[-1])})
    provided = [c for c in found if c['history_rows'] > 0]
    return {'status': _status(bool(provided)), 'probes': found,
            'note': '退市证券有历史日线≠有退市日期；退市日仍需外部来源'}


def probe_ticker_history(ctx, old_code, new_code, start, end):
    from futu import AuType
    if not old_code or not new_code:
        return {'status': 'untested', 'reason': '未提供 --rename-old/--rename-new'}
    old = _fetch_kline(ctx, old_code, start, end, AuType.NONE)
    new = _fetch_kline(ctx, new_code, start, end, AuType.NONE)
    return {'status': 'unavailable',
            'reason': 'Futu 无 ticker 变更映射接口；旧码可否取到历史仅说明代码仍在目录',
            'old_rows': 0 if old is None else int(len(old)),
            'new_rows': 0 if new is None else int(len(new))}


def probe_corporate_actions(ctx, codes, start, end):
    from futu import AuType
    from scripts.data.derive_corporate_actions import derive_actions
    results = []
    for code in codes:
        raw = _fetch_kline(ctx, code, start, end, AuType.NONE)
        qfq = _fetch_kline(ctx, code, start, end, AuType.QFQ)
        if raw is None or qfq is None or raw.empty or qfq.empty:
            results.append({'code': code, 'derived': None}); continue
        r = raw.rename(columns={'time_key': 'session'})[['session', 'close']]
        a = qfq.rename(columns={'time_key': 'session'})[['session', 'close']]
        results.append({'code': code, 'derived': int(len(derive_actions(r, a, code)))})
    return {'status': 'derived_only', 'probes': results,
            'note': '可由 QFQ 与不复权价差反推，但标记 unverified，需人工复核'}


def main():
    p = argparse.ArgumentParser(description='富途历史数据能力探针（只读）')
    p.add_argument('--host', default='127.0.0.1'); p.add_argument('--port', type=int, default=11111)
    p.add_argument('--delisted-codes', nargs='*', default=[])
    p.add_argument('--rename-old'); p.add_argument('--rename-new')
    p.add_argument('--corporate-codes', nargs='*', default=['US.AAPL', 'US.MU'])
    p.add_argument('--start', default='2016-01-01'); p.add_argument('--end', default='2026-08-31')
    p.add_argument('--output-dir', required=True)
    args = p.parse_args()

    from futu import OpenQuoteContext
    ctx = OpenQuoteContext(host=args.host, port=args.port)
    report = {'source': 'futu_opend', 'as_of': date.today().isoformat(), 'probes': {}}
    try:
        for name, fn in (('basicinfo', lambda: probe_basicinfo(ctx, [])),
                         ('delisted', lambda: probe_delisted(ctx, args.delisted_codes, args.start, args.end)),
                         ('ticker_history', lambda: probe_ticker_history(ctx, args.rename_old,
                                                                         args.rename_new, args.start, args.end)),
                         ('corporate_actions', lambda: probe_corporate_actions(ctx, args.corporate_codes,
                                                                                args.start, args.end))):
            try:
                report['probes'][name] = fn()
            except Exception as exc:  # 单项失败不阻塞其余
                report['probes'][name] = {'status': 'error', 'reason': str(exc)}
    finally:
        ctx.close()

    # 许可三项与时间可审计性无法由 API 判定，留给人工。
    report['manual'] = {
        'download_right': 'requires_user_input',
        'retention_right': 'requires_user_input',
        'research_use': 'requires_user_input',
        'timestamp_auditability': 'unavailable',
    }
    delisted_ok = report['probes'].get('delisted', {}).get('status') == 'provided'
    report['conclusion'] = 'partial' if not delisted_ok else 'pass_candidate'
    report['conclusion_note'] = (
        'Futu 覆盖当前存续证券；退市/ticker 变更/公司行动为不可得或仅可反推。'
        '结论若需要退市样本，仍应标 inconclusive。')

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'source_assessment.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'conclusion': report['conclusion'], 'path': str(out / 'source_assessment.json')},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
