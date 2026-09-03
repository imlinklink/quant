"""宏观日报 HTML → 纯文本（供大模型阅读）。"""
import os
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Optional


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self._skip_depth += 1
        if tag in ('p', 'div', 'tr', 'li', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.parts.append('\n')
        if tag in ('td', 'th'):
            self.parts.append(' | ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in ('p', 'div', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self.parts.append('\n')

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self.parts.append(data)


def html_to_text(path) -> str:
    """把 HTML 日报转成保留标题/表格结构的纯文本。"""
    parser = _TextExtractor()
    try:
        parser.feed(Path(path).read_text(encoding='utf-8', errors='ignore'))
    except OSError as e:
        raise OSError(f'读取失败 {path}: {e}') from e
    text = ''.join(parser.parts)
    lines = [ln.strip() for ln in text.splitlines()]
    out = []
    for ln in lines:
        if ln and (not out or out[-1] != ln):
            out.append(ln)
    return '\n'.join(out)


def _candidates(directory: Path, kind: str) -> List[Path]:
    """按文件名关键字在目录里找日报。"""
    results = []
    if not directory.exists():
        return results
    for p in directory.rglob('*.html'):
        name = p.name.lower()
        if kind == 'pre' and ('pre-market' in name or 'pre_market' in name or 'premarket' in name):
            results.append(p)
        elif kind == 'post' and (
            'post-market' in name or 'post_market' in name or 'postmarket' in name
        ):
            results.append(p)
    return results


def find_latest_report(dirs, kind: str) -> Optional[Path]:
    """从多个目录找最近修改的那份盘前/盘后日报。"""
    all_hits = []
    for d in dirs:
        all_hits.extend(_candidates(Path(d), kind))
    if not all_hits:
        return None
    all_hits.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return all_hits[0]
