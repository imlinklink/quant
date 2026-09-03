"""
回测引擎单元测试
"""
import pytest
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock
from scripts.backtest.backtest_engine import BacktestEngine


class TestBacktestEngine:
    """回测引擎测试"""

    @pytest.fixture
    def engine(self):
        """创建回测引擎实例"""
        config = {
            'strategy': {
                'initial_capital': 100000,
                'start_date': '2024-01-01',
                'end_date': '2024-12-31'
            },
            'momentum': {
                'momentum_window': 120,
                'rsrs_window': 18
            },
            'risk': {
                'exit_strategy': 'atr_dynamic',
                'atr_period': 14
            }
        }
        logger = logging.getLogger('test_backtest')
        logger.setLevel(logging.DEBUG)
        return BacktestEngine(config, logger)

    def test_init(self, engine):
        """测试初始化"""
        assert engine.config is not None
        assert engine.logger is not None
        assert engine.fetcher is None

    @patch('scripts.backtest.backtest_engine.BacktestRunner')
    @patch('scripts.backtest.backtest_engine.prepare_data')
    @patch.object(BacktestEngine, '_fetch_price_data')
    def test_run_success(self, mock_fetch, mock_prepare, mock_runner_class, engine):
        """测试回测成功执行"""
        # Mock数据获取
        mock_fetch.return_value = {
            'HK.00700': pd.DataFrame({
                'date': pd.date_range('2024-01-01', periods=100),
                'open': np.random.randn(100) + 100,
                'high': np.random.randn(100) + 101,
                'low': np.random.randn(100) + 99,
                'close': np.random.randn(100) + 100,
                'volume': np.random.randint(1000000, 10000000, 100)
            })
        }

        # Mock数据准备
        mock_prepare.return_value = mock_fetch.return_value

        # Mock回测运行器
        mock_runner = Mock()
        mock_runner.run_backtest.return_value = {
            'total_return': 0.15,
            'sharpe_ratio': 1.5
        }
        mock_runner_class.return_value = mock_runner

        # 执行回测
        results = engine.run(
            stock_codes=['HK.00700'],
            start_date='2024-01-01',
            end_date='2024-12-31'
        )

        # 验证
        assert results is not None
        assert 'total_return' in results
        mock_fetch.assert_called_once()

    def test_run_no_stock_codes(self, engine):
        """测试无股票代码"""
        # 使用空股票列表
        results = engine.run(stock_codes=[])
        assert results is None

    @patch.object(BacktestEngine, '_fetch_price_data')
    def test_run_no_data(self, mock_fetch, engine):
        """测试无数据"""
        mock_fetch.return_value = {}

        results = engine.run(
            stock_codes=['HK.00700'],
            start_date='2024-01-01',
            end_date='2024-12-31'
        )

        assert results is None

    @patch.object(BacktestEngine, '_fetch_price_data')
    @patch('scripts.backtest.backtest_engine.prepare_data')
    def test_run_prepare_data_failure(self, mock_prepare, mock_fetch, engine):
        """测试数据准备失败"""
        mock_fetch.return_value = {'HK.00700': pd.DataFrame()}
        mock_prepare.return_value = None

        results = engine.run(
            stock_codes=['HK.00700'],
            start_date='2024-01-01',
            end_date='2024-12-31'
        )

        assert results is None


class TestBacktestEngineConfig:
    """配置测试"""

    def test_config_validation(self):
        """测试配置验证"""
        config = {
            'strategy': {
                'initial_capital': 100000,
                'start_date': '2024-01-01',
                'end_date': '2024-12-31'
            }
        }
        logger = logging.getLogger('test_config')
        engine = BacktestEngine(config, logger)

        assert engine.config['strategy']['initial_capital'] == 100000

    def test_default_dates(self):
        """测试默认日期"""
        config = {}
        logger = logging.getLogger('test_dates')
        engine = BacktestEngine(config, logger)

        # 验证引擎创建成功
        assert engine.config == {}


class TestBacktestEngineLogging:
    """日志测试"""

    def test_logger_usage(self):
        """测试日志器使用"""
        config = {}
        logger = logging.getLogger('test_logger')
        logger.setLevel(logging.INFO)

        # 添加handler以捕获日志
        handler = logging.StreamHandler()
        logger.addHandler(handler)

        engine = BacktestEngine(config, logger)

        # 验证logger已设置
        assert engine.logger == logger
