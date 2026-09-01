"""
数据获取模块

提供统一的数据获取接口，美股项目
"""

from mutifactor.data.base_fetcher import (
    DataFetcherBase,
    MarketRuleBase,
    MarketType,
    SecurityType,
    KLineType,
)

from mutifactor.data.us_fetcher import FutuUSDataFetcher
from mutifactor.data.us_market_rule import USMarketRule
from mutifactor.data.futu_common import (
    RateLimiter, FUTU_AVAILABLE, with_retry, convert_kline_type, APIError,
    RateLimitConfig,
)
# factory 已移除，回测直接导入具体实现
# from mutifactor.data.factory import (
#     DataFetcherFactory,
#     create_us_fetcher,
# )


__all__ = [
    # 基类和枚举
    'DataFetcherBase',
    'MarketRuleBase',
    'MarketType',
    'SecurityType',
    'KLineType',

    # 美股实现
    'FutuUSDataFetcher',

    # 市场规则
    'USMarketRule',

    # 共用工具
    'RateLimiter',
    'FUTU_AVAILABLE',
    'with_retry',
    'convert_kline_type',
    'APIError',
    'RateLimitConfig',

    # 工厂（已移除）
    # 'DataFetcherFactory',
    # 'create_us_fetcher',
]
