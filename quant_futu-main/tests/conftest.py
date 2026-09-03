"""
pytest 配置文件
提供共享的fixtures和测试配置
"""
import pytest
import pandas as pd
import numpy as np
import os
import sys
from pathlib import Path
from datetime import date

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# ==================== 全局配置 ====================

def pytest_configure(config):
    """pytest配置"""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )
    config.addinivalue_line(
        "markers", "unit: marks tests as unit tests"
    )


def pytest_collection_modifyitems(config, items):
    """修改测试集合"""
    # 为所有测试添加默认标记
    for item in items:
        if "slow" not in item.keywords:
            item.add_marker(pytest.mark.unit)


# ==================== 通用Fixtures ====================

@pytest.fixture
def sample_stock_codes():
    """示例股票代码列表"""
    return ['HK.00700', 'HK.00005', 'HK.00941', 'HK.01919', 'HK.09866']


@pytest.fixture
def sample_dates():
    """示例日期范围"""
    start_date = date(2024, 1, 1)
    end_date = date(2024, 1, 31)
    return start_date, end_date


@pytest.fixture
def sample_price_data():
    """示例价格数据"""
    np.random.seed(42)
    dates = pd.date_range(start='2024-01-01', periods=100, freq='D')

    data = {}
    for i, code in enumerate(['HK.00001', 'HK.00002', 'HK.00003']):
        returns = np.random.randn(100) * 0.02
        close = (100 + i * 10) * np.cumprod(1 + returns)

        data[code] = pd.DataFrame({
            'date': dates,
            'open': close * (1 + np.random.randn(100) * 0.005),
            'high': close * (1 + np.abs(np.random.randn(100)) * 0.01),
            'low': close * (1 - np.abs(np.random.randn(100)) * 0.01),
            'close': close,
            'volume': np.random.randint(1000000, 10000000, 100)
        })

    return data


@pytest.fixture
def sample_kline_data():
    """示例K线数据"""
    dates = pd.date_range(start='2024-01-01', periods=30, freq='D')
    np.random.seed(42)

    return pd.DataFrame({
        'date': dates,
        'open': np.random.uniform(100, 110, 30),
        'high': np.random.uniform(110, 120, 30),
        'low': np.random.uniform(90, 100, 30),
        'close': np.random.uniform(100, 110, 30),
        'volume': np.random.randint(1000000, 10000000, 30)
    })


@pytest.fixture
def sample_position():
    """示例持仓数据"""
    return {
        'stock_code': 'HK.00700',
        'quantity': 1000,
        'cost_price': 350.0,
        'current_price': 360.0,
        'highest_price': 365.0,
        'buy_date': '2024-01-15',
        'direction': 'long'
    }


@pytest.fixture
def sample_config():
    """示例配置"""
    return {
        'strategy': {
            'name': 'momentum',
            'initial_capital': 100000,
            'max_positions': 5
        },
        'trading': {
            'live_trading': {
                'enabled': True,
                'order_timeout': 60,
                'position_check_interval': 30
            }
        },
        'risk': {
            'stop_loss': 0.05,
            'take_profit': 0.10,
            'max_single_position_ratio': 0.5
        }
    }


# ==================== Mock Fixtures ====================

@pytest.fixture
def mock_db_connection():
    """模拟数据库连接"""
    from unittest.mock import Mock, MagicMock

    mock_conn = Mock()
    mock_cursor = Mock()
    mock_conn.cursor.return_value = mock_cursor

    return mock_conn, mock_cursor


@pytest.fixture
def mock_api_response():
    """模拟API响应"""
    return {
        'ret': 0,
        'msg': 'success',
        'data': {
            'stock_code': 'HK.00700',
            'price': 350.0
        }
    }


# ==================== 临时文件Fixtures ====================

@pytest.fixture
def temp_config_file(tmp_path, sample_config):
    """临时配置文件"""
    import yaml

    config_file = tmp_path / "test_config.yaml"
    with open(config_file, 'w') as f:
        yaml.dump(sample_config, f)

    return config_file


@pytest.fixture
def temp_log_file(tmp_path):
    """临时日志文件"""
    log_file = tmp_path / "test.log"
    return log_file



# ==================== 交易相关Fixtures ====================

@pytest.fixture
def mock_trader():
    """模拟交易接口"""
    from unittest.mock import Mock

    mock_trader = Mock()
    mock_trader.place_order.return_value = ('ORDER123', 350.5)
    mock_trader.get_positions.return_value = []
    mock_trader.get_account_info.return_value = {
        'total_assets': 100000,
        'cash': 100000,
        'power': 50000
    }

    return mock_trader


@pytest.fixture
def mock_price_fetcher():
    """模拟价格获取器"""
    from unittest.mock import Mock

    mock_fetcher = Mock()
    mock_fetcher.get_current_price.return_value = 350.0
    mock_fetcher.get_open_price.return_value = 345.0
    mock_fetcher.get_lot_size.return_value = 100

    return mock_fetcher


# ==================== 清理Fixtures ====================

@pytest.fixture(autouse=True)
def cleanup_environment():
    """自动清理环境变量"""
    # 保存原始环境变量
    original_env = os.environ.copy()

    yield

    # 恢复环境变量
    for key in list(os.environ.keys()):
        if key not in original_env:
            del os.environ[key]
        else:
            os.environ[key] = original_env[key]


@pytest.fixture(autouse=True)
def reset_random_seed():
    """重置随机种子"""
    np.random.seed(42)
    yield
    # 每个测试后重置
    np.random.seed(None)


# ==================== 性能测试Fixtures ====================

@pytest.fixture
def large_dataset():
    """大数据集用于性能测试"""
    dates = pd.date_range(start='2020-01-01', periods=500, freq='D')
    data = {}

    for i in range(50):  # 50只股票
        np.random.seed(i)
        returns = np.random.randn(500) * 0.02
        close = 100 * np.cumprod(1 + returns)

        data[f'HK.{i:05d}'] = pd.DataFrame({
            'date': dates,
            'open': close * (1 + np.random.randn(500) * 0.005),
            'high': close * (1 + np.abs(np.random.randn(500)) * 0.01),
            'low': close * (1 - np.abs(np.random.randn(500)) * 0.01),
            'close': close,
            'volume': np.random.randint(1000000, 10000000, 500)
        })

    return data


# ==================== 跳过条件Fixtures ====================

@pytest.fixture
def skip_if_no_database():
    """如果没有数据库则跳过测试"""
    # 检查数据库连接
    try:
        from mutifactor.infra.yaml_storage import YAMLStorage
        db = YAMLStorage()
        # 尝试简单查询
        # db.query("SELECT 1")
        return False
    except:
        return True


@pytest.fixture
def skip_if_no_api():
    """如果没有API连接则跳过测试"""
    # 检查API连接
    try:
        # 尝试连接OpenD
        return False
    except:
        return True
