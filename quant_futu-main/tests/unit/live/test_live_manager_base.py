"""
LiveTradingManager 单元测试
测试核心交易管理器的连接、选股、买入、卖出等关键逻辑
"""
import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))

from scripts.live_trading.live_manager_base import LiveTradingManager, TradingState
from mutifactor.trading import ConnectionState
import pandas as pd


class MockLiveTradingManager(LiveTradingManager):
    """Mock实现用于测试，覆盖抽象方法"""
    
    def _create_position_manager(self):
        return Mock()
    
    def _get_selection_time_window(self):
        from datetime import time
        return time(9, 30), time(16, 0)
    
    def _create_trader(self, futu_config, env):
        return Mock()
    
    def _get_stock_name(self, stock_code):
        return "Test Stock"
    
    def _get_data_fetcher_class(self):
        return Mock
    
    def _calculate_buy_quantity(self, stock_code, per_stock_capital,
                               current_price, total_remaining):
        return 100


class TestLiveTradingManagerConnect(unittest.TestCase):
    """测试连接方法"""
    
    def setUp(self):
        self.config = {
            'trading': {
                'futu': {'host': '127.0.0.1', 'port': 11111},
                'env': 'SIMULATE'
            }
        }
        self.manager = MockLiveTradingManager(self.config, 'HK')
    
    def test_connect_success_all_steps(self):
        """测试连接成功 - 所有步骤都通过"""
        # Mock price_fetcher
        mock_price_fetcher = Mock()
        mock_price_fetcher.connect.return_value = True
        mock_price_fetcher.get_current_price.return_value = 350.0
        self.manager.price_fetcher = mock_price_fetcher
        
        # Mock trader - 需要通过 _create_trader 返回
        mock_trader = Mock()
        mock_trader.connect.return_value = True
        mock_trader.get_account_info.return_value = {'cash': 500000, 'total_assets': 1000000}
        self.manager._create_trader = Mock(return_value=mock_trader)
        
        # Mock position_manager - 需要 _create_position_manager 返回
        mock_position_manager = Mock()
        mock_position_manager.sync_with_broker.return_value = True
        self.manager._create_position_manager = Mock(return_value=mock_position_manager)
        
        # Mock _shared_fetcher - 新增的共享数据获取器
        mock_fetcher_class = Mock()
        mock_fetcher_instance = Mock()
        mock_fetcher_instance.connect.return_value = True
        mock_fetcher_class.return_value = mock_fetcher_instance
        self.manager._get_data_fetcher_class = Mock(return_value=mock_fetcher_class)
        
        # 执行连接
        result = self.manager.connect()
        
        # 验证结果
        self.assertTrue(result)
        self.assertEqual(self.manager.connection_state, ConnectionState.CONNECTED)
        
        # 验证调用次数
        mock_price_fetcher.connect.assert_called_once()
        mock_trader.connect.assert_called_once()
        mock_position_manager.sync_with_broker.assert_called_once()
    
    def test_connect_price_fetcher_fail(self):
        """测试连接失败 - 行情服务连接失败"""
        mock_price_fetcher = Mock()
        mock_price_fetcher.connect.return_value = False
        self.manager.price_fetcher = mock_price_fetcher
        
        result = self.manager.connect()
        
        self.assertFalse(result)
    
    def test_connect_price_fetcher_test_fail(self):
        """测试连接失败 - 行情服务测试调用失败"""
        mock_price_fetcher = Mock()
        mock_price_fetcher.connect.return_value = True
        mock_price_fetcher.get_current_price.return_value = None
        self.manager.price_fetcher = mock_price_fetcher
        
        result = self.manager.connect()
        
        self.assertFalse(result)
    
    def test_connect_trader_fail(self):
        """测试连接失败 - 交易服务连接失败"""
        mock_price_fetcher = Mock()
        mock_price_fetcher.connect.return_value = True
        mock_price_fetcher.get_current_price.return_value = 350.0
        self.manager.price_fetcher = mock_price_fetcher
        
        mock_trader = Mock()
        mock_trader.connect.return_value = False
        self.manager.trader = mock_trader
        
        result = self.manager.connect()
        
        self.assertFalse(result)
    
    def test_connect_trader_test_fail(self):
        """测试连接失败 - 交易服务测试调用失败"""
        mock_price_fetcher = Mock()
        mock_price_fetcher.connect.return_value = True
        mock_price_fetcher.get_current_price.return_value = 350.0
        self.manager.price_fetcher = mock_price_fetcher
        
        mock_trader = Mock()
        mock_trader.connect.return_value = True
        mock_trader.get_account_info.return_value = {}  # 没有 cash 字段
        self.manager.trader = mock_trader
        
        result = self.manager.connect()
        
        self.assertFalse(result)
    
    def test_connect_sync_positions_fail(self):
        """测试连接 - 同步持仓失败但连接仍然成功"""
        mock_price_fetcher = Mock()
        mock_price_fetcher.connect.return_value = True
        mock_price_fetcher.get_current_price.return_value = 350.0
        self.manager.price_fetcher = mock_price_fetcher
        
        # Mock trader - 需要通过 _create_trader 返回
        mock_trader = Mock()
        mock_trader.connect.return_value = True
        mock_trader.get_account_info.return_value = {'cash': 500000, 'total_assets': 1000000}
        self.manager._create_trader = Mock(return_value=mock_trader)
        
        mock_position_manager = Mock()
        mock_position_manager.sync_with_broker.return_value = False
        self.manager._create_position_manager = Mock(return_value=mock_position_manager)
        
        # Mock _shared_fetcher - 新增的共享数据获取器
        mock_fetcher_class = Mock()
        mock_fetcher_instance = Mock()
        mock_fetcher_instance.connect.return_value = True
        mock_fetcher_class.return_value = mock_fetcher_instance
        self.manager._get_data_fetcher_class = Mock(return_value=mock_fetcher_class)
        
        result = self.manager.connect()
        
        # 同步持仓失败不影响连接成功
        self.assertTrue(result)
        self.assertEqual(self.manager.connection_state, ConnectionState.CONNECTED)
    
    @patch('scripts.live_trading.live_manager_base.logger')
    def test_connect_exception_handling(self, mock_logger):
        """测试连接异常处理"""
        # Mock price_fetcher 抛出异常
        mock_price_fetcher = Mock()
        mock_price_fetcher.connect.side_effect = Exception("Test error")
        self.manager.price_fetcher = mock_price_fetcher
        
        result = self.manager.connect()
        
        self.assertFalse(result)


