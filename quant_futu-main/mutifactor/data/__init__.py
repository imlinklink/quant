"""
数据获取模块

提供统一的港股数据获取接口
"""

from mutifactor.data.base_fetcher import (
    DataFetcherBase,
    MarketRuleBase,
    MarketType,
    SecurityType,
    KLineType,
)

from mutifactor.data.hk_fetcher import (
    FutuHKDataFetcher,
    get_hk_stock_name,
)

from mutifactor.data.hk_market_rule import HKMarketRule
from mutifactor.data.factory import (
    DataFetcherFactory,
    create_hk_fetcher,
)


__all__ = [
    'DataFetcherBase',
    'MarketRuleBase',
    'MarketType',
    'SecurityType',
    'KLineType',
    'FutuHKDataFetcher',
    'get_hk_stock_name',
    'HKMarketRule',
    'DataFetcherFactory',
    'create_hk_fetcher',
]
