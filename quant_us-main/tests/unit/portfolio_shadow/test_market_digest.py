"""市场日报 → 市场级证据：抽取、挑选、截断、追加去重。"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.portfolio_shadow.evidence_source import EVIDENCE_IDENTITY, import_evidence_jsonl
from scripts.portfolio_shadow.market_digest import (MARKET_EVENT_TYPE, MARKET_SECURITY,
                                                    build_digest_event, html_to_text,
                                                    latest_digest)

HTML = """<html><head><title>日报</title>
<style>body{color:red}</style><script>var x=1;</script></head>
<body><h1>今日结论</h1><p>四连阴后<b>期货转涨</b></p><table><tr><td>SPX -0.58%</td></tr></table>
</body></html>"""


class HtmlToTextTests(unittest.TestCase):
    def test_strips_scripts_styles_and_tags(self):
        text = html_to_text(HTML)
        for gone in ('<', '>', 'var x=1', 'color:red'):
            self.assertNotIn(gone, text)
        for kept in ('今日结论', '期货转涨', 'SPX -0.58%'):
            self.assertIn(kept, text)

    def test_collapses_whitespace(self):
        self.assertEqual(html_to_text('<p>a</p>\n\n   <p>b</p>'), 'a b')


class DigestEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / '2026-09-16_premarket_report.html'
        self.path.write_text(HTML * 400, encoding='utf-8')      # 足够长以触发截断

    def test_event_is_market_scoped_and_citable(self):
        e = build_digest_event(self.path, max_chars=500)
        self.assertEqual(e['security_id'], MARKET_SECURITY)
        self.assertEqual(e['event_type'], MARKET_EVENT_TYPE)
        self.assertTrue(e['source_url'].endswith('.html'))
        self.assertTrue(e['published_at'])                     # 用文件 mtime，非报告自述

    def test_truncation_is_recorded_and_hash_covers_full_text(self):
        e = build_digest_event(self.path, max_chars=500)
        self.assertEqual(len(e['summary']), 500)
        self.assertTrue(e['digest_truncated'])
        self.assertGreater(e['digest_total_chars'], 500)
        # 哈希对**全文** —— 被截掉的部分仍可审计
        full = html_to_text(self.path.read_text(errors='replace'))
        self.assertEqual(e['content_hash'], hashlib.sha256(full.encode()).hexdigest())

    def test_untruncated_when_short(self):
        short = self.tmp / 'x.html'
        short.write_text('<p>很短</p>', encoding='utf-8')
        e = build_digest_event(short, max_chars=500)
        self.assertFalse(e['digest_truncated'])


class LatestDigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ('2026-09-09_premarket_report.html', '2026-09-11_premarket_report_v2.html',
                     '2026-09-11_premarket_report_v4.html', 'joe-trading-diary-0908.html'):
            (self.tmp / name).write_text('<p>x</p>', encoding='utf-8')

    def test_picks_latest_version_of_the_day(self):
        self.assertEqual(latest_digest(self.tmp, '2026-09-11').name,
                         '2026-09-11_premarket_report_v4.html')

    def test_falls_back_to_the_most_recent_earlier_day(self):
        """日报不一定每个交易日都有；≤ session 的最近一份仍是有用的背景。"""
        self.assertEqual(latest_digest(self.tmp, '2026-09-16').name,
                         '2026-09-11_premarket_report_v4.html')
        self.assertEqual(latest_digest(self.tmp, '2026-09-10').name,
                         '2026-09-09_premarket_report.html')

    def test_none_when_nothing_is_early_enough_or_dir_missing(self):
        self.assertIsNone(latest_digest(self.tmp, '2026-09-01'))
        self.assertIsNone(latest_digest(self.tmp / 'nope', '2026-09-16'))


class AppendSemanticsTests(unittest.TestCase):
    """追加时「首次导入为准」：重跑不能把 observed_at 刷成今天。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / 'e.jsonl'
        self.src.write_text(json.dumps({
            'security_id': MARKET_SECURITY, 'event_type': MARKET_EVENT_TYPE,
            'summary': '今日市场综述', 'source_url': 'file://x',
            'source_type': 'daily_market_report', 'published_at': '2026-09-16T20:00:00Z',
            'quality_status': 'verified'}) + '\n', encoding='utf-8')
        self.out = self.tmp / 'store.csv'

    def _import(self, ingested_at):
        return import_evidence_jsonl(self.src, self.out, append=True,
                                     ingested_at=ingested_at)

    def test_first_import_is_kept_on_reimport(self):
        first = self._import('2026-09-16T21:00:00+00:00')
        second = self._import('2026-09-17T21:00:00+00:00')
        self.assertEqual(first['added'], 1)
        self.assertEqual(second['added'], 0)               # 没有新增
        row = pd.read_csv(self.out)
        self.assertEqual(len(row), 1)
        self.assertTrue(str(row.observed_at.iloc[0]).startswith('2026-09-16'))

    def test_dedup_key_is_content_identity_not_evidence_id(self):
        """`evidence_id` 把 observed_at 算进去，按它去重等于没去。"""
        self.assertNotIn('observed_at', EVIDENCE_IDENTITY)
        self.assertIn('content_hash', EVIDENCE_IDENTITY)

    def test_different_content_is_added(self):
        self._import('2026-09-16T21:00:00+00:00')
        self.src.write_text(json.dumps({
            'security_id': MARKET_SECURITY, 'event_type': MARKET_EVENT_TYPE,
            'summary': '另一天的综述', 'source_url': 'file://y',
            'source_type': 'daily_market_report', 'published_at': '2026-09-17T20:00:00Z',
            'quality_status': 'verified'}) + '\n', encoding='utf-8')
        self.assertEqual(self._import('2026-09-17T21:00:00+00:00')['added'], 1)
        self.assertEqual(len(pd.read_csv(self.out)), 2)