class TestLiveTradingManagerSelection(unittest.TestCase):
    """测试选股逻辑"""
    
    def setUp(self):
        self.config = {
            'trading': {
                'futu': {'host': '127.0.0.1', 'port': 11111},
                'env': 'SIMULATE'
            },
            'momentum': {
                'max_positions': 3
            }
        }
        self.manager = MockLiveTradingManager(self.config, 'HK')
    
    @patch('mutifactor.strategies.momentum.MomentumStrategy')
    @patch('mutifactor.data.db_kline_cache.DBKlineCache')
    @patch('scripts.live_trading.live_manager_base.logger')
    def test_do_selection_success(self, mock_logger, mock_kline_cache_class, mock_strategy_class):
        """测试选股成功"""
        from datetime import time, date, datetime
        import pandas as pd
        
        # Mock _shared_fetcher
        mock_fetcher = Mock()
        mock_fetcher.get_blue_chip_stocks.return_value = ['HK.00700', 'HK.09988']
        mock_fetcher.fetch_multiple_stocks.return_value = {
            'HK.00700': pd.DataFrame({'close': [400, 410, 420]}),
            'HK.09988': pd.DataFrame({'close': [100, 105, 110]})
        }
        self.manager._shared_fetcher = mock_fetcher
        self.manager.kline_cache = {}  # 模拟空缓存，触发重拉
        
        # Mock DBKlineCache（新版代码已不再用，直接返回 None）
        mock_kline_cache_class.return_value = Mock()
        
        # Mock MomentumStrategy
        mock_strategy = Mock()
        mock_strategy.select_stocks.return_value = ['HK.00700', 'HK.09988']
        mock_strategy_class.return_value = mock_strategy
        
        # Mock state persistence
        self.manager.state_persistence = Mock()
        
        # 设置选股时间窗口
        self.manager.last_trading_date = None
        self.manager.cached_selected_stocks = None
        
        # 执行选股
        current_time = time(10, 0)
        current_timestamp = datetime.now().timestamp()
        current_date = date.today()
        self.manager._do_selection(current_time, current_timestamp, current_date)
        
        # 验证结果
        self.assertEqual(self.manager.cached_selected_stocks, ['HK.00700', 'HK.09988'])
        mock_fetcher.get_blue_chip_stocks.assert_called_once()
    
    @patch('scripts.live_trading.live_manager_base.logger')
    def test_do_selection_no_fetcher(self, mock_logger):
        """测试选股 - 没有共享数据获取器"""
        from datetime import time, date, datetime
        
        self.manager._shared_fetcher = None
        self.manager.state_persistence = Mock()
        
        current_time = time(10, 0)
        current_timestamp = datetime.now().timestamp()
        current_date = date.today()
        self.manager._do_selection(current_time, current_timestamp, current_date)
        
        # 应该记录错误
        mock_logger.error.assert_called()
    
    @patch('mutifactor.strategies.momentum.MomentumStrategy')
    @patch('mutifactor.data.db_kline_cache.DBKlineCache')
    @patch('scripts.live_trading.live_manager_base.logger')
    def test_do_selection_empty_result(self, mock_logger, mock_kline_cache_class, mock_strategy_class):
        """测试选股 - 选股结果为空"""
        from datetime import time, date, datetime
        import pandas as pd
        
        # Mock _shared_fetcher
        mock_fetcher = Mock()
        mock_fetcher.get_blue_chip_stocks.return_value = ['HK.00700']
        mock_fetcher.fetch_multiple_stocks.return_value = {
            'HK.00700': pd.DataFrame({'close': [400, 410, 420]})
        }
        self.manager._shared_fetcher = mock_fetcher
        self.manager.kline_cache = {}  # 模拟空缓存，触发重拉
        
        # Mock DBKlineCache（新版代码已不再用，直接返回 None）
        mock_kline_cache_class.return_value = Mock()
        
        # Mock MomentumStrategy - 返回空列表
        mock_strategy = Mock()
        mock_strategy.select_stocks.return_value = []
        mock_strategy_class.return_value = mock_strategy
        
        self.manager.state_persistence = Mock()
        self.manager.last_trading_date = None
        self.manager.cached_selected_stocks = None
        
        current_time = time(10, 0)
        current_timestamp = datetime.now().timestamp()
        current_date = date.today()
        self.manager._do_selection(current_time, current_timestamp, current_date)
        
        # 验证选股结果为空列表
        self.assertEqual(self.manager.cached_selected_stocks, [])
        mock_logger.warning.assert_called()
    
    @patch('scripts.live_trading.live_manager_base.logger')
    def test_do_selection_exception(self, mock_logger):
        """测试选股 - 异常处理"""
        from datetime import time, date, datetime
        
        # Mock _shared_fetcher 抛出异常
        mock_fetcher = Mock()
        mock_fetcher.get_blue_chip_stocks.side_effect = Exception("Test error")
        self.manager._shared_fetcher = mock_fetcher
        
        self.manager.state_persistence = Mock()
        self.manager.last_trading_date = None
        self.manager.cached_selected_stocks = None
        
        current_time = time(10, 0)
        current_timestamp = datetime.now().timestamp()
        current_date = date.today()
        self.manager._do_selection(current_time, current_timestamp, current_date)
        
        # 应该记录错误
        mock_logger.error.assert_called()


