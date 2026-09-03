"""
富途牛牛(Futu OpenAPI)数据获取器 - 港股实现
使用富途OpenD接入实时港股行情数据

股票池从数据库 stock_info 表读取，不再硬编码
"""

import pandas as pd
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import logging
import time
import functools

from tqdm import tqdm

from mutifactor.data.base_fetcher import (
    DataFetcherBase, MarketRuleBase, MarketType,
    SecurityType, KLineType
)
from mutifactor.data.hk_market_rule import HKMarketRule

# 尝试导入futu
# 明确导入所需的类，避免通配符导入
try:
    from futu import OpenQuoteContext, KLType, Market, SecurityType, RET_OK, AuType
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False

logger = logging.getLogger(__name__)


def get_hk_stock_name(stock_code: str) -> str:
    """获取港股中文名称 - 从数据库读取"""
    try:
        from mutifactor.infra.yaml_storage import YAMLStorage
        db = YAMLStorage()
        name = db.get_stock_name(stock_code)
        return name if name else stock_code
    except Exception as e:
        logger.debug(f"获取股票名称失败: {e}")
        return stock_code


# ==================== API限流配置 ====================
class RateLimitConfig:
    """API限流配置"""
    # 基础请求间隔（秒）
    # 富途限制：每30秒最多60次 = 每秒2次
    # 设置更保守：0.6秒 = 每秒2次，避免触发限流
    BASE_INTERVAL = 0.7
    # 最大重试次数
    MAX_RETRIES = 3
    # 指数退避基数（秒）
    BACKOFF_BASE = 1.0
    # 最大退避时间（秒）
    MAX_BACKOFF = 30.0
    # 富途API限制：每30秒最多60次，设置更保守（50次）
    MAX_REQUESTS_PER_WINDOW = 40
    # 限流窗口时间（秒）
    RATE_LIMIT_WINDOW = 30


class APIError(Exception):
    """API调用错误"""
    def __init__(self, message: str, retry_count: int = 0, is_rate_limited: bool = False):
        super().__init__(message)
        self.retry_count = retry_count
        self.is_rate_limited = is_rate_limited


