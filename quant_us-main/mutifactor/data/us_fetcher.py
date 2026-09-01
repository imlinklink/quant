"""
富途美股数据获取器
独立实现，不依赖 hk_fetcher
"""
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import logging

from mutifactor.data.base_fetcher import DataFetcherBase, MarketType, MarketRuleBase, KLineType
from mutifactor.data.us_market_rule import USMarketRule
from mutifactor.data.futu_common import (
    RateLimiter, FUTU_AVAILABLE, with_retry, convert_kline_type, APIError
)

logger = logging.getLogger(__name__)


class FutuUSDataFetcher(DataFetcherBase):
    """富途美股数据获取器"""

    def __init__(self, host: str = '127.0.0.1', port: int = 11111,
                 user_id: int = None, config: Dict = None):
        DataFetcherBase.__init__(self, MarketType.US, config)

        if not FUTU_AVAILABLE:
            raise ImportError(
                "未安装futubridge库,请运行: pip install futu-api\n"
                "同时需要下载并运行FutuOpenD: https://openapi.futunn.com/download"
            )

        self.host = host
        self.port = port
        self.user_id = user_id
        self.quote_ctx = None
        self._rate_limiter = RateLimiter()

        logger.info(f"富途美股数据获取器初始化完成, OpenD地址: {host}:{port}")

    def _init_market_rule(self) -> MarketRuleBase:
        """初始化美股市场规则"""
        return USMarketRule()

    def _convert_kline_type(self, ktype: KLineType):
        """转换K线类型"""
        return convert_kline_type(ktype)

    def connect(self) -> bool:
        """连接到富途OpenD（美股）"""
        try:
            logger.info("正在连接到富途OpenD(美股)...")

            from futu import OpenQuoteContext, Market, SecurityType, RET_OK

            quote_ctx = OpenQuoteContext(
                host=self.host,
                port=self.port
            )

            ret, data = quote_ctx.get_stock_basicinfo(
                market=Market.US,
                stock_type=SecurityType.STOCK
            )

            if ret == RET_OK:
                self.quote_ctx = quote_ctx
                self._connected = True
                logger.info("成功连接到富途OpenD(美股)")
                return True
            else:
                logger.error(f"连接失败: {data}")
                quote_ctx.close()
                return False

        except ImportError as e:
            logger.error(f"未安装futu库: {e}")
            return False
        except (OSError, IOError) as e:
            logger.error(f"连接富途OpenD网络错误: {e}")
            return False
        except (RuntimeError, ValueError, TypeError) as e:
            logger.error(f"连接富途OpenD运行时错误: {type(e).__name__}: {e}")
            return False
        except Exception as e:
            logger.critical(f"连接富途OpenD未知错误: {type(e).__name__}: {e}", exc_info=True)
            return False

    @with_retry(max_retries=2)
    def fetch_stock_kline(self, stock_code: str, start_date: str, end_date: str,
                          ktype: KLineType = KLineType.DAY) -> Optional[pd.DataFrame]:
        """获取美股K线数据"""
        if not self._connected or not self.quote_ctx:
            logger.error("未连接到富途OpenD")
            return None

        try:
            self._rate_limiter.wait_if_needed()

            from futu import KLType, RET_OK

            kl_type = self._convert_kline_type(ktype)

            logger.debug(f"获取美股K线: {stock_code}, {start_date} ~ {end_date}, {ktype.value}")

            ret, data, _ = self.quote_ctx.request_history_kline(
                code=stock_code,
                start=start_date,
                end=end_date,
                ktype=kl_type,
                autype='qfq'
            )

            if ret != RET_OK:
                logger.error(f"获取K线数据失败: {stock_code}, 错误: {data}")
                return None

            if data is None or len(data) == 0:
                logger.warning(f"未获取到K线数据: {stock_code}")
                return None

            # 保留完整时间戳（对于日内K线需要时分秒）
            time_key = pd.to_datetime(data['time_key'])

            df = pd.DataFrame({
                'date': time_key,  # 完整datetime，不是纯日期
                'open': data['open'].astype(float),
                'high': data['high'].astype(float),
                'low': data['low'].astype(float),
                'close': data['close'].astype(float),
                'volume': data['volume'].astype(int),
            })

            df = df.sort_values('date').reset_index(drop=True)
            logger.debug(f"获取到 {len(df)} 条K线数据: {stock_code}")
            return df

        except (OSError, IOError) as e:
            logger.error(f"获取K线数据网络错误: {stock_code}, {e}")
            return None
        except (KeyError, ValueError, pd.errors.EmptyDataError) as e:
            logger.error(f"获取K线数据格式错误: {stock_code}, {e}")
            return None
        except (RuntimeError, ValueError, TypeError) as e:
            logger.error(f"获取K线数据运行时错误: {stock_code}, {type(e).__name__}: {e}")
            return None
        except Exception as e:
            logger.critical(f"获取K线数据未知错误: {stock_code}, {type(e).__name__}: {e}", exc_info=True)
            raise

    def fetch_multiple_stocks(self, stock_codes: List[str], start_date: str, end_date: str,
                               ktype: KLineType = KLineType.DAY) -> Dict[str, pd.DataFrame]:
        """批量获取美股K线数据"""
        results = {}
        total = len(stock_codes)

        for i, code in enumerate(stock_codes, 1):
            logger.info(f"[{i}/{total}] 获取美股数据: {code}")
            df = self.fetch_stock_kline(code, start_date, end_date, ktype)
            if df is not None and len(df) > 0:
                results[code] = df
            else:
                logger.warning(f"未能获取数据: {code}")

        logger.info(f"成功获取 {len(results)}/{total} 只美股数据")
        return results

    def disconnect(self):
        """断开连接"""
        if self.quote_ctx:
            try:
                self.quote_ctx.close()
                logger.info("已断开富途OpenD连接")
            except Exception as e:
                logger.warning(f"断开连接时出错: {e}")
            finally:
                self.quote_ctx = None
                self._connected = False

    def get_minute_klines(self, stock_code: str, count: int = 60) -> Optional[pd.DataFrame]:
        """获取1分钟K线（用于ATR计算和实时分析）"""
        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=5)
        start_date = start_dt.strftime('%Y-%m-%d')
        end_date = end_dt.strftime('%Y-%m-%d')
        return self.fetch_stock_kline(stock_code, start_date, end_date, ktype=KLineType.MIN_1)
