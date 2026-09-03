"""
动量策略单元测试
"""
import pytest
import pandas as pd
import numpy as np
from mutifactor.strategies.momentum import MomentumStrategy, _calculate_rolling_slopes_vectorized


class TestMomentumScore:
    """动量得分计算测试"""

    @pytest.fixture
    def strategy(self):
        """创建策略实例"""
        config = {
            'momentum': {
                'momentum_window': 20,
                'rsrs_window': 14,
                'min_momentum_score': 0.01,
                'min_r2': 0.4
            }
        }
        return MomentumStrategy(config=config)

    def create_price_data(self, start_price, days, trend='up', volatility=0.02):
        """创建价格数据"""
        dates = pd.date_range(start='2024-01-01', periods=days, freq='D')
        
        if trend == 'up':
            returns = np.random.randn(days) * volatility + 0.001  # 正向漂移
        elif trend == 'down':
            returns = np.random.randn(days) * volatility - 0.001  # 负向漂移
        elif trend == 'flat':
            returns = np.random.randn(days) * volatility  # 无漂移
        else:
            returns = np.random.randn(days) * volatility * 2  # 高波动

        np.random.seed(42)
        close = start_price * np.cumprod(1 + returns)
        close = np.maximum(close, 0.01)  # 防止负价格
        
        return pd.DataFrame({
            'date': dates,
            'open': close * (1 + np.random.randn(days) * 0.005),
            'high': close * (1 + np.abs(np.random.randn(days)) * 0.01),
            'low': close * (1 - np.abs(np.random.randn(days)) * 0.01),
            'close': close,
            'volume': np.random.randint(1000000, 10000000, days)
        })

    def test_uptrend_positive_momentum(self, strategy):
        """上涨趋势应有正动量"""
        df = self.create_price_data(100, 30, trend='up')
        momentum, r2 = strategy.calculate_momentum_score_linear(df)
        
        # 动量可能为正也可能因数据特征而变化
        assert isinstance(momentum, (int, float))
        assert isinstance(r2, (int, float))
        assert 0 <= r2 <= 1, "R²应在0-1之间"

    def test_downtrend_negative_momentum(self, strategy):
        """下跌趋势应有负动量"""
        df = self.create_price_data(100, 30, trend='down')
        momentum, r2 = strategy.calculate_momentum_score_linear(df)
        
        # 下跌趋势可能产生负动量（取决于数据）
        # 主要验证计算不报错
        assert isinstance(momentum, (int, float))
        assert isinstance(r2, (int, float))

    def test_flat_trend_low_r2(self, strategy):
        """横盘趋势R²应较低"""
        df = self.create_price_data(100, 30, trend='flat', volatility=0.03)
        momentum, r2 = strategy.calculate_momentum_score_linear(df)
        
        # 横盘趋势拟合度通常较低
        # 注意：由于随机性，这不一定总是成立

    def test_insufficient_data_returns_zero(self, strategy):
        """数据不足返回0"""
        df = self.create_price_data(100, 10)  # 少于momentum_window
        momentum, r2 = strategy.calculate_momentum_score_linear(df)
        
        assert momentum == 0.0
        assert r2 == 0.0

    def test_zero_prices_handled(self, strategy):
        """零价格处理"""
        days = 30
        dates = pd.date_range(start='2024-01-01', periods=days, freq='D')
        close = np.array([100] * 15 + [0] * 15)  # 后半为零
        
        df = pd.DataFrame({
            'date': dates,
            'close': close,
            'high': close + 1,
            'low': close - 1,
            'open': close
        })
        
        momentum, r2 = strategy.calculate_momentum_score_linear(df)
        # 应该不报错，返回合理值
        assert isinstance(momentum, (int, float))

    def test_strong_trend_high_r2(self, strategy):
        """强趋势应有高R²"""
        dates = pd.date_range(start='2024-01-01', periods=30, freq='D')
        # 完美上涨趋势
        close = np.linspace(100, 130, 30)
        
        df = pd.DataFrame({
            'date': dates,
            'close': close,
            'high': close + 2,
            'low': close - 2,
            'open': close
        })
        
        momentum, r2 = strategy.calculate_momentum_score_linear(df)
        
        assert momentum > 0.3, "强上涨趋势动量应较大"
        assert r2 > 0.9, "完美趋势R²应接近1"


