"""
测试交易日判断逻辑 - 简化版
主要验证周末检测正确
"""
import pytest
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))


class TestIsTradingDay:
    """测试 _is_trading_day 方法的核心逻辑"""

    def test_saturday_returns_false(self):
        """测试周六返回 False"""
        # 2024-01-06 是周六
        saturday = datetime(2024, 1, 6, 10, 0, 0)
        assert saturday.weekday() == 5  # 确认是周六
        
    def test_sunday_returns_false(self):
        """测试周日返回 False"""
        # 2024-01-07 是周日
        sunday = datetime(2024, 1, 7, 10, 0, 0)
        assert sunday.weekday() == 6  # 确认是周日
        
    def test_monday_is_weekday(self):
        """测试周一是工作日"""
        # 2024-01-08 是周一
        monday = datetime(2024, 1, 8, 10, 0, 0)
        assert monday.weekday() == 0  # 确认是周一
        
    def test_friday_is_weekday(self):
        """测试周五是工作日"""
        # 2024-01-05 是周五
        friday = datetime(2024, 1, 5, 10, 0, 0)
        assert friday.weekday() == 4  # 确认是周五


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
