"""
日内分钟K线数据提供器
从富途API获取实时分钟K线，供买入时机分析使用
"""
import logging
import threading
import time
from datetime import datetime, time as dt_time
from typing import Dict, Optional

import pandas as pd
from futu import OpenQuoteContext, KLType, RET_OK

logger = logging.getLogger(__name__)

# 港股交易时段
HK_MARKET_OPEN_MORNING = dt_time(9, 30)
HK_MARKET_CLOSE_MORNING = dt_time(12, 0)
HK_MARKET_OPEN_AFTERNOON = dt_time(13, 0)
HK_MARKET_CLOSE_AFTERNOON = dt_time(16, 0)


class IntradayKlineProvider:
    """
    日内分钟K线数据提供器

    从富途API获取指定股票的分钟K线，支持缓存避免频繁请求。
    """

    def __init__(self, host: str = '127.0.0.1', port: int = 11111,
                 cache_ttl: int = 25):
        """
        Args:
            host: 富途OpenD地址
            port: 富途OpenD端口
            cache_ttl: 缓存有效期（秒），默认25秒略大于一个5分钟K线周期
        """
        self.host = host
        self.port = port
        self.cache_ttl = cache_ttl

        self._ctx: Optional[OpenQuoteContext] = None
        self._ctx_lock = threading.Lock()

        # 缓存：{stock_code: {'bars': DataFrame, 'timestamp': float}}
        self._cache: Dict[str, Dict] = {}
        self._cache_lock = threading.Lock()

    def connect(self) -> bool:
        """连接到富途OpenD"""
        with self._ctx_lock:
            if self._ctx:
                try:
                    self._ctx.close()
                except Exception:
                    pass
            try:
                self._ctx = OpenQuoteContext(host=self.host, port=self.port)
                ret, _ = self._ctx.get_market_snapshot(['HK.00700'])
                if ret == RET_OK:
                    logger.info("IntradayKlineProvider 连接成功")
                    return True
                else:
                    logger.error(f"IntradayKlineProvider 连接测试失败: {_}")
                    self._ctx.close()
                    self._ctx = None
                    return False
            except Exception as e:
                logger.error(f"IntradayKlineProvider 连接异常: {e}")
                self._ctx = None
                return False

    def disconnect(self):
        """断开连接"""
        with self._ctx_lock:
            if self._ctx:
                try:
                    self._ctx.close()
                except Exception:
                    pass
                self._ctx = None
        logger.info("IntradayKlineProvider 已断开")

    def is_market_open(self) -> bool:
        """判断当前是否在港股交易时段"""
        now = datetime.now()
        t = now.time()
        weekday = now.weekday()
        # 周一到周五，0=周一，6=周日
        if weekday >= 5:
            return False
        if HK_MARKET_OPEN_MORNING <= t <= HK_MARKET_CLOSE_MORNING:
            return True
        if HK_MARKET_OPEN_AFTERNOON <= t <= HK_MARKET_CLOSE_AFTERNOON:
            return True
        return False

    def get_min5_bars(self, stock_code: str, force_refresh: bool = False) -> Optional[pd.DataFrame]:
        """
        获取股票的5分钟K线（当日日内）

        Args:
            stock_code: 股票代码，如 'HK.00700'
            force_refresh: 是否强制刷新缓存

        Returns:
            DataFrame columns: time_key, open, close, high, low, volume, turnover
            或 None（获取失败）
        """
        now = time.time()

        # 检查缓存
        if not force_refresh:
            with self._cache_lock:
                cached = self._cache.get(stock_code)
                if cached and (now - cached['timestamp']) < self.cache_ttl:
                    return cached['bars']

        # 获取实时数据
        bars = self._fetch_min5_bars(stock_code)

        # 更新缓存
        with self._cache_lock:
            self._cache[stock_code] = {
                'bars': bars,
                'timestamp': now
            }

        return bars

    def _fetch_min5_bars(self, stock_code: str) -> Optional[pd.DataFrame]:
        """从富途API获取5分钟K线"""
        with self._ctx_lock:
            if not self._ctx:
                ctx = OpenQuoteContext(host=self.host, port=self.port)
            else:
                ctx = self._ctx

        try:
            today = datetime.now().strftime('%Y-%m-%d')
            # 获取当天全部5分钟K线，最多取100根（覆盖全天交易）
            ret, data, _ = ctx.request_history_kline(
                code=stock_code,
                start=today,
                end=today,
                ktype=KLType.K_5M,
                max_count=100
            )

            if ret != RET_OK or data is None or len(data) == 0:
                logger.debug(f"获取5分钟K线失败 {stock_code}: ret={ret}, data={data}")
                return None

            # 整理列名
            df = data[['time_key', 'open', 'close', 'high', 'low', 'volume', 'turnover']].copy()
            df = df.sort_values('time_key').reset_index(drop=True)
            df['time_key'] = pd.to_datetime(df['time_key'])
            return df

        except Exception as e:
            logger.warning(f"_fetch_min5_bars 异常 {stock_code}: {e}")
            return None
        finally:
            if self._ctx is None:
                ctx.close()

    def get_latest_bar(self, stock_code: str) -> Optional[Dict]:
        """获取最新一根5分钟K线"""
        bars = self.get_min5_bars(stock_code)
        if bars is not None and len(bars) > 0:
            row = bars.iloc[-1]
            return {
                'time_key': row['time_key'],
                'open': float(row['open']),
                'close': float(row['close']),
                'high': float(row['high']),
                'low': float(row['low']),
                'volume': int(row['volume']),
            }
        return None

    def get_min1_bars(self, stock_code: str, force_refresh: bool = False) -> Optional[pd.DataFrame]:
        """
        获取股票的1分钟K线（当日日内）

        Args:
            stock_code: 股票代码，如 'HK.00700'
            force_refresh: 是否强制刷新缓存

        Returns:
            DataFrame columns: time_key, open, close, high, low, volume, turnover
            或 None（获取失败）
        """
        cache_key = f"{stock_code}_1m"
        now = time.time()

        # 检查缓存
        if not force_refresh:
            with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached and (now - cached['timestamp']) < self.cache_ttl:
                    return cached['bars']

        # 获取实时数据
        bars = self._fetch_min1_bars(stock_code)

        # 更新缓存
        with self._cache_lock:
            self._cache[cache_key] = {
                'bars': bars,
                'timestamp': now
            }

        return bars

    def _fetch_min1_bars(self, stock_code: str) -> Optional[pd.DataFrame]:
        """从富途API获取1分钟K线"""
        with self._ctx_lock:
            if not self._ctx:
                ctx = OpenQuoteContext(host=self.host, port=self.port)
            else:
                ctx = self._ctx

        try:
            today = datetime.now().strftime('%Y-%m-%d')
            ret, data, _ = ctx.request_history_kline(
                code=stock_code,
                start=today,
                end=today,
                ktype=KLType.K_1M,
                max_count=480  # 最多取480根（约8小时=480分钟）
            )

            if ret != RET_OK or data is None or len(data) == 0:
                logger.debug(f"获取1分钟K线失败 {stock_code}: ret={ret}, data={data}")
                return None

            df = data[['time_key', 'open', 'close', 'high', 'low', 'volume', 'turnover']].copy()
            df = df.sort_values('time_key').reset_index(drop=True)
            df['time_key'] = pd.to_datetime(df['time_key'])
            return df

        except Exception as e:
            logger.warning(f"_fetch_min1_bars 异常 {stock_code}: {e}")
            return None
        finally:
            if self._ctx is None:
                ctx.close()

    def clear_cache(self, stock_code: Optional[str] = None):
        """清除缓存"""
        with self._cache_lock:
            if stock_code:
                self._cache.pop(stock_code, None)
                self._cache.pop(f"{stock_code}_1m", None)
            else:
                self._cache.clear()
