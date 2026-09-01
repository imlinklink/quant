"""
通用数据获取器基类
定义统一的接口规范,支持港股、美股等不同市场
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Optional
from enum import Enum
import pandas as pd
import logging

logger = logging.getLogger(__name__)


class MarketType(Enum):
    """市场类型枚举"""
    HK = "HK"  # 港股
    US = "US"  # 美股
    A = "A"    # A股


class SecurityType(Enum):
    """证券类型枚举"""
    STOCK = "STOCK"        # 股票
    INDEX = "INDEX"        # 指数
    OPTION = "OPTION"      # 期权
    FUTURE = "FUTURE"      # 期货


class KLineType(Enum):
    """K线类型枚举"""
    MIN_1 = "1min"         # 1分钟
    MIN_5 = "5min"         # 5分钟
    MIN_15 = "15min"       # 15分钟
    MIN_30 = "30min"       # 30分钟
    MIN_60 = "60min"       # 60分钟
    DAY = "day"            # 日线
    WEEK = "week"          # 周线
    MONTH = "month"        # 月线


class MarketRuleBase(ABC):
    """市场规则基类"""

    def __init__(self, market_type: MarketType):
        self.market_type = market_type

    @abstractmethod
    def format_stock_code(self, code: str) -> str:
        """
        格式化股票代码为统一格式
        例如: HK.00700, US.AAPL
        """
        pass

    @abstractmethod
    def calculate_shares(self, cash: float, price: float, stock_code: str) -> int:
        """
        根据资金和价格计算可买入股数
        考虑最小交易单位(手数)
        """
        pass

    @abstractmethod
    def get_trading_cost_config(self, stock_code: str) -> Dict[str, float]:
        """
        获取交易成本配置
        返回包含: commission, stamp_duty, trading_fee, settlement_fee, slippage
        """
        pass

    @abstractmethod
    def is_trading_time(self, timestamp: pd.Timestamp) -> bool:
        """
        判断是否为交易时间
        """
        pass


class DataFetcherBase(ABC):
    """数据获取器基类"""

    def __init__(self, market_type: MarketType, config: Dict = None):
        self.market_type = market_type
        self.config = config or {}
        self._connected = False
        self.market_rule = self._init_market_rule()

    def _init_market_rule(self) -> MarketRuleBase:
        """初始化市场规则实例"""
        raise NotImplementedError("子类需要实现此方法")

    @abstractmethod
    def connect(self) -> bool:
        """连接数据源"""
        pass

    @abstractmethod
    def disconnect(self):
        """断开连接"""
        pass

    def __enter__(self):
        """上下文管理器入口"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器退出"""
        self.disconnect()

    @abstractmethod
    def fetch_stock_kline(self, stock_code: str, start_date: str, end_date: str,
                         ktype: KLineType = KLineType.DAY) -> Optional[pd.DataFrame]:
        """
        获取单只股票的K线数据

        参数:
            stock_code: 股票代码
            start_date: 开始日期,格式 'YYYY-MM-DD'
            end_date: 结束日期,格式 'YYYY-MM-DD'
            ktype: K线类型

        返回:
            DataFrame包含: date, open, high, low, close, volume
        """
        pass

    @abstractmethod
    def fetch_multiple_stocks(self, stock_codes: List[str], start_date: str,
                            end_date: str, ktype: KLineType = KLineType.DAY) -> Dict[str, pd.DataFrame]:
        """
        批量获取多只股票的K线数据

        参数:
            stock_codes: 股票代码列表
            start_date: 开始日期
            end_date: 结束日期
            ktype: K线类型

        返回:
            {stock_code: DataFrame} 字典
        """
        pass

    def save_data(self, data: Dict[str, pd.DataFrame], filepath: str) -> None:
        """
        保存数据到本地文件

        参数:
            data: 股票数据字典
            filepath: 保存路径
        """
        try:
            logger.info(f"开始保存数据到 {filepath}")
            logger.info(f"待保存股票数量: {len(data)}")

            # 计算总数据量
            total_records = sum(len(df) for df in data.values())
            logger.info(f"总数据条数: {total_records:,}")

            # 将所有DataFrame合并
            all_dfs = []
            for stock_code, df in data.items():
                df_copy = df.copy()
                df_copy['stock_code'] = stock_code
                df_copy['market'] = self.market_type.value
                all_dfs.append(df_copy)

            logger.info("正在合并数据...")
            combined_df = pd.concat(all_dfs, ignore_index=True)

            logger.info("正在写入文件...")
            # 根据文件扩展名保存
            if filepath.endswith('.csv'):
                combined_df.to_csv(filepath, index=False, encoding='utf-8-sig')
            elif filepath.endswith('.parquet'):
                combined_df.to_parquet(filepath, index=False)
            elif filepath.endswith('.xlsx'):
                combined_df.to_excel(filepath, index=False)
            else:
                # 默认保存为parquet
                combined_df.to_parquet(filepath, index=False)

            # 获取文件大小
            import os
            file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
            logger.info(f"数据已保存到: {filepath} ({file_size_mb:.2f} MB)")

        except (IOError, OSError) as e:
            logger.error(f"保存数据IO错误: {e}")
        except (TypeError, ValueError) as e:
            logger.error(f"保存数据格式错误: {e}")
        except (RuntimeError, ValueError, TypeError) as e:
            logger.error(f"保存数据运行时错误: {type(e).__name__}: {e}")
        except Exception as e:
            logger.critical(f"保存数据未知错误: {type(e).__name__}: {e}", exc_info=True)
            raise

    def load_data(self, filepath: str) -> Dict[str, pd.DataFrame]:
        """
        从本地文件加载数据

        参数:
            filepath: 文件路径

        返回:
            {stock_code: DataFrame} 字典
        """
        try:
            logger.info(f"开始从 {filepath} 加载数据")

            # 根据文件扩展名加载
            if filepath.endswith('.csv'):
                logger.info("正在读取CSV文件...")
                df = pd.read_csv(filepath, encoding='utf-8-sig')
            elif filepath.endswith('.parquet'):
                logger.info("正在读取Parquet文件...")
                df = pd.read_parquet(filepath)
            elif filepath.endswith('.xlsx'):
                logger.info("正在读取Excel文件...")
                df = pd.read_excel(filepath)
            else:
                df = pd.read_parquet(filepath)

            logger.info(f"文件读取完成,共 {len(df):,} 条记录")

            # 按股票代码分组
            if 'stock_code' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                data = {code: group.drop(['stock_code', 'market'], axis=1, errors='ignore')
                        .sort_values('date').reset_index(drop=True)
                       for code, group in df.groupby('stock_code')}

                logger.info(f"数据分组完成,共 {len(data)} 只股票")

                # 显示日期范围
                all_dates = set()
                for df_ in data.values():
                    all_dates.update(df_['date'].dt.strftime('%Y-%m-%d').tolist())
                if all_dates:
                    logger.info(f"日期范围: {min(all_dates)} 至 {max(all_dates)}")

                return data
            else:
                logger.error("数据中没有stock_code列")
                return {}

        except (IOError, OSError) as e:
            logger.error(f"加载数据IO错误: {e}")
            return {}
        except (pd.errors.EmptyDataError, pd.errors.ParserError) as e:
            logger.error(f"加载数据解析错误: {e}")
            return {}
        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"加载数据格式错误: {e}")
            return {}
        except (RuntimeError, ValueError, TypeError) as e:
            logger.error(f"加载数据运行时错误: {type(e).__name__}: {e}")
            return {}
        except Exception as e:
            logger.critical(f"加载数据未知错误: {type(e).__name__}: {e}", exc_info=True)
            raise