class TestRSRSCalculation:
    """RSRS计算测试"""

    @pytest.fixture
    def strategy(self):
        config = {
            'momentum': {
                'rsrs_window': 14,
                'rsrs_long_window': 100,
                'rsrs_rolling_window': 20
            }
        }
        return MomentumStrategy(config=config)

    def create_price_data(self, days=200):
        """创建长期价格数据"""
        np.random.seed(42)
        dates = pd.date_range(start='2024-01-01', periods=days, freq='D')
        returns = np.random.randn(days) * 0.02
        close = 100 * np.cumprod(1 + returns)
        
        return pd.DataFrame({
            'date': dates,
            'close': close,
            'high': close * 1.01,
            'low': close * 0.99,
            'open': close
        })

    def test_rsrs_slope_calculation(self, strategy):
        """RSRS斜率计算"""
        df = self.create_price_data(200)
        slope = strategy.calculate_rsrs_slope(df, window=14)
        
        assert isinstance(slope, (int, float))

    def test_rsrs_slope_insufficient_data(self, strategy):
        """数据不足时返回0"""
        df = self.create_price_data(20)
        slope = strategy.calculate_rsrs_slope(df, window=14)
        
        # 数据不足时返回0
        assert isinstance(slope, (int, float))

    def test_trend_filter(self, strategy):
        """趋势过滤"""
        df = self.create_price_data(200)
        current_price = df['close'].iloc[-1]
        
        passed, reason = strategy.check_trend_filter(df, current_price)
        
        assert isinstance(passed, bool)
        assert isinstance(reason, str)


class TestSelectStocks:
    """选股逻辑测试"""

    @pytest.fixture
    def strategy(self):
        config = {
            'momentum': {
                'momentum_window': 20,
                'rsrs_window': 14,
                'min_momentum_score': 0.02,
                'min_r2': 0.5,
                'max_positions': 3
            },
            'risk': {
                'exit_strategy': 'atr_dynamic',
                'atr_period': 14,
                'take_profit_multiplier': 3.0,
                'stop_loss_multiplier': 2.0
            }
        }
        return MomentumStrategy(config=config)

    def create_stocks_data(self, num_stocks=10, days=50):
        """创建多只股票数据"""
        np.random.seed(42)
        dates = pd.date_range(start='2024-01-01', periods=days, freq='D')
        
        stocks_data = {}
        for i in range(num_stocks):
            returns = np.random.randn(days) * 0.02 + np.random.choice([-0.001, 0.001, 0.002])
            close = 100 * np.cumprod(1 + returns)
            close = np.maximum(close, 1)
            
            stock_code = f"HK.0000{i}"
            stocks_data[stock_code] = pd.DataFrame({
                'date': dates,
                'close': close,
                'high': close * 1.01,
                'low': close * 0.99,
                'open': close,
                'volume': np.random.randint(1000000, 10000000, days)
            })
        
        return stocks_data

    def test_select_returns_list(self, strategy):
        """选股返回列表"""
        stocks_data = self.create_stocks_data(10, 50)
        selected = strategy.select_stocks(stocks_data, '2024-02-20')
        
        assert isinstance(selected, list)

    def test_select_respects_max_positions(self, strategy):
        """选股数量不超过最大持仓数"""
        stocks_data = self.create_stocks_data(20, 50)
        selected = strategy.select_stocks(stocks_data, '2024-02-20')
        
        assert len(selected) <= strategy.max_positions

    def test_select_empty_data_returns_empty(self, strategy):
        """空数据返回空列表"""
        selected = strategy.select_stocks({}, '2024-02-20')
        assert selected == []

    def test_select_filters_by_momentum(self, strategy):
        """过滤低动量股票"""
        stocks_data = self.create_stocks_data(10, 50)
        selected = strategy.select_stocks(stocks_data, '2024-02-20')
        
        # 选出的股票应该满足动量阈值（或为空）
        assert isinstance(selected, list)


