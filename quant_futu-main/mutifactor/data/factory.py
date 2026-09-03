"""
数据获取器工厂
根据市场类型创建相应的数据获取器实例
"""

from typing import Dict, Optional
from mutifactor.data.base_fetcher import DataFetcherBase, MarketType
from mutifactor.data.hk_fetcher import FutuHKDataFetcher
import logging

logger = logging.getLogger(__name__)


# 数据获取器注册表
_FETCHER_REGISTRY: Dict[MarketType, type] = {
    MarketType.HK: FutuHKDataFetcher,
}


class DataFetcherFactory:
    """数据获取器工厂类"""

    @staticmethod
    def create_fetcher(market_type: MarketType, **kwargs) -> Optional[DataFetcherBase]:
        """
        创建数据获取器实例

        参数:
            market_type: 市场类型
            **kwargs: 传递给数据获取器的参数

        返回:
            数据获取器实例,如果不支持则返回None
        """
        fetcher_class = _FETCHER_REGISTRY.get(market_type)

        if fetcher_class is None:
            logger.error(f"不支持的市场类型: {market_type}")
            return None

        try:
            fetcher = fetcher_class(**kwargs)
            logger.info(f"成功创建 {market_type.value} 市场数据获取器")
            return fetcher
        except (TypeError, ValueError) as e:
            logger.error(f"创建数据获取器参数错误: {e}")
            return None
        except (OSError, IOError) as e:
            logger.error(f"创建数据获取器IO错误: {e}")
            return None
        except (RuntimeError, ValueError, TypeError) as e:
            logger.error(f"创建数据获取器运行时错误: {type(e).__name__}: {e}")
            return None
        except Exception as e:
            logger.critical(f"创建数据获取器未知错误: {type(e).__name__}: {e}", exc_info=True)
            raise

    @staticmethod
    def register_fetcher(market_type: MarketType, fetcher_class: type):
        """
        注册数据获取器

        参数:
            market_type: 市场类型
            fetcher_class: 数据获取器类
        """
        _FETCHER_REGISTRY[market_type] = fetcher_class
        logger.info(f"已注册 {market_type.value} 市场数据获取器: {fetcher_class.__name__}")

    @staticmethod
    def get_supported_markets() -> list:
        """
        获取支持的市场列表

        返回:
            支持的市场类型列表
        """
        return list(_FETCHER_REGISTRY.keys())

    @staticmethod
    def is_supported(market_type: MarketType) -> bool:
        """
        检查市场类型是否支持

        参数:
            market_type: 市场类型

        返回:
            是否支持
        """
        return market_type in _FETCHER_REGISTRY


# 便捷函数
def create_hk_fetcher(host: str = '127.0.0.1', port: int = 11111, **kwargs) -> Optional[FutuHKDataFetcher]:
    """
    创建港股数据获取器

    参数:
        host: OpenD服务器地址
        port: OpenD端口
        **kwargs: 其他参数

    返回:
        港股数据获取器实例
    """
    return DataFetcherFactory.create_fetcher(MarketType.HK, host=host, port=port, **kwargs)


if __name__ == "__main__":
    # 测试工厂
    print("支持的市场:", DataFetcherFactory.get_supported_markets())
    print("港股是否支持:", DataFetcherFactory.is_supported(MarketType.HK))
    print("沪市A股是否支持:", DataFetcherFactory.is_supported(MarketType.SH))
    print("深市A股是否支持:", DataFetcherFactory.is_supported(MarketType.SZ))
