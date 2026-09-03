"""
止盈止损策略单元测试
"""
import pytest
import pandas as pd
import numpy as np
from mutifactor.strategies.exit_strategy import (
    ExitStrategy,
    ATRStopOnlyStrategy,
    ATRDynamicStrategy,
    ExitStrategyFactory
)


class TestCalculateATR:
    """ATR计算测试"""

    def create_ohlc_data(self, high, low, close):
        """创建OHLC数据"""
        return pd.DataFrame({
            'high': high,
            'low': low,
            'close': close
        })

    def test_normal_calculation(self):
        """正常ATR计算"""
        df = self.create_ohlc_data(
            high=[10, 12, 11, 13, 12],
            low=[9, 10, 9.5, 11, 10],
            close=[9.5, 11, 10, 12, 11]
        )
        strategy = ATRStopOnlyStrategy()
        atr = strategy.calculate_atr(df, period=3)
        assert atr > 0, "ATR应该大于0"

    def test_flat_market(self):
        """无波动市场ATR应为0"""
        df = self.create_ohlc_data(
            high=[100, 100, 100, 100, 100],
            low=[100, 100, 100, 100, 100],
            close=[100, 100, 100, 100, 100]
        )
        strategy = ATRStopOnlyStrategy()
        atr = strategy.calculate_atr(df, period=3)
        assert atr == 0, "无波动时ATR应为0"

    def test_high_volatility(self):
        """高波动市场ATR应较大"""
        df = self.create_ohlc_data(
            high=[110, 120, 115, 125, 118],
            low=[95, 100, 105, 100, 102],
            close=[100, 110, 110, 115, 110]
        )
        strategy = ATRStopOnlyStrategy()
        atr = strategy.calculate_atr(df, period=3)
        assert atr > 5, "高波动时ATR应较大"

    def test_insufficient_data(self):
        """数据不足时返回默认值"""
        df = self.create_ohlc_data(
            high=[10, 12],
            low=[9, 10],
            close=[9.5, 11]
        )
        strategy = ATRStopOnlyStrategy()
        atr = strategy.calculate_atr(df, period=14)
        # 数据不足时应返回默认值或0
        assert isinstance(atr, (int, float))

    def test_gap_up(self):
        """跳空上涨情况"""
        df = self.create_ohlc_data(
            high=[10, 15, 14],  # 第二天跳空高开
            low=[9, 12, 12],
            close=[9.5, 14, 13]
        )
        strategy = ATRStopOnlyStrategy()
        atr = strategy.calculate_atr(df, period=2)
        assert atr > 0, "跳空时ATR应考虑前日收盘价"


class TestATRStopOnlyStrategy:
    """ATR止损策略测试"""

    def create_strategy(self, atr_multiplier=2.0, atr_period=14, chandelier_period=22):
        """创建策略实例"""
        config = {
            'atr_multiplier': atr_multiplier,
            'atr_period': atr_period,
            'chandelier_period': chandelier_period
        }
        return ATRStopOnlyStrategy(config)

    def create_position(self, cost_price=100, highest_price=105, stop_price=0):
        """创建持仓数据"""
        return {
            'cost_price': cost_price,
            'highest_price': highest_price,
            'stop_price': stop_price
        }

    def create_price_data(self, base_price=100, days=30, volatility=2):
        """创建价格数据"""
        np.random.seed(42)
        dates = pd.date_range(start='2024-01-01', periods=days, freq='D')
        close = base_price + np.random.randn(days) * volatility
        high = close + np.random.rand(days) * volatility
        low = close - np.random.rand(days) * volatility
        return pd.DataFrame({
            'high': high,
            'low': low,
            'close': close
        }, index=dates)

    def test_stop_loss_below_cost(self):
        """止损价不应高于成本价"""
        strategy = self.create_strategy(atr_multiplier=2.0)
        position = self.create_position(cost_price=100)
        df = self.create_price_data(base_price=110)  # 当前价格高于成本

        stop_price = strategy.calculate_stop_loss(position, df)
        assert stop_price < position['cost_price'], "止损价应低于成本价"

    def test_stop_loss_trails_up(self):
        """止损价只能上移（追踪盈利）"""
        strategy = self.create_strategy(atr_multiplier=2.0)
        position = self.create_position(cost_price=100, stop_price=95)
        df = self.create_price_data(base_price=120)  # 价格上涨

        stop_price = strategy.calculate_stop_loss(position, df)
        # 止损价应该大于等于之前的止损价
        assert stop_price >= position['stop_price'], "止损价不应下降"

    def test_check_exit_trigger(self):
        """触发止损检测"""
        strategy = self.create_strategy(atr_multiplier=1.5)
        position = self.create_position(cost_price=100, highest_price=100)
        df = self.create_price_data(base_price=100, volatility=1)

        # 计算止损价
        stop_price = strategy.calculate_stop_loss(position, df)
        
        # 价格跌破止损价
        current_price = stop_price - 5
        should_exit, reason, atr, take_profit_price, stop_loss_price = strategy.check_exit(position, current_price, df)
        
        assert should_exit, "价格跌破止损价应触发退出"
        assert 'stop_loss' in reason.lower() or 'stop' in reason.lower(), "退出原因应包含止损"

    def test_no_exit_above_stop(self):
        """价格在止损价之上不应退出"""
        strategy = self.create_strategy(atr_multiplier=2.0)
        position = self.create_position(cost_price=100, highest_price=110)
        df = self.create_price_data(base_price=100, volatility=2)

        stop_price = strategy.calculate_stop_loss(position, df)
        current_price = stop_price + 10  # 高于止损价

        should_exit, reason, atr, take_profit_price, stop_loss_price = strategy.check_exit(position, current_price, df)
        assert not should_exit, "价格高于止损价不应退出"