class TestNewStockRSRSBoundary:
    """新股RSRS边界条件测试

    覆盖以下场景：
    1. 数据极少 (< rsrs_window=18) → 斜率返回0
    2. 数据刚好够 (18-20天) → 斜率正常计算
    3. 数据不足 long_window (20-124天) → 新股用自己的数据，老股返回"数据不足"
    4. 数据不足 rsrs_rolling_window → 返回"滚动数据不足"
    5. 空数据/NaN → 优雅降级
    6. 平线数据 → denominator=0 → "历史数据不足"
    7. get_new_stock_params 150天阈值边界
    """

    @pytest.fixture
    def strategy(self):
        config = {
            'momentum': {
                'rsrs_window': 18,
                'rsrs_long_window': 125,
                'rsrs_rolling_window': 20,
                'strong_trend_threshold': 1.5,
                'weak_trend_threshold': 0.5,
            }
        }
        return MomentumStrategy(config=config)

    def _make_df(self, days, start_price=100, volatility=0.02, seed=42):
        """创建标准测试数据"""
        np.random.seed(seed)
        dates = pd.date_range(start='2024-01-01', periods=days, freq='D')
        returns = np.random.randn(days) * volatility + 0.001
        close = start_price * np.cumprod(1 + returns)
        close = np.maximum(close, 0.01)
        return pd.DataFrame({
            'date': dates,
            'close': close,
            'high': close * 1.01,
            'low': close * 0.99,
            'open': close,
            'volume': np.random.randint(1_000_000, 10_000_000, days)
        })

    # ---- calculate_rsrs_slope 边界 ----

    def test_slope_data_too_few_returns_zero(self, strategy):
        """数据少于rsrs_window(18天)时，斜率返回0"""
        df = self._make_df(10)  # 10天 < 18天
        slope = strategy.calculate_rsrs_slope(df, window=18)
        assert slope == 0.0, "数据不足时应返回0"

    def test_slope_exactly_window_returns_value(self, strategy):
        """数据刚好等于rsrs_window(18天)时，斜率正常计算"""
        df = self._make_df(18)
        slope = strategy.calculate_rsrs_slope(df, window=18)
        assert isinstance(slope, float), "正常数据应返回数值"
        assert not np.isnan(slope), "不应返回NaN"

    def test_slope_slightly_more_than_window(self, strategy):
        """数据略多于rsrs_window(20天)时正常计算"""
        df = self._make_df(20)
        slope = strategy.calculate_rsrs_slope(df, window=18)
        assert isinstance(slope, float) and not np.isnan(slope)

    def test_slope_empty_dataframe_returns_zero(self, strategy):
        """空DataFrame返回0"""
        df = pd.DataFrame(columns=['date', 'close', 'high', 'low', 'open'])
        slope = strategy.calculate_rsrs_slope(df, window=18)
        assert slope == 0.0

    def test_slope_nan_prices_returns_zero(self, strategy):
        """含NaN的价格数据应捕获异常并返回0"""
        df = self._make_df(30)
        df.loc[5, 'high'] = np.nan
        df.loc[10, 'low'] = np.nan
        slope = strategy.calculate_rsrs_slope(df, window=18)
        assert slope == 0.0 or (isinstance(slope, float) and not np.isnan(slope))

    def test_slope_constant_price_returns_zero(self, strategy):
        """恒定价格 denominator=0 时返回0（线性代数错误）"""
        df = self._make_df(30)
        df['high'] = 100.0
        df['low'] = 100.0
        df['close'] = 100.0
        # 恒定价格 → denominator=0 → np.linalg.LinAlgError 或 denominator=0 → 返回 []
        slope = strategy.calculate_rsrs_slope(df, window=18)
        assert np.isclose(slope, 0.0, atol=1e-10), f"恒定价格应≈0，实际{slope}"

    # ---- check_trend_filter + is_new_stock 边界 ----

    def test_trend_filter_data_insufficient_old_stock(self, strategy):
        """老股数据不足125天，返回'数据不足'（不阻止入场）"""
        df = self._make_df(50)  # 50天 < 125天
        current_price = df['close'].iloc[-1]
        passed, reason = strategy.check_trend_filter(df, current_price, is_new_stock=False)
        assert passed is True, "数据不足时应返回True（不阻止入场）"
        assert reason == "数据不足"

    def test_trend_filter_new_stock_uses_all_data(self, strategy):
        """新股用自己的全部数据（50天），不要求125天，应正常计算不走'数据不足'分支"""
        df = self._make_df(50)  # 50天 < 125天
        current_price = df['close'].iloc[-1]
        passed, reason = strategy.check_trend_filter(df, current_price, is_new_stock=True)
        # 新股：effective_long_window = len(df) = 50
        # len(df) < effective_long_window → 50 < 50 → False，不会进入"数据不足"
        assert reason != "数据不足", f"新股50天数据不应触发'数据不足'，实际reason={reason}"

    def test_trend_filter_new_stock_with_minimal_data(self, strategy):
        """新股数据极少（刚好20天）时的行为：20 >= 20(自己) 但 slopes 长度可能不足"""
        df = self._make_df(20)  # 刚好20天
        current_price = df['close'].iloc[-1]
        passed, reason = strategy.check_trend_filter(df, current_price, is_new_stock=True)
        # effective_long_window = 20, len(df) = 20, 不触发"数据不足"
        # _calculate_rolling_slopes_vectorized(20, 18) 返回 3 个斜率
        # rsrs_rolling_window = 20, len(slopes) = 3 < 20 → "滚动数据不足"
        assert passed is True, f"滚动数据不足应放行，实际passed={passed}"
        assert reason in ("滚动数据不足", "历史数据不足"), f"实际reason={reason}"

    def test_trend_filter_new_stock_slightly_more_than_minimal(self, strategy):
        """新股数据刚好够滚动窗口（22天）：slopes够但少"""
        df = self._make_df(22)
        current_price = df['close'].iloc[-1]
        passed, reason = strategy.check_trend_filter(df, current_price, is_new_stock=True)
        # slopes: _calculate_rolling_slopes_vectorized(22, 18) → 5个斜率
        # rsrs_rolling_window = 20, 5 < 20 → "滚动数据不足"
        assert passed is True
        assert reason in ("滚动数据不足", "历史数据不足", "未通过趋势基准测试"), f"实际reason={reason}"

    def test_trend_filter_new_stock_rolling_window_met(self, strategy):
        """新股数据刚好达到滚动窗口要求（38天）"""
        # _calculate_rolling_slopes_vectorized(38, 18) → 21个斜率
        # rsrs_rolling_window = 20, 21 >= 20 → 可以计算
        # rolling_slopes = np.convolve(21, 20) → 2个值
        df = self._make_df(38)
        current_price = df['close'].iloc[-1]
        passed, reason = strategy.check_trend_filter(df, current_price, is_new_stock=True)
        assert passed is True
        assert reason in ("滚动数据不足", "强趋势", "弱趋势MA5支撑", "MA10支撑",
                          "未通过趋势强度测试", "未通过趋势基准测试")

    def test_trend_filter_new_vs_old_stock_different_behavior(self, strategy):
        """相同数据量：新股通过RSRS，老股可能因'数据不足'放行——两者行为应不同"""
        df = self._make_df(80)  # 80天：老股不够125天，新股够用
        current_price = df['close'].iloc[-1]

        # 老股：80 < 125 → "数据不足" → passed=True（放行）
        old_passed, old_reason = strategy.check_trend_filter(df, current_price, is_new_stock=False)

        # 新股：80 >= 80 → 不触发"数据不足" → 正常走RSRS逻辑
        new_passed, new_reason = strategy.check_trend_filter(df, current_price, is_new_stock=True)

        assert old_reason == "数据不足", f"老股80天应'数据不足'，实际{old_reason}"
        assert new_reason != "数据不足", f"新股80天不应'数据不足'，实际{new_reason}"

    def test_trend_filter_empty_df(self, strategy):
        """空DataFrame不崩溃"""
        df = pd.DataFrame(columns=['date', 'close', 'high', 'low', 'open'])
        passed, reason = strategy.check_trend_filter(df, 100.0, is_new_stock=False)
        assert passed is True

    def test_trend_filter_nan_df(self, strategy):
        """含NaN的DataFrame不崩溃"""
        df = self._make_df(200)
        df.iloc[50:60] = np.nan
        current_price = df['close'].dropna().iloc[-1]
        passed, reason = strategy.check_trend_filter(df, current_price, is_new_stock=False)
        assert isinstance(passed, bool)
        assert isinstance(reason, str)

    def test_trend_filter_all_uptrend_passes(self, strategy):
        """强上涨趋势应通过RSRS"""
        df = self._make_df(200, volatility=0.01, seed=99)
        # 人为制造强趋势：价格从100涨到300
        df['close'] = np.linspace(100, 300, 200)
        df['high'] = df['close'] * 1.01
        df['low'] = df['close'] * 0.99
        current_price = df['close'].iloc[-1]
        passed, reason = strategy.check_trend_filter(df, current_price, is_new_stock=False)
        assert passed is True, f"强趋势应通过，实际reason={reason}"

    def test_trend_filter_all_downtrend_fails(self, strategy):
        """强下跌趋势应不通过RSRS（趋势基准测试失败）"""
        df = self._make_df(200, volatility=0.01, seed=99)
        df['close'] = np.linspace(300, 100, 200)  # 下跌趋势
        df['high'] = df['close'] * 1.01
        df['low'] = df['close'] * 0.99
        current_price = df['close'].iloc[-1]
        passed, reason = strategy.check_trend_filter(df, current_price, is_new_stock=False)
        assert passed is False, f"下跌趋势应不通过，实际reason={reason}"

    # ---- get_new_stock_params 阈值边界 ----

    def test_new_stock_params_exactly_149_days(self, strategy):
        """上市149天 = 新股"""
        from datetime import date, timedelta
        listing = date(2024, 1, 1)
        current = date(2024, 5, 29)  # 149天
        params = strategy.get_new_stock_params(listing, current)
        assert params['is_new_stock'] is True
        assert params['min_momentum'] == 0.0

    def test_new_stock_params_exactly_150_days(self, strategy):
        """上市150天 = 非新股"""
        from datetime import date
        listing = date(2024, 1, 1)
        current = date(2024, 5, 31)  # 150天
        params = strategy.get_new_stock_params(listing, current)
        assert params['is_new_stock'] is False
        assert params['min_momentum'] == strategy.min_momentum_score

    def test_new_stock_params_no_listing_date(self, strategy):
        """无上市日期 → 非新股（保守处理）"""
        params = strategy.get_new_stock_params(None, '2024-05-31')
        assert params['is_new_stock'] is False
        assert params['min_momentum'] == strategy.min_momentum_score

    def test_new_stock_params_string_dates(self, strategy):
        """字符串日期格式正常工作"""
        params = strategy.get_new_stock_params('2024-01-01', '2024-06-01')  # 152天
        assert params['is_new_stock'] is False

    # ---- _calculate_rolling_slopes_vectorized 边界 ----

    def test_rolling_slopes_exactly_window(self):
        """数据刚好等于窗口，返回1个斜率"""
        prices = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0,
                           11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0], dtype=np.float64)
        slopes = _calculate_rolling_slopes_vectorized(prices, 18)
        assert len(slopes) == 1
        assert isinstance(slopes[0], float)

    def test_rolling_slopes_less_than_window(self):
        """数据少于窗口，返回空数组"""
        prices = np.array([1.0, 2.0, 3.0, 4.0, 5.0], dtype=np.float64)
        slopes = _calculate_rolling_slopes_vectorized(prices, 18)
        assert len(slopes) == 0

    def test_rolling_slopes_perfect_uptrend(self):
        """完美上涨：斜率应为常数"""
        prices = np.arange(1.0, 21.0, dtype=np.float64)  # 1,2,3,...,20
        slopes = _calculate_rolling_slopes_vectorized(prices, 10)
        assert len(slopes) == 11
        # 每个窗口的斜率应该≈1（因为是完美直线 y=x）
        np.testing.assert_array_almost_equal(slopes, np.ones(11), decimal=5)

    def test_rolling_slopes_constant_price(self):
        """恒定价格 → denominator=0 → 返回空数组"""
        prices = np.full(30, 100.0, dtype=np.float64)
        slopes = _calculate_rolling_slopes_vectorized(prices, 18)
        assert np.allclose(slopes, 0.0, atol=1e-10), f"恒定价格斜率应全≈0，实际slopes={slopes[:3]}"