class TestLiveTradingManagerBuy(unittest.TestCase):
    """测试买入逻辑"""
    
    def setUp(self):
        self.config = {
            'trading': {
                'futu': {'host': '127.0.0.1', 'port': 11111},
                'env': 'SIMULATE'
            }
        }
        self.manager = MockLiveTradingManager(self.config, 'HK')
    
    def test_do_buy_no_selection(self):
        """测试买入 - 没有选股结果"""
        self.manager.cached_selected_stocks = []
        
        # Mock buy_timing
        self.manager.buy_timing = Mock()
        self.manager.buy_timing.should_buy_now.return_value = (False, "not_time", "none")
        
        self.manager._do_buy()
        # 无选股结果时直接返回
        self.manager.buy_timing.should_buy_now.assert_called_once()
    
    def test_do_buy_skip_already_in_position(self):
        """测试买入 - 跳过已在持仓的股票"""
        self.manager.cached_selected_stocks = ['HK.00700']
        
        # Mock buy_timing - 允许买入
        self.manager.buy_timing = Mock()
        self.manager.buy_timing.should_buy_now.return_value = (True, "test_reason", "bottom_fish")
        
        # Mock position_manager
        self.manager.position_manager = Mock()
        self.manager.position_manager.strategy_positions = {'HK.00700': {'quantity': 100}}
        
        # Mock _get_today_bought_stocks
        self.manager._get_today_bought_stocks = Mock(return_value=set())
        
        self.manager._do_buy()
        # 应该检查持仓并跳过
        self.assertTrue('HK.00700' in self.manager.position_manager.strategy_positions)
    
    def test_do_buy_skip_already_bought(self):
        """测试买入 - 跳过今日已买入的股票"""
        self.manager.cached_selected_stocks = ['HK.00700']
        
        # Mock buy_timing - 允许买入
        self.manager.buy_timing = Mock()
        self.manager.buy_timing.should_buy_now.return_value = (True, "test_reason", "bottom_fish")
        
        # Mock position_manager - 无持仓
        self.manager.position_manager = Mock()
        self.manager.position_manager.strategy_positions = {}
        
        # Mock _get_today_bought_stocks - 今日已买
        self.manager._get_today_bought_stocks = Mock(return_value={'HK.00700'})
        
        self.manager._do_buy()
        # 应该检查今日已买并跳过
        self.manager._get_today_bought_stocks.assert_called_once()
    
    @patch('scripts.live_trading.live_manager_base.logger')
    def test_do_buy_not_time(self, mock_logger):
        """测试买入 - 非买入时间"""
        self.manager.cached_selected_stocks = ['HK.00700']
        
        # Mock buy_timing - 不允许买入
        self.manager.buy_timing = Mock()
        self.manager.buy_timing.should_buy_now.return_value = (False, "not_time", "none")
        
        self.manager._do_buy()
        # 不应该执行买入逻辑
        self.manager.buy_timing.should_buy_now.assert_called_once()
    
    @patch('scripts.live_trading.live_manager_base.logger')
    def test_do_buy_price_fetch_fail(self, mock_logger):
        """测试买入 - 获取价格失败"""
        self.manager.cached_selected_stocks = ['HK.00700']
        
        # Mock buy_timing - 允许买入
        self.manager.buy_timing = Mock()
        self.manager.buy_timing.should_buy_now.return_value = (True, "test_reason", "bottom_fish")
        
        # Mock position_manager
        self.manager.position_manager = Mock()
        self.manager.position_manager.strategy_positions = {}
        self.manager.position_manager.recently_stopped = {}  # 止损冷却期记录
        self.manager.position_manager.get_remaining_capital.return_value = 10000.0
        self.manager.position_manager.execute_buy.return_value = False
        
        # Mock _get_today_bought_stocks
        self.manager._get_today_bought_stocks = Mock(return_value=set())
        
        # Mock price_fetcher
        self.manager.price_fetcher = Mock()
        self.manager.price_fetcher.get_current_price.return_value = None
        
        self.manager._do_buy()
        # 获取价格失败时不应该执行买入
        self.manager.buy_timing.should_buy_now.assert_called_once()
    
    @patch('scripts.live_trading.live_manager_base.logger')
    def test_do_buy_execute_fail(self, mock_logger):
        """测试买入 - 执行买入失败"""
        self.manager.cached_selected_stocks = ['HK.00700']
        
        # Mock buy_timing - 允许买入
        self.manager.buy_timing = Mock()
        self.manager.buy_timing.should_buy_now.return_value = (True, "test_reason", "bottom_fish")
        
        # Mock position_manager
        self.manager.position_manager = Mock()
        self.manager.position_manager.strategy_positions = {}
        self.manager.position_manager.recently_stopped = {}  # 止损冷却期记录
        self.manager.position_manager.get_remaining_capital.return_value = 10000.0
        self.manager.position_manager.execute_buy.return_value = False
        
        # Mock _get_today_bought_stocks
        self.manager._get_today_bought_stocks = Mock(return_value=set())
        
        # Mock price_fetcher
        self.manager.price_fetcher = Mock()
        self.manager.price_fetcher.get_current_price.return_value = 100.0
        
        # Mock _calculate_buy_quantity
        self.manager._calculate_buy_quantity = Mock(return_value=100)
        
        self.manager._do_buy()
        # 应该尝试买入
        self.manager.buy_timing.should_buy_now.assert_called_once()


