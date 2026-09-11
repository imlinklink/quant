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
                retries=3, retry_seconds=2.0):
    """拉取并验证所有分页；ctx 便于测试时注入。"""
    page_key = None; frames = []; pages = 0
    while True:
        for attempt in range(retries):
            ret, data, next_key = ctx.request_history_kline(
                code, start=str(pd.Timestamp(start).date()), end=str(pd.Timestamp(end).date()),
                ktype=ktype, autype=autype, max_count=max_count,
                page_req_key=page_key, extended_time=False, session=session)
            if ret == 0:
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
    if not path.exists(): return {'completed': {}, 'failed': {}}
    return json.loads(path.read_text(encoding='utf-8'))


def _save_checkpoint(path, state):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def download(ctx, codes, start, end, output_root, checkpoint, *, kinds=('day',),
             overwrite=False, futu_types=None):
    if futu_types is None:
        from futu import AuType, KLType, Session
        futu_types = {'day': KLType.K_DAY, 'autype': AuType.NONE, 'session': Session.RTH}
    state = _load_checkpoint(checkpoint); root = Path(output_root)
    for code in codes:
        safe_code = code.replace('.', '_')
        for kind in kinds:
            for part_start, part_end in year_ranges(start, end):
                key = f'{code}|{kind}|{part_start.year}'
                target = root / kind / f'year={part_start.year}' / f'{safe_code}.csv.gz'
                if target.exists() and not overwrite:
                    if state['completed'].get(key, {}).get('sha256') == sha256_file(target):
                        continue
                    raise FileExistsError(f'已有分区未出现在有效检查点中: {target}')
                try:
                    frame, pages = fetch_pages(ctx, code, part_start, part_end,
                        futu_types[kind], autype=futu_types['autype'],
                        session=futu_types['session'])
                    if frame.empty:
                        raise RuntimeError('EMPTY_RESPONSE: 需核对上市区间、权限或数据源覆盖')
                    frame['downloaded_at'] = datetime.now(timezone.utc).isoformat()
                    frame['requested_start'] = str(part_start.date())
                    frame['requested_end'] = str(part_end.date())
                    write_frame(frame, target, overwrite=overwrite)
                    state['completed'][key] = {'path': str(target.resolve()), 'rows': len(frame),
                        'pages': pages, 'sha256': sha256_file(target)}
                    state['failed'].pop(key, None)
                except Exception as exc:
                    state['failed'][key] = {'error': str(exc),
                        'at': datetime.now(timezone.utc).isoformat()}
                    _save_checkpoint(checkpoint, state)
                    continue
                _save_checkpoint(checkpoint, state)
    return state


def main():
    parser = argparse.ArgumentParser(description='下载并缓存 Futu 历史行情')
    parser.add_argument('--master', required=True); parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True); parser.add_argument('--output-root',
        default='data/market_history/raw'); parser.add_argument('--checkpoint',
        default='data/market_history/checkpoints/download_state.json')
    parser.add_argument('--overwrite', action='store_true')
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
                         args.checkpoint, kinds=('day',), overwrite=args.overwrite)
    finally:
        ctx.close()
    print(json.dumps({'completed': len(state['completed']), 'failed': len(state['failed'])},
                     ensure_ascii=False))
    return 1 if state['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