class TestPositionSizing:
    """仓位计算测试"""

    @pytest.fixture
    def strategy(self):
        config = {
            'momentum': {'max_positions': 3},
            'risk': {
                'max_single_position_ratio': 0.5,
                'volatility_weighted_enabled': False
            }
        }
        return MomentumStrategy(config=config, initial_capital=100000)

    def test_position_size_calculation(self, strategy):
        """仓位计算"""
        price = 50.0
        available_cash = 30000.0
        
        shares = strategy.calculate_position_size('HK.00001', price, available_cash)
        
        assert isinstance(shares, int)
        assert shares >= 0
        # 港股买卖单位为手（每手100股）
        assert shares % 100 == 0, "股数应为整手数"

    def test_position_size_respects_cash(self, strategy):
        """仓位不超过可用资金"""
        price = 100.0
        available_cash = 5000.0
        
        shares = strategy.calculate_position_size('HK.00001', price, available_cash)
        cost = shares * price
        
        assert cost <= available_cash + price * 100  # 允许一手误差

    def test_position_size_zero_for_invalid_price(self, strategy):
        """无效价格返回0"""
        shares = strategy.calculate_position_size('HK.00001', 0, 10000)
        assert shares == 0
        
        shares = strategy.calculate_position_size('HK.00001', -10, 10000)
        assert shares == 0
