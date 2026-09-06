# -*- coding: utf-8 -*-
"""外部观点（推特总结）输入通道：prompt 拼接与文件读取。"""
import tempfile
import unittest
from pathlib import Path

from scripts.live_trading.llm_suggestions import picker
from scripts.live_trading.llm_suggestions.run_suggestions import load_view_files


class TestFormatExternalViews(unittest.TestCase):
    def test_empty_placeholder(self):
        out = picker.format_external_views(None)
        self.assertIn('没有提供外部观点素材', out)

    def test_two_views_with_sources(self):
        out = picker.format_external_views([
            {'source': '推特观点A', 'text': '看多存储周期。'},
            {'source': '推特观点B', 'text': '警惕利率上行。'},
        ])
        self.assertIn('推特观点A', out)
        self.assertIn('看多存储周期', out)
        self.assertIn('推特观点B', out)
        self.assertIn('未验证', out)


class TestLoadViewFiles(unittest.TestCase):
    def test_read_files_and_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f1 = root / 'tw_a.md'
            f2 = root / 'sub'
            f2.mkdir()
            f3 = f2 / 'tw_b.txt'
            f1.write_text('观点一内容', encoding='utf-8')
            f3.write_text('观点二内容', encoding='utf-8')
            views = load_view_files([str(root)])
            self.assertEqual(len(views), 2)
            self.assertEqual({v['source'] for v in views},
                             {'tw_a.md', 'tw_b.txt'})
            text = ' '.join(v['text'] for v in views)
            self.assertIn('观点一内容', text)
            self.assertIn('观点二内容', text)


class TestNormalizeCandidate(unittest.TestCase):
    def test_valid_codes_pass(self):
        self.assertIsNotNone(picker.normalize_candidate(
            {'market': 'US', 'code': 'US.AAPL', 'direction': '多头'}, 0))
        self.assertIsNotNone(picker.normalize_candidate(
            {'market': 'HK', 'code': 'HK.00700', 'direction': '观察'}, 0))

    def test_placeholders_rejected(self):
        for code in ('HK.0000', 'US.XXXX', '空仓', ''):
            self.assertIsNone(picker.normalize_candidate(
                {'market': 'HK', 'code': code, 'direction': '多头'}, 0))

    def test_illegal_code_rejected(self):
        self.assertIsNone(picker.normalize_candidate(
            {'market': 'US', 'code': 'US.12345', 'direction': '多头'}, 0))
        self.assertIsNone(picker.normalize_candidate(
            {'market': 'HK', 'code': 'HK.700', 'direction': '多头'}, 0))

    def test_unknown_direction_falls_back(self):
        item = picker.normalize_candidate(
            {'market': 'US', 'code': 'US.MU', 'direction': '空仓'}, 0)
        self.assertEqual(item['direction'], '观察')


if __name__ == '__main__':
    unittest.main()
