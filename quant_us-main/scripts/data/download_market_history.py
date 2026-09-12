#!/usr/bin/env python3
"""通过 Futu 分页下载历史 K 线到本地不可变分区，支持断点续传。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.data.io_utils import read_frame, sha256_file, write_frame


def year_ranges(start, end):
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    for year in range(start.year, end.year + 1):
        yield max(start, pd.Timestamp(year, 1, 1)), min(end, pd.Timestamp(year, 12, 31))


def fetch_pages(ctx, code, start, end, ktype, *, autype, session, max_count=1000,
                retries=5, retry_seconds=3.0, request_interval=.55):
    """拉取并验证所有分页；ctx 便于测试时注入。"""
    page_key = None; frames = []; pages = 0
    while True:
        for attempt in range(retries):
            ret, data, next_key = ctx.request_history_kline(
                code, start=str(pd.Timestamp(start).date()), end=str(pd.Timestamp(end).date()),
                ktype=ktype, autype=autype, max_count=max_count,
                page_req_key=page_key, extended_time=False, session=session)
            if ret == 0:
                if request_interval:time.sleep(request_interval)
                break
            if attempt + 1 == retries:
                raise RuntimeError(f'{code} {start}..{end} 下载失败: {data}')
            time.sleep(retry_seconds * (2 ** attempt))
        if data is not None and len(data):
            frames.append(data.copy())
        pages += 1
        if next_key is None:
            break
        if next_key == page_key:
            raise RuntimeError(f'{code} 分页游标没有前进')
        page_key = next_key
    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return frame, pages


def _load_checkpoint(path):
    path = Path(path)
    if not path.exists(): return {'completed': {}, 'failed': {}, 'unavailable': {}}
    state=json.loads(path.read_text(encoding='utf-8'));state.setdefault('unavailable',{});return state


def _save_checkpoint(path, state):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def records_for_request(records, codes, kinds, start, end, adjustment):
    """筛选本次代码、周期、复权口径和年份对应的检查点记录。"""
    years={str(part_start.year) for part_start,_ in year_ranges(start,end)}
    prefixes={(str(code),str(kind),str(adjustment)) for code in codes for kind in kinds}
    result={}
    for key,item in records.items():
        parts=key.split('|')
        if len(parts)==4 and tuple(parts[:3]) in prefixes and parts[3] in years:
            result[key]=item
    return result


def failures_for_request(state, codes, kinds, start, end, adjustment):
    """只返回本次口径和区间的失败项，忽略检查点中的历史口径。"""
    return records_for_request(state.get('failed',{}),codes,kinds,start,end,adjustment)


def download(ctx, codes, start, end, output_root, checkpoint, *, kinds=('day',),
             overwrite=False, futu_types=None, listing_dates=None, autype='qfq'):
    if futu_types is None:
        from futu import AuType, KLType, Session
        _AUTYPES = {'qfq': AuType.QFQ, 'none': AuType.NONE, 'hfq': AuType.HFQ}
        if autype not in _AUTYPES:
            raise ValueError(f'未知 autype: {autype}（可选 {" / ".join(_AUTYPES)}）')
        futu_types = {'day': KLType.K_DAY, 'autype': _AUTYPES[autype],
                      'autype_name': autype, 'session': Session.RTH}
    autype_name=str(futu_types.get('autype_name') or futu_types['autype']).lower().split('.')[-1]
    state = _load_checkpoint(checkpoint); root = Path(output_root)
    for code in codes:
        safe_code = code.replace('.', '_')
        for kind in kinds:
            for part_start, part_end in year_ranges(start, end):
                key = f'{code}|{kind}|{autype_name}|{part_start.year}'
                if key in state['unavailable'] and not overwrite:
                    continue
                listed=pd.to_datetime((listing_dates or {}).get(code),errors='coerce')
                if pd.notna(listed) and listed.year>1970 and part_end<listed:
                    state['unavailable'][key]={'reason':'BEFORE_REPORTED_LISTING',
                        'listing_date':str(listed.date())}
                    state['failed'].pop(key,None);_save_checkpoint(checkpoint,state);continue
                target = root / kind / autype_name / f'year={part_start.year}' / f'{safe_code}.csv.gz'
                existing = None
                if target.exists() and not overwrite:
                    record = state['completed'].get(key, {})
                    if record.get('sha256') != sha256_file(target):
                        raise FileExistsError(f'已有分区哈希与检查点不一致: {target}')
                    covered_start = pd.Timestamp(record.get('requested_start', '2999-01-01'))
                    covered_end = pd.Timestamp(record.get('requested_end', '1900-01-01'))
                    if covered_start <= part_start and covered_end >= part_end:
                        continue
                    existing = read_frame(target)
                try:
                    frame, pages = fetch_pages(ctx, code, part_start, part_end,
                        futu_types[kind], autype=futu_types['autype'],
                        session=futu_types['session'])
                    if frame.empty:
                        raise RuntimeError('EMPTY_RESPONSE: 需核对上市区间、权限或数据源覆盖')
                    if existing is not None and not existing.empty:
                        frame = pd.concat([existing, frame], ignore_index=True)
                        keys = [c for c in ('code', 'time_key') if c in frame]
                        frame = frame.drop_duplicates(keys, keep='last') if keys else frame.drop_duplicates()
                    frame['downloaded_at'] = datetime.now(timezone.utc).isoformat()
                    frame['requested_start'] = str(part_start.date())
                    frame['requested_end'] = str(part_end.date())
                    write_frame(frame, target, overwrite=overwrite or existing is not None)
                    state['completed'][key] = {'path': str(target.resolve()), 'rows': len(frame),
                        'pages': pages, 'sha256': sha256_file(target),
                        'requested_start': str(part_start.date()),
                        'requested_end': str(part_end.date())}
                    state['failed'].pop(key, None)
                except Exception as exc:
                    state['failed'][key] = {'error': str(exc),
                        'at': datetime.now(timezone.utc).isoformat()}
                    _save_checkpoint(checkpoint, state)
                    continue
                _save_checkpoint(checkpoint, state)
    # Futu常用1970占位。若后续年份有数据，则更早EMPTY_RESPONSE属于上市前不可用。
    first_year={}
    for key in state['completed']:
        parts=key.split('|')
        if len(parts)!=4:continue
        code,kind,adjustment,year=parts
        first_year[(code,kind,adjustment)]=min(first_year.get((code,kind,adjustment),9999),int(year))
    for key,item in list(state['failed'].items()):
        parts=key.split('|')
        if len(parts)!=4:continue
        code,kind,adjustment,year=parts;first=first_year.get((code,kind,adjustment))
        if first is not None and int(year)<first and str(item.get('error','')).startswith('EMPTY_RESPONSE'):
            state['unavailable'][key]={'reason':'UNAVAILABLE_BEFORE_FIRST_DATA','first_data_year':first}
            del state['failed'][key]
    _save_checkpoint(checkpoint,state)
    return state


def main():
    parser = argparse.ArgumentParser(description='下载并缓存 Futu 历史行情')
    parser.add_argument('--master', required=True); parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True); parser.add_argument('--output-root',
        default='data/market_history/raw'); parser.add_argument('--checkpoint',
        default='data/market_history/checkpoints/download_state.json')
    parser.add_argument('--overwrite', action='store_true')
    parser.add_argument('--autype', default='qfq', choices=('qfq', 'none', 'hfq'),
                        help='复权口径；正式执行价需用 none（原始可交易价），特征价用 qfq')
    parser.add_argument('--host', default='127.0.0.1'); parser.add_argument('--port', type=int, default=11111)
    args = parser.parse_args()
    master = read_frame(args.master); codes = master.code.dropna().astype(str).unique().tolist()
    try:
        from futu import OpenQuoteContext
    except Exception as exc:
        raise SystemExit(f'无法导入 futu SDK: {exc}')
    ctx = OpenQuoteContext(host=args.host, port=args.port)
    try:
        state = download(ctx, codes, args.start, args.end, args.output_root,
                         args.checkpoint, kinds=('day',), overwrite=args.overwrite,
                         autype=args.autype)
    finally:
        ctx.close()
    active_failures=failures_for_request(state,codes,('day',),args.start,args.end,args.autype)
    print(json.dumps({'completed': len(state['completed']), 'failed': len(active_failures)},
                     ensure_ascii=False))
    return 1 if active_failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