class TestLiveTradingManagerState(unittest.TestCase):
    """测试状态管理"""
    
    def setUp(self):
        self.config = {
            'trading': {
                'futu': {'host': '127.0.0.1', 'port': 11111},
                'env': 'SIMULATE'
            }
        }
        self.manager = MockLiveTradingManager(self.config, 'HK')
    
    def test_pause_resume(self):
        """测试暂停和恢复"""
        # 初始状态是 STOPPED
        self.assertEqual(self.manager.state, TradingState.STOPPED)
        
        # 设置为 RUNNING 状态后才能暂停
        self.manager.state = TradingState.RUNNING
        self.manager.pause()
        self.assertEqual(self.manager.state, TradingState.PAUSED)
        
        self.manager.resume()
        self.assertEqual(self.manager.state, TradingState.RUNNING)
    
    def test_check_connection_connected(self):
        """测试连接检查 - 已连接"""
        self.manager.connection_state = ConnectionState.CONNECTED
        self.manager.retry_count = 0
        
        result = self.manager.check_connection()
        
        self.assertTrue(result)
        self.assertEqual(self.manager.retry_count, 0)
    
    @patch('scripts.live_trading.live_manager_base.logger')
    @patch('scripts.live_trading.live_manager_base.time.sleep')
    def test_check_connection_reconnect(self, mock_sleep, mock_logger):
        """测试连接检查 - 触发重连"""
        self.manager.connection_state = ConnectionState.DISCONNECTED
        self.manager.retry_count = 0
        
        # Mock connect方法 - 需要正确设置 connection_state
        def mock_connect():
            self.manager.connection_state = ConnectionState.CONNECTED
            return True
        self.manager.connect = Mock(side_effect=mock_connect)
        
        result = self.manager.check_connection()
        
        self.assertTrue(result)
        self.assertEqual(self.manager.connection_state, ConnectionState.CONNECTED)
        mock_sleep.assert_called_once()
    
    def test_check_connection_max_retry(self):
        """测试连接检查 - 达到最大重试次数"""
        self.manager.connection_state = ConnectionState.DISCONNECTED
        self.manager._connection_retry_count = 10  # 超过最大重试次数
        
        result = self.manager.check_connection()
        
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()