def with_retry(
    max_retries: int = RateLimitConfig.MAX_RETRIES,
    backoff_base: float = RateLimitConfig.BACKOFF_BASE,
    max_backoff: float = RateLimitConfig.MAX_BACKOFF
):
    """
    重试装饰器 - 支持指数退避
    
    Args:
        max_retries: 最大重试次数
        backoff_base: 退避基数（秒）
        max_backoff: 最大退避时间（秒）
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except APIError as e:
                    last_exception = e
                    
                    if attempt < max_retries:
                        # 指数退避
                        wait_time = min(backoff_base * (2 ** attempt), max_backoff)
                        logger.warning(
                            f"API调用失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}, "
                            f"{wait_time:.1f}秒后重试..."
                        )
                        time.sleep(wait_time)
                    else:
                        logger.error(f"API调用失败，已达到最大重试次数: {e}")
                        raise
                except Exception as e:
                    # 非API错误，直接抛出
                    raise
            
            # 不应该到达这里
            raise last_exception or APIError("未知错误")
        
        return wrapper
    return decorator


class RateLimiter:
    """API请求限流器"""
    
    def __init__(
        self,
        min_interval: float = RateLimitConfig.BASE_INTERVAL,
        max_requests_per_window: int = RateLimitConfig.MAX_REQUESTS_PER_WINDOW,
        window_seconds: float = RateLimitConfig.RATE_LIMIT_WINDOW
    ):
        """
        初始化限流器
        
        Args:
            min_interval: 最小请求间隔（秒）
            max_requests_per_window: 时间窗口内最大请求数
            window_seconds: 时间窗口（秒）
        """
        self.min_interval = min_interval
        self.max_requests_per_window = max_requests_per_window
        self.window_seconds = window_seconds
        self.last_request_time = 0.0
        self.request_timestamps: List[float] = []
    
    def wait_if_needed(self) -> None:
        """如果需要，等待以遵守限流规则"""
        current_time = time.time()
        
        # 清理超过时间窗口的时间戳
        self.request_timestamps = [
            ts for ts in self.request_timestamps 
            if current_time - ts < self.window_seconds
        ]
        
        # 检查时间窗口内请求数限制
        if len(self.request_timestamps) >= self.max_requests_per_window:
            wait_time = self.window_seconds - (current_time - self.request_timestamps[0])
            if wait_time > 0:
                logger.debug(f"达到限流限制（{self.max_requests_per_window}次/{self.window_seconds}秒），等待 {wait_time:.1f} 秒")
                time.sleep(wait_time)
                self.request_timestamps = []
        
        # 检查最小间隔
        time_since_last = current_time - self.last_request_time
        if time_since_last < self.min_interval:
            sleep_time = self.min_interval - time_since_last
            time.sleep(sleep_time)
        
        # 记录本次请求时间
        self.last_request_time = time.time()
        self.request_timestamps.append(self.last_request_time)


class FutuHKDataFetcher(DataFetcherBase):
    """富途港股数据获取器"""

    def __init__(self, host: str = '127.0.0.1', port: int = 11111,
                 user_id: int = None, config: Dict = None):
        """
        初始化富途港股数据获取器

        参数:
            host: OpenD服务器地址,默认本地127.0.0.1
            port: OpenD端口,默认11111
            user_id: 用户ID,用于多用户环境
            config: 配置字典
        """
        super().__init__(MarketType.HK, config)

        if not FUTU_AVAILABLE:
            raise ImportError(
                "未安装futubridge库,请运行: pip install futu-api\n"
                "同时需要下载并运行FutuOpenD: https://openapi.futunn.com/download"
            )

        self.host = host
        self.port = port
        self.user_id = user_id
        self.quote_ctx = None
        
        # 初始化限流器
        self._rate_limiter = RateLimiter()

        logger.info(f"富途港股数据获取器初始化完成, OpenD地址: {host}:{port}")

    def _init_market_rule(self) -> MarketRuleBase:
        """初始化港股市场规则"""
        return HKMarketRule()

    def _convert_kline_type(self, ktype: KLineType) -> KLType:
        """转换K线类型枚举"""
        mapping = {
            KLineType.MIN_1: KLType.K_1M,
            KLineType.MIN_5: KLType.K_5M,
            KLineType.MIN_15: KLType.K_15M,
            KLineType.MIN_30: KLType.K_30M,
            KLineType.MIN_60: KLType.K_60M,
            KLineType.DAY: KLType.K_DAY,
            KLineType.WEEK: KLType.K_WEEK,
            # 富途API不支持月线,映射到日线
            KLineType.MONTH: KLType.K_DAY,
        }
        return mapping.get(ktype, KLType.K_DAY)

    def connect(self) -> bool:
        """
        连接到富途OpenD

        返回:
            连接是否成功
        """
        try:
            logger.info("正在连接到富途OpenD...")

            quote_ctx = OpenQuoteContext(
                host=self.host,
                port=self.port
            )

            # 测试连接 - 使用获取股票列表API
            ret, data = quote_ctx.get_stock_basicinfo(
                market=Market.HK,
                stock_type=SecurityType.STOCK
            )

            if ret == RET_OK:
                self.quote_ctx = quote_ctx
                self._connected = True
                logger.info("成功连接到富途OpenD")
                return True
            else:
                logger.error(f"连接失败: {data}")
                quote_ctx.close()
                return False

        except (OSError, IOError) as e:
            logger.error(f"连接富途OpenD网络错误: {e}")
            return False
        except (RuntimeError, ValueError, TypeError) as e:
            logger.error(f"连接富途OpenD运行时错误: {type(e).__name__}: {e}")
            return False
        except Exception as e:
            logger.critical(f"连接富途OpenD未知错误: {type(e).__name__}: {e}", exc_info=True)
            raise

    def disconnect(self):
        """断开连接"""
        if self.quote_ctx:
            self.quote_ctx.close()
            self.quote_ctx = None
            self._connected = False
            logger.info("已断开富途OpenD连接")

    def get_lot_size(self, stock_code: str) -> int:
        """
        获取股票的每手股数

        参数:
            stock_code: 股票代码 (格式: HK.00700 或 00700)

        返回:
            每手股数，获取失败时返回默认值100
        """
        # 标准化代码格式
        if not stock_code.startswith('HK.'):
            stock_code = f"HK.{stock_code}"

        # 优先从数据库获取
        try:
            from mutifactor.infra.yaml_storage import yaml_storage
            lot_size = yaml_storage.get_lot_size(stock_code)
            if lot_size:
                return lot_size
        except Exception:
            pass

        # 未连接时返回默认值
        if not self._connected or not self.quote_ctx:
            logger.debug(f"未连接富途，使用默认每手100股: {stock_code}")
            return 100

        try:
            # 应用限流
            self._rate_limiter.wait_if_needed()

            # 获取市场快照
            ret, data = self.quote_ctx.get_market_snapshot([stock_code])

            if ret == RET_OK and len(data) > 0:
                lot_size = int(data['lot_size'].iloc[0])
                logger.debug(f"获取 {stock_code} 每手股数: {lot_size}")
                return lot_size
            else:
                logger.warning(f"获取 {stock_code} 市场快照失败，使用默认100股")
                return 100

        except Exception as e:
            logger.warning(f"获取 {stock_code} 每手股数失败: {e}，使用默认100股")
            return 100

    def get_blue_chip_stocks(self) -> List[str]:
        """
        获取港股蓝筹股列表 - 从数据库stock_info表读取
        
        返回:
            股票代码列表
        """
        try:
            from mutifactor.infra.yaml_storage import YAMLStorage
            db = YAMLStorage()
            stock_codes = db.get_all_stock_codes(market='HK')
            logger.info(f"从数据库获取股票列表完成,共{len(stock_codes)}只")
            return stock_codes
        except Exception as e:
            logger.error(f"从数据库获取股票列表失败: {e}")
            return []



    @with_retry(max_retries=3, backoff_base=1.0, max_backoff=30.0)
    def fetch_stock_kline(self, stock_code: str, start_date: str, end_date: str,
                         ktype: KLineType = KLineType.DAY) -> Optional[pd.DataFrame]:
        """
        获取单只股票的K线数据 - 支持历史数据

        参数:
            stock_code: 股票代码,如 '00700'
            start_date: 开始日期,格式 'YYYY-MM-DD'
            end_date: 结束日期,格式 'YYYY-MM-DD'
            ktype: K线类型,默认日线

        返回:
            DataFrame with columns: date, open, high, low, close, volume
        """
        if not self._connected:
            raise APIError("未连接到OpenD", is_rate_limited=False)

        # 应用限流
        self._rate_limiter.wait_if_needed()

        try:
            # 转换K线类型
            futu_ktype = self._convert_kline_type(ktype)

            # 判断是否为指数（指数不需要复权）
            # 富途指数代码: HK.800000(恒生), HK.800100(恒生科技) 等
            is_index = '800000' in stock_code or '800100' in stock_code or stock_code.upper() in ['HK.HSI', 'HK.HSTECH', 'HSI', 'HSTECH']
            autype = AuType.QFQ if not is_index else None

            # 使用 request_history_kline 分页获取全部历史数据
            # 富途单次最多返回 max_count 条，通过 page_req_key 分页
            logger.info(f"正在获取 {stock_code} 历史K线数据{'(指数)' if is_index else ''}...")
            all_data = []
            page_key = None
            first_error = None
            while True:
                ret_code, data, page_key = self.quote_ctx.request_history_kline(
                    code=stock_code,
                    start=start_date,
                    end=end_date,
                    ktype=futu_ktype,
                    autype=autype,
                    max_count=2000,
                    page_req_key=page_key
                )
                if ret_code != RET_OK:
                    first_error = data
                    break
                if data is None or (hasattr(data, '__len__') and len(data) == 0):
                    break
                all_data.append(data)
                logger.info(f"  分页获取 {stock_code}: {len(data)} 条, 累计 {sum(len(d) for d in all_data)} 条")
                if page_key is None:
                    break

            if all_data:
                import pandas as pd
                data = pd.concat(all_data, ignore_index=True)
                data = data.drop_duplicates('time_key').sort_values('time_key').reset_index(drop=True)
                data['date'] = pd.to_datetime(data['time_key'])
                logger.info(f"获取 {stock_code} 数据成功: 共 {len(data)} 天")
            else:
                logger.warning(f"获取 {stock_code} 数据为空: {first_error or 'unknown error'}")
                data = None

            # 如果分页获取全部失败（all_data为空），降级使用 get_cur_kline（只能获取最近500根）
            fallback_triggered = False
            if data is None and all_data:
                # 分页循环至少成功了一次，数据已在 all_data 中，不需要降级
                pass
            elif data is None:
                # 分页获取完全失败，尝试降级
                logger.warning(f"request_history_kline 分页获取 {stock_code} 失败，尝试 get_cur_kline...")
                self._rate_limiter.wait_if_needed()
                ret, data = self.quote_ctx.get_cur_kline(
                    code=stock_code, num=500, ktype=futu_ktype, autype=autype
                )
                if ret != RET_OK:
                    logger.error(f"get_cur_kline 也失败 {stock_code}: {data}")
                    raise APIError(f"获取K线数据失败 {stock_code}: {data}", is_rate_limited=True)
                fallback_triggered = True

            if data is None or len(data) == 0:
                logger.warning(f"无数据 {stock_code}")
                return None

            # 确保有必要的列
            required_cols = ['time_key', 'open', 'high', 'low', 'close', 'volume', 'turnover_rate']
            if not all(col in data.columns for col in required_cols):
                logger.warning(f"数据缺少必要的列: {stock_code}, 列: {data.columns.tolist()}")
                return None

            # 复制并重命名列
            df = data[required_cols].copy()
            df = df.rename(columns={'time_key': 'date'})

            # 转换日期格式
            df['date'] = pd.to_datetime(df['date'])

            # 按日期排序
            df = df.sort_values('date').reset_index(drop=True)

            # 过滤日期范围
            start_dt = pd.to_datetime(start_date)
            end_dt = pd.to_datetime(end_date)
            df = df[(df['date'] >= start_dt) & (df['date'] <= end_dt)]

            if len(df) == 0:
                logger.warning(f"日期范围内无数据 {stock_code}")
                return None

            # 转换为float避免Decimal问题
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col in df.columns:
                    df[col] = df[col].astype(float)

            logger.info(f"获取K线数据成功: {stock_code}, 共{len(df)}条记录")

            return df

        except APIError:
            raise
        except (KeyError, ValueError, pd.errors.EmptyDataError) as e:
            logger.warning(f"数据格式错误 {stock_code}: {e}")
            return None
        except (OSError, IOError) as e:
            logger.error(f"获取K线数据网络错误 {stock_code}: {e}")
            raise APIError(f"获取K线数据网络错误 {stock_code}: {e}", is_rate_limited=False)
        except Exception as e:
            logger.error(f"获取K线数据异常 {stock_code}: {type(e).__name__}: {e}", exc_info=True)
            raise APIError(f"获取K线数据异常 {stock_code}: {e}")

    def fetch_multiple_stocks(self, stock_codes: List[str], start_date: str,
                            end_date: str, ktype: KLineType = KLineType.DAY) -> Dict[str, pd.DataFrame]:
        """
        批量获取多只股票的K线数据（带智能限流）

        参数:
            stock_codes: 股票代码列表
            start_date: 开始日期,格式 'YYYY-MM-DD'
            end_date: 结束日期,格式 'YYYY-MM-DD'
            ktype: K线类型

        返回:
            {stock_code: DataFrame} 字典
        """
        all_data: Dict[str, pd.DataFrame] = {}
        failed_stocks: List[str] = []
        rate_limited_stocks: List[str] = []

        logger.info(f"开始批量获取K线数据,股票数: {len(stock_codes)}")
        logger.info(f"日期范围: {start_date} - {end_date}")

        iterator = tqdm(stock_codes, desc="📥 下载K线", unit="只", leave=False)
        for stock_code in iterator:
            try:
                df = self.fetch_stock_kline(stock_code, start_date, end_date, ktype)
                if df is not None and len(df) > 0:
                    all_data[stock_code] = df

            except APIError as e:
                if e.is_rate_limited:
                    iterator.write(f"  ⚠ {stock_code} 限流: {e}")
                    rate_limited_stocks.append(stock_code)
                else:
                    iterator.write(f"  ✗ {stock_code} 失败: {e}")
                    failed_stocks.append(stock_code)
            except (OSError, IOError) as e:
                iterator.write(f"  ✗ {stock_code} 网络错误: {e}")
                failed_stocks.append(stock_code)
            except (KeyError, ValueError) as e:
                iterator.write(f"  ✗ {stock_code} 格式错误: {e}")
                failed_stocks.append(stock_code)
            except (RuntimeError, ValueError, TypeError) as e:
                iterator.write(f"  ✗ {stock_code} 运行时错误: {type(e).__name__}: {e}")
                failed_stocks.append(stock_code)
            except Exception as e:
                iterator.write(f"  ✗ {stock_code} 未知错误: {type(e).__name__}: {e}")
                raise
        
        logger.info(f"数据获取完成,成功: {len(all_data)}, 失败: {len(failed_stocks)}, 限流: {len(rate_limited_stocks)}")

        logger.info(f"数据获取完成,成功: {len(all_data)}, 失败: {len(failed_stocks)}, 限流: {len(rate_limited_stocks)}")

        if failed_stocks:
            logger.warning(f"获取失败的股票: {failed_stocks}")
        if rate_limited_stocks:
            logger.warning(f"被限流的股票: {rate_limited_stocks}")

        return all_data

    def get_stock_name(self, stock_code: str) -> Optional[str]:
        """
        获取股票名称
        
        参数:
            stock_code: 股票代码,如 '00700'
            
        返回:
            股票名称,如 '腾讯控股'
        """
        if not self._connected:
            logger.error("未连接到OpenD")
            return None
        
        try:
            # 获取股票基本信息
            ret, data = self.quote_ctx.get_stock_basicinfo(
                market=Market.HK,
                stock_type=SecurityType.STOCK,
                code_list=[stock_code]
            )
            
            if ret == RET_OK and len(data) > 0:
                # 返回股票名称
                return data.iloc[0]['name']
            else:
                logger.warning(f"获取股票名称失败 {stock_code}: {data}")
                return None
                
        except (OSError, IOError) as e:
            logger.error(f"获取股票名称网络错误 {stock_code}: {e}")
            return None
        except (KeyError, IndexError) as e:
            logger.error(f"获取股票名称数据错误 {stock_code}: {e}")
            return None
        except (RuntimeError, ValueError, TypeError) as e:
            logger.error(f"获取股票名称运行时错误 {stock_code}: {type(e).__name__}: {e}")
            return None
        except Exception as e:
            logger.critical(f"获取股票名称未知错误 {stock_code}: {type(e).__name__}: {e}", exc_info=True)
            raise
    
    def get_stock_names(self, stock_codes: List[str]) -> Dict[str, str]:
        """
        批量获取股票名称
        
        参数:
            stock_codes: 股票代码列表
            
        返回:
            字典 {股票代码: 股票名称}
        """
        stock_names = {}
        
        if not self._connected:
            logger.error("未连接到OpenD")
            return stock_names
        
        try:
            # 批量获取股票基本信息（含上市日期）
            ret, data = self.quote_ctx.get_stock_basicinfo(
                market=Market.HK,
                stock_type=SecurityType.STOCK,
                code_list=stock_codes
            )
            
            if ret == RET_OK and len(data) > 0:
                try:
                    from mutifactor.infra.yaml_storage import yaml_storage
                    for _, row in data.iterrows():
                        code = row['code']
                        stock_names[code] = row['name']
                        listing_date = None
                        if 'listing_date' in row.index and pd.notna(row.get('listing_date')):
                            try:
                                listing_date = pd.to_datetime(row['listing_date']).date()
                            except (ValueError, TypeError):
                                pass
                        yaml_storage.save_listing_date(code, row['name'], 'HK', listing_date)
                except Exception as e:
                    logger.debug(f"保存股票信息到数据库失败: {e}")
            else:
                logger.warning(f"批量获取股票名称失败: {data}")
                
        except (OSError, IOError) as e:
            logger.error(f"批量获取股票名称网络错误: {e}")
        except (KeyError, IndexError) as e:
            logger.error(f"批量获取股票名称数据错误: {e}")
        except Exception as e:
            logger.error(f"批量获取股票名称异常: {type(e).__name__}: {e}", exc_info=True)

        return stock_names






if __name__ == "__main__":
    # 测试连接和数据获取
    try:
        with FutuHKDataFetcher(host='127.0.0.1', port=11111) as fetcher:
            blue_chips = fetcher.get_blue_chip_stocks()

            if blue_chips:
                test_stock = blue_chips[0]
                end_date = datetime.now().strftime('%Y-%m-%d')
                start_date = (datetime.now() - timedelta(days=90)).strftime('%Y-%m-%d')

                df = fetcher.fetch_stock_kline(test_stock, start_date, end_date)

                if df is not None:
                    print(f"成功获取 {test_stock} 的K线数据")
                    print(df.head())
    except (OSError, IOError) as e:
        print(f"测试失败 - 网络错误: {e}")
    except APIError as e:
        print(f"测试失败 - API错误: {e}")
    except Exception as e:
        print(f"测试失败 - {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
