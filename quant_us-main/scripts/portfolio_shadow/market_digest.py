"""市场级日报 → 证据事件。

日报由**另一个项目**产出，各定时任务用 `ops/publish_digest.py` 发布到
`~/quant-inputs/market-digest/`（不是 `~/Documents` —— 后台调度没有 TCC 权限，读不到）。
它是**市场级**的：不属于任何单一标的，但每个候选都该看到 —— 所以以
`security_id='MARKET'` 的事件入库，由来源适配器并入**每一个**候选的证据包。

三个刻意的选择：

- **从报告开头截断**，不按小节解析。执行摘要（一句话结论 / 核心驱动 / 关键风险）永远在
  最前面；而分节结构随版本演进（目录下已有 v2/v3/v4 并存），按小节解析太脆。截断状态记进
  包内，`content_hash` 仍对**全文**计算，所以被截掉的部分依然可审计。
- **`published_at` 用文件 mtime**，不用报告自述的时间。按设计 §5.2 的同一条道理：文件是
  数据、它的自述是它的说法；我们能用的是「我们这边何时拿到它」。报告自述时间仍在正文里。
- **同日多份按文件名取用序**（见 `DIGEST_PRIORITY`）。2026-09-18 起同一天会并存盘前/盘后/
  美股盘后/全球盘后四类日报，它们同日期前缀、而盘前总是**最早**写出（mtime 最小），
  若只按 mtime 比就会被盘后顶掉、且每天喂给模型的是哪一类会随产出顺序漂移。
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
DIGEST_DIR = Path.home() / 'quant-inputs' / 'market-digest'

# 同日多份日报时的取用序：**数字越小越优先**，未列出的文件名排最后。
# 与 `ops/publish_digest.py` 的 KINDS 一一对应，改动需两边同步。
# 为什么盘前优先：它是决策链的起点，且与本次改造前的既有效果一致（此前目录里只有盘前）。
# 想换冠军只改这一个元组的顺序，不必动排序逻辑。
DIGEST_PRIORITY = (
    ('premarket', 0),       # 盘前简报
    ('uspostmarket', 1),    # 美股盘后深度
    ('globalpost', 2),      # 全球盘后
    ('postmarket', 3),      # 每日盘后复盘
)
DEFAULT_PRIORITY = 9


def name_priority(name: str) -> int:
    """从文件名推断取用序；命中不到返回 ``DEFAULT_PRIORITY``。

    先去掉非字母数字再匹配，这样 `2026-09-18_us_postmarket.html` 与
    `2026-09-18_uspostmarket.html` 等价；同时按 ``DIGEST_PRIORITY`` 的顺序匹配，
    保证 `uspostmarket`（含子串 `postmarket`）不会先被 `postmarket` 抢走。
    """
    stem = re.sub(r'[^a-z0-9]', '', Path(name).stem.lower())
    for token, pri in DIGEST_PRIORITY:
        if token in stem:
            return pri
    return DEFAULT_PRIORITY


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

    同一天可能有多份（盘前/盘后/美股盘后/全球盘后、v2/v3/v4），取该日中
    「取用序最小、其次 mtime 最大」的一份 —— 取用序见 ``DIGEST_PRIORITY``。
    取哪一份会打印出来 —— 取错必须看得见。

    取「≤ session 的最近一份」而不是「必须等于 session」：日报不一定每个交易日都有，
    而最近一份市场综述仍是有用的背景（可见性由 `published_at` 与窗口自行把关）。

    注意只看**顶层** `*.html`：辅助产物（监控页、周报）发布在 `aux/` 子目录里，
    因此不会被当成市场日报喂给模型。
    """
    if not directory.is_dir():
        return None
    best = None
    for path in directory.glob('*.html'):
        m = DATE_PREFIX.match(path.name)
        if not m or m.group(1) > session:
            continue
        # 先比日期，再比取用序（小的优先 ⇒ 取负），最后才用 mtime 兜底
        key = (m.group(1), -name_priority(path.name), path.stat().st_mtime)
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
