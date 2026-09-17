"""市场级日报 → 证据事件。

用户每日采集的市场舆情以 HTML 报告落在 `~/Documents/daily-market-suite-v10-1/output/`。
它是**市场级**的：不属于任何单一标的，但每个候选都该看到 —— 所以以
`security_id='MARKET'` 的事件入库，由来源适配器并入**每一个**候选的证据包。

两个刻意的选择：

- **从报告开头截断**，不按小节解析。执行摘要（一句话结论 / 核心驱动 / 关键风险）永远在
  最前面；而分节结构随版本演进（目录下已有 v2/v3/v4 并存），按小节解析太脆。截断状态记进
  包内，`content_hash` 仍对**全文**计算，所以被截掉的部分依然可审计。
- **`published_at` 用文件 mtime**，不用报告自述的时间。按设计 §5.2 的同一条道理：文件是
  数据、它的自述是它的说法；我们能用的是「我们这边何时拿到它」。报告自述时间仍在正文里。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

# 市场级正文上限。比单条证券事件的 2000 宽 —— 一份日报抽出来有 13–17K 字符，2000 会砍掉
# 执行摘要之外的全部内容，而执行摘要正是它最有价值的部分。
MAX_DIGEST_CHARS = 6000
MARKET_SECURITY = 'MARKET'
MARKET_EVENT_TYPE = 'market_digest'
DIGEST_DIR = Path.home() / 'Documents' / 'daily-market-suite-v10-1' / 'output'


def html_to_text(html: str) -> str:
    """去脚本/样式/标签，压缩空白。"""
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', html, flags=re.S | re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def publish_time(path: Path) -> str:
    """`published_at` = 文件的 mtime（我们这边拿到它的时刻），不是报告自述的时间。"""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def build_digest_event(path: Path, *, max_chars: int = MAX_DIGEST_CHARS) -> dict:
    """把一份 HTML 日报变成一条市场级证据事件。"""
    raw = path.read_text(errors='replace')
    text = html_to_text(raw)
    if not text:
        raise ValueError(f'DIGEST_EMPTY:{path}')
    return {
        'security_id': MARKET_SECURITY,
        'event_type': MARKET_EVENT_TYPE,
        'summary': text[:max_chars],
        # 全文哈希：截断不改哈希，被截掉的部分仍可审计
        'content_hash': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'source_url': str(path),
        'source_type': 'daily_market_report',
        'published_at': publish_time(path),
        'quality_status': 'verified',
        'digest_total_chars': len(text),
        'digest_truncated': len(text) > max_chars,
    }


DATE_PREFIX = re.compile(r'^(\d{4}-\d{2}-\d{2})')


def latest_digest(directory: Path, session: str) -> Path | None:
    """**日期 ≤ session 的最近一份**日报；没有则 None。

    同一天可能有多份（v2/v3/v4、盘前/盘后、交易日记），取该日 mtime 最新的一份。
    取哪一份会打印出来 —— 取错必须看得见。

    取「≤ session 的最近一份」而不是「必须等于 session」：日报不一定每个交易日都有，
    而最近一份市场综述仍是有用的背景（可见性由 `published_at` 与窗口自行把关）。
    """
    if not directory.is_dir():
        return None
    best = None
    for path in directory.glob('*.html'):
        m = DATE_PREFIX.match(path.name)
        if not m or m.group(1) > session:
            continue
        key = (m.group(1), path.stat().st_mtime)
        if best is None or key > best[0]:
            best = (key, path)
    return best[1] if best else None


def ingest(digest_path: Path, out_path: Path, *, max_chars: int = MAX_DIGEST_CHARS) -> dict:
    """产出一条市场级事件的 JSONL，交给 `import-evidence` 走同一条入库路径。"""
    event = build_digest_event(digest_path, max_chars=max_chars)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(event, ensure_ascii=False) + '\n', encoding='utf-8')
    return {'source': str(digest_path), 'output': str(out_path),
            'total_chars': event['digest_total_chars'],
            'kept_chars': len(event['summary']),
            'truncated': event['digest_truncated'],
            'published_at': event['published_at']}


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(description='把当天 HTML 日报转成市场级证据 JSONL')
    p.add_argument('--digest', help='指定 HTML；缺省时按 --session 在 --dir 里找最新一份')
    p.add_argument('--dir', default=str(DIGEST_DIR), help='日报目录')
    p.add_argument('--session', help='交易日 YYYY-MM-DD（用于挑文件）')
    p.add_argument('--output', required=True)
    args = p.parse_args(argv)

    src = Path(args.digest) if args.digest else (
        latest_digest(Path(args.dir), args.session) if args.session else None)
    if src is None:
        print(json.dumps({'status': 'NO_DIGEST', 'dir': args.dir,
                          'session': args.session}, ensure_ascii=False))
        return 0
    print(json.dumps(ingest(src, Path(args.output)), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
