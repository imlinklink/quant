# -*- coding: utf-8 -*-
"""picker confidence 安全转换回归。"""
import unittest

from scripts.live_trading.llm_suggestions.picker import _clean_confidence


class TestCleanConfidence(unittest.TestCase):
    def test_normal(self):
        assert _clean_confidence(0.8) == 0.8
        assert _clean_confidence('0.3') == 0.3

    def test_none_and_bad_type(self):
        assert _clean_confidence(None) == 0.5
        assert _clean_confidence('高') == 0.5
        assert _clean_confidence({}) == 0.5

    def test_out_of_range_falls_back(self):
        assert _clean_confidence(2.0) == 0.5
        assert _clean_confidence(-0.1) == 0.5

    def test_missing_defaults_to_half(self):
        assert _clean_confidence(0.5) == 0.5