@pytest.mark.skip("hybrid_stop 已删除，仅保留 dual_chandelier")
class TestATRDynamicStrategy:
    """ATR动态策略测试（包含止盈止损）"""

    def create_strategy(self):
        config = {
            'atr_period': 14,
            'take_profit_multiplier': 3.0,
            'stop_loss_multiplier': 2.0,
            'chandelier_period': 22
        }
        return ATRDynamicStrategy(config)

    def test_take_profit_trigger(self):
        """止盈触发"""
        strategy = self.create_strategy()
        
        # 创建上涨趋势数据
        days = 30
        dates = pd.date_range(start='2024-01-01', periods=days, freq='D')
        close = np.linspace(100, 130, days)  # 持续上涨
        high = close + 2
        low = close - 2
        df = pd.DataFrame({'high': high, 'low': low, 'close': close}, index=dates)
        
        position = {'cost_price': 100, 'highest_price': 130}
        current_price = 130 - 15  # 从最高点回撤
        
        should_exit, reason, atr, take_profit_price, stop_loss_price = strategy.check_exit(position, current_price, df)
        
        # 大回撤可能触发止盈
        if should_exit:
            assert 'take_profit' in reason.lower() or 'stop_loss' in reason.lower()

    def test_stop_loss_before_take_profit(self):
        """止损优先于止盈"""
        strategy = self.create_strategy()
        
        days = 30
        dates = pd.date_range(start='2024-01-01', periods=days, freq='D')
        close = np.concatenate([np.linspace(100, 110, 15), np.linspace(110, 95, 15)])  # 先涨后跌
        high = close + 3
        low = close - 3
        df = pd.DataFrame({'high': high, 'low': low, 'close': close}, index=dates)
        
        position = {'cost_price': 100, 'highest_price': 110}
        current_price = 85  # 大幅跌破成本价
        
        should_exit, reason, atr, take_profit_price, stop_loss_price = strategy.check_exit(position, current_price, df)
        # 可能触发止损
        assert isinstance(should_exit, bool)


@pytest.mark.skip("hybrid_stop 已删除，仅保留 dual_chandelier")
class TestExitStrategyFactory:
    """策略工厂测试"""

    def test_create_atr_dynamic(self):
        """创建ATR动态策略"""
        config = {'atr_period': 14, 'take_profit_multiplier': 3.0}
        strategy = ExitStrategyFactory.create('atr_dynamic', config)
        assert isinstance(strategy, ATRDynamicStrategy)

    def test_create_atr_stop_only(self):
        """创建ATR止损策略"""
        config = {'atr_multiplier': 2.5, 'atr_period': 14}
        strategy = ExitStrategyFactory.create('atr_stop_only', config)
        assert isinstance(strategy, ATRStopOnlyStrategy)

    def test_invalid_strategy_type(self):
        """无效策略类型使用默认策略"""
        strategy = ExitStrategyFactory.create('invalid_type', {})
        # 应该返回默认的atr_dynamic策略
        assert isinstance(strategy, ATRDynamicStrategy)

    def test_list_strategies(self):
        """列出所有可用策略"""
        strategies = ExitStrategyFactory.list_strategies()
        
        assert isinstance(strategies, list)
        assert 'atr_dynamic' in strategies
        assert 'atr_stop_only' in strategies
        assert 'hybrid_stop' in strategies

    def test_register_new_strategy(self):
        """注册新策略"""
        class CustomStrategy(ExitStrategy):
            def calculate_stop_loss(self, position, df):
                return position['cost_price'] * 0.9
            
            def check_exit(self, position, current_price, df):
                return False, '', 0.0, 0.0, 0.0
        
        ExitStrategyFactory.register('custom', CustomStrategy)
        strategy = ExitStrategyFactory.create('custom', {})
        
        assert isinstance(strategy, CustomStrategy)
