"""
K线数据缓存层 - 优先从数据库读取,减少API调用
"""
import logging
from datetime import datetime, date, timedelta
from typing import Dict, Optional, List
import pandas as pd

# MySQL→YAML迁移：db_storage已废弃，改用yaml_storage
# from mutifactor.infra.db_storage import YAMLStorage  # 旧代码
try:
    from mutifactor.infra.yaml_storage import yaml_storage
except ImportError:
    yaml_storage = None

from mutifactor.data.base_fetcher import DataFetcherBase

logger = logging.getLogger(__name__)


class DBKlineCache:
    """K线数据数据库缓存"""

    def __init__(self, data_fetcher: DataFetcherBase, is_us: bool = False):
        """
        初始化K线缓存

        Args:
            data_fetcher: 数据获取器 (FutuHKDataFetcher 或 FutuUSDataFetcher)
            is_us: 是否美股
        """
        self.data_fetcher = data_fetcher
        self.is_us = is_us
        self.db = YAMLStorage()

    def get_kline_data(self, stock_code: str, start_date: date, end_date: date,
                       kline_type: str = 'DAY') -> Optional[pd.DataFrame]:
        """
        获取K线数据 (优先从数据库)

        Args:
            stock_code: 股票代码
            start_date: 开始日期
            end_date: 结束日期
            kline_type: K线类型

        Returns:
            DataFrame with columns: date, open, high, low, close, volume
        """
        # 1. 尝试从数据库读取
        df = self._get_from_db(stock_code, start_date, end_date, kline_type)

        if df is not None and len(df) >= 30:
            logger.debug(f"从数据库读取K线: {stock_code}, {len(df)}条记录")
            return df

        # 2. 数据库数据不足,从API拉取
        logger.info(f"数据库数据不足,从API拉取: {stock_code}")
        df_api = self._fetch_from_api(stock_code, start_date, end_date, kline_type)

        if df_api is not None and len(df_api) > 0:
            # 3. 保存到数据库
            self._save_to_db(stock_code, df_api, kline_type)
            logger.info(f"K线数据已保存到数据库: {stock_code}, {len(df_api)}条")

        return df_api

    def get_multiple_klines(self, stock_codes: List[str], start_date: date, end_date: date,
                            kline_type: str = 'DAY',
                            require_latest: bool = False) -> Dict[str, pd.DataFrame]:
        """
        批量获取多只股票的K线数据 (优先从数据库)

        Args:
            stock_codes: 股票代码列表
            start_date: 开始日期
            end_date: 结束日期
            kline_type: K线类型
            require_latest: 是否要求数据最新（即最新日期 >= end_date）

        Returns:
            {stock_code: DataFrame} 字典
        """
        logger.info(f"get_multiple_klines开始执行, 股票数量: {len(stock_codes)}, require_latest={require_latest}")
        result = {}
        missing_stocks = []

        # 1. 批量从数据库读取
        logger.info(f"开始批量从数据库读取...")
        for i, stock_code in enumerate(stock_codes):
            logger.debug(f"正在读取第{i+1}/{len(stock_codes)}只股票: {stock_code}")
            df = self._get_from_db(stock_code, start_date, end_date, kline_type)

            # 检查数据是否有效：条数足够且（不要求最新 或 最新日期 >= end_date）
            is_valid = False
            if df is not None and len(df) >= 30:
                if not require_latest:
                    is_valid = True
                else:
                    # 检查最新日期是否 >= end_date（考虑交易日，允许1天缓冲）
                    latest_date = pd.to_datetime(df['date'].max()).date()
                    # 如果 end_date 是今天或未来，要求最新数据至少是 end_date 或前一个交易日
                    buffer_days = 1 if self.is_us else 0  # 美股可能有延迟
                    is_valid = latest_date >= (end_date - timedelta(days=buffer_days))
                    if not is_valid:
                        logger.debug(f"数据过期: {stock_code}, 最新{latest_date}, 需要{end_date}")

            if is_valid:
                result[stock_code] = df
                logger.debug(f"数据库命中: {stock_code}, {len(df)}条记录, 最新{df['date'].max().date() if hasattr(df['date'].max(), 'date') else df['date'].max()}")
            else:
                missing_stocks.append(stock_code)
                logger.debug(f"数据库未命中: {stock_code}")

        logger.info(f"数据库读取完成, 命中: {len(result)}/{len(stock_codes)}, 缺失: {len(missing_stocks)}")

        # 2. 拉取缺失的数据
        if missing_stocks:
            logger.info(f"从API拉取缺失数据: {len(missing_stocks)}只")
            df_dict = self.data_fetcher.fetch_multiple_stocks(missing_stocks, start_date.strftime('%Y-%m-%d'),
                                                                end_date.strftime('%Y-%m-%d'))

            # 3. 保存新数据到数据库
            for stock_code, df in df_dict.items():
                if df is not None and len(df) > 0:
                    result[stock_code] = df
                    self._save_to_db(stock_code, df, kline_type)
        else:
            if require_latest:
                logger.info(f"所有数据已是最新,跳过API拉取")
            else:
                logger.info(f"没有缺失数据,跳过API拉取")

        # 4. 返回结果
        logger.info(f"准备返回结果, 共{len(result)}只股票")
        return result

    def _get_from_db(self, stock_code: str, start_date: date, end_date: date,
                     kline_type: str) -> Optional[pd.DataFrame]:
        """从数据库读取K线数据"""
        logger.debug(f"_get_from_db开始: {stock_code}")
        try:
            if self.is_us:
                result = self.db.get_us_kline_data(stock_code, start_date, end_date, kline_type)
            else:
                result = self.db.get_kline_data(stock_code, start_date, end_date, kline_type)
            logger.debug(f"_get_from_db完成: {stock_code}, 结果: {'有' if result is not None else '无'}")
            return result
        except Exception as e:
            logger.warning(f"数据库读取失败 {stock_code}: {e}")
            return None

    def _fetch_from_api(self, stock_code: str, start_date: date, end_date: date,
                        kline_type: str) -> Optional[pd.DataFrame]:
        """从API拉取K线数据"""
        try:
            return self.data_fetcher.fetch_stock_kline(
                stock_code,
                start_date.strftime('%Y-%m-%d'),
                end_date.strftime('%Y-%m-%d'),
                kline_type
            )
        except Exception as e:
            logger.error(f"API拉取失败 {stock_code}: {e}")
            return None

    def _save_to_db(self, stock_code: str, df: pd.DataFrame, kline_type: str):
        """保存K线数据到数据库"""
        try:
            if self.is_us:
                self.db.save_us_kline_data(stock_code, df, kline_type)
            else:
                self.db.save_kline_data(stock_code, df, kline_type)
        except Exception as e:
            logger.warning(f"数据库保存失败 {stock_code}: {e}")
