# -*- coding: utf-8 -*-
"""market_brief 简报 schema 回归：generate_brief 输出必须能过校验。"""
import unittest

import pytest


class TestMarketBriefSchema(unittest.TestCase):
    def setUp(self):
        js = pytest.importorskip('jsonschema')
        self.validate = js.validate
        self.ValidationError = js.ValidationError
        from mutifactor.llm.schemas import MARKET_BRIEF_SCHEMA, MARKET_STATUS_SCHEMA
        from mutifactor.llm.advisor import SCHEMA_MAP
        self.brief_schema = MARKET_BRIEF_SCHEMA
        self.status_schema = MARKET_STATUS_SCHEMA
        self.map = SCHEMA_MAP

    def test_brief_sample_passes_brief_schema(self):
        sample = {
            'risk_level': 'cautious',
            'risk_note': '非农引发重定价，注意利率风险',
            'buy_frequency': 'reduce',
            'suggested_position_ratio': 0.3,
        }
        self.validate(sample, self.brief_schema)
        self.validate(sample, self.map['market_brief'])

    def test_brief_sample_with_null_ratio_passes(self):
        sample = {
            'risk_level': 'normal',
            'risk_note': '',
            'buy_frequency': 'normal',
            'suggested_position_ratio': None,
        }
        self.validate(sample, self.brief_schema)

    def test_old_status_schema_rejects_brief_fields(self):
        # 回归根因：曾误把简报输出交给 market_status schema（要求 market_type，
        # 且 additionalProperties=False），必然失败
        sample = {
            'risk_level': 'cautious',
            'risk_note': 'x',
            'buy_frequency': 'reduce',
            'suggested_position_ratio': None,
        }
        with pytest.raises(self.ValidationError):
            self.validate(sample, self.status_schema)

    def test_status_schema_still_matches_status_prompt(self):
        sample = {
            'market_type': 'range',
            'risk_note': '窄幅震荡',
            'suggested_max_positions': 2,
            'suggested_stop_multiplier': 2.0,
        }
        self.validate(sample, self.status_schema)

    def test_market_brief_in_schema_map(self):
        assert 'market_brief' in self.map
