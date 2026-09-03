"""
价格获取模块 - 封装行情数据获取
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple, Dict

from futu import OpenQuoteContext, RET_OK

logger = logging.getLogger(__name__)


class PriceFetcher:
    """价格获取器"""

    def __init__(self, host: str = '127.0.0.1', port: int = 11111,
                 price_cache_ttl: int = 5, price_gap_threshold: float = 0.05):
        self.host = host
        self.port = port
        self.price_cache_ttl = price_cache_ttl
        self.price_gap_threshold = price_gap_threshold

        self.quote_ctx: Optional[OpenQuoteContext] = None
        self.price_cache: Dict[str, Dict] = {}

    def connect(self) -> bool:
        """连接到行情服务器"""
        try:
            if self.quote_ctx:
                self.quote_ctx.close()

            self.quote_ctx = OpenQuoteContext(host=self.host, port=self.port)

            # 测试连接
            ret, data = self.quote_ctx.get_market_snapshot(['HK.00700'])
            if ret == RET_OK and len(data) > 0:
                logger.info("行情服务器连接成功")
                return True
            else:
                logger.error(f"行情连接失败: {data}")
                return False

        except Exception as e:
            logger.error(f"行情连接异常: {e}")
            return False

    def disconnect(self):
        """断开连接"""
        if self.quote_ctx:
            self.quote_ctx.close()
            self.quote_ctx = None
            logger.info("行情连接已断开")

    def get_current_price(self, stock_code: str, force_refresh: bool = False) -> Optional[float]:
        """获取当前价格"""
        try:
            current_time = time.time()

            # 检查缓存
            if (not force_refresh and
                stock_code in self.price_cache and
                current_time - self.price_cache[stock_code]['timestamp'] < self.price_cache_ttl):
                return self.price_cache[stock_code]['price']

            if not self.quote_ctx:
                logger.warning("行情连接未就绪")
                return self.price_cache.get(stock_code, {}).get('price')

            # 获取实时报价
            ret, data = self.quote_ctx.get_market_snapshot([stock_code])
            if ret == RET_OK and len(data) > 0:
                current_price = float(data['last_price'].iloc[0])

                # 价格跳空检测
                old_price = self.price_cache.get(stock_code, {}).get('price')
                if old_price and old_price > 0:
                    price_change_pct = abs(current_price - old_price) / old_price
                    if price_change_pct > self.price_gap_threshold:
                        logger.warning(f"价格跳空 {stock_code}: {old_price:.2f} -> {current_price:.2f} ({price_change_pct*100:.1f}%)")

                # 更新缓存
                self.price_cache[stock_code] = {
                    'price': current_price,
                    'timestamp': current_time,
                    'last_price': old_price
                }

                return current_price
            else:
                logger.warning(f"获取价格失败 {stock_code}")
                return self.price_cache.get(stock_code, {}).get('price')

        except Exception as e:
            logger.error(f"获取价格异常 {stock_code}: {e}")
            return self.price_cache.get(stock_code, {}).get('price')

    def get_open_price(self, stock_code: str) -> Optional[float]:
        """获取今日开盘价"""
        try:
            if not self.quote_ctx:
                return None

            ret, data = self.quote_ctx.get_market_snapshot([stock_code])
            if ret == RET_OK and len(data) > 0:
                open_price = float(data['open_price'].iloc[0])
                return open_price if open_price > 0 else None
            return None
        except Exception as e:
            logger.warning(f"获取开盘价失败 {stock_code}: {e}")
            return None

    def get_previous_close(self, stock_code: str) -> Optional[float]:
        """获取昨日收盘价（上一个交易日收盘价）"""
        try:
            # 优先从 market_snapshot 获取（复用已有连接，无泄漏）
            if self.quote_ctx:
                ret, data = self.quote_ctx.get_market_snapshot([stock_code])
                if ret == RET_OK and len(data) > 0 and 'prev_close_price' in data.columns:
                    prev_close = float(data['prev_close_price'].iloc[0])
                    return prev_close if prev_close > 0 else None

            # 降级：从 K 线数据获取
            from mutifactor.data import FutuHKDataFetcher
            fetcher = FutuHKDataFetcher(host=self.host, port=self.port)

            if not fetcher.connect():
                return None

            try:
                end_date = datetime.now().strftime('%Y-%m-%d')
                start_date = (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d')

                df = fetcher.fetch_stock_kline(stock_code, start_date, end_date)
                if df is not None and len(df) > 0:
                    df = df.sort_values('date').reset_index(drop=True)
                    # 如果有多条数据，返回倒数第二条（上一个交易日收盘价）
                    if len(df) >= 2:
                        close_price = df['close'].iloc[-2]
                    else:
                        close_price = df['close'].iloc[-1]
                    return float(close_price) if close_price is not None else None
                return None
            finally:
                fetcher.disconnect()  # 修复：确保关闭连接

        except Exception as e:
            logger.warning(f"获取昨日收盘价失败 {stock_code}: {e}")
            return None

    def get_today_high_low(self, stock_code: str) -> Tuple[Optional[float], Optional[float]]:
        """获取今日最高价和最低价"""
        try:
            if not self.quote_ctx:
                return None, None

            ret, data = self.quote_ctx.get_market_snapshot([stock_code])
            if ret == RET_OK and len(data) > 0:
                today_high = float(data['high_price'].iloc[0]) if 'high_price' in data.columns else None
                today_low = float(data['low_price'].iloc[0]) if 'low_price' in data.columns else None
                return today_high, today_low
            return None, None
        except Exception as e:
            logger.error(f"获取当日高低价异常 {stock_code}: {e}")
            return None, None

    def get_volume(self, stock_code: str) -> Optional[float]:
        """获取当前成交量"""
        try:
            if not self.quote_ctx:
                return None

            ret, data = self.quote_ctx.get_market_snapshot([stock_code])
            if ret == RET_OK and len(data) > 0:
                volume = float(data['volume'].iloc[0])
                return volume if volume > 0 else None
            return None
        except Exception as e:
            logger.debug(f"获取成交量失败 {stock_code}: {e}")
            return None

    def get_lot_size(self, stock_code: str) -> int:
        """获取每手股数 - 优先从数据库读取"""
        # 优先从数据库获取
        try:
            from mutifactor.infra.yaml_storage import yaml_storage
            lot_size = yaml_storage.get_lot_size(stock_code)
            if lot_size:
                return lot_size
        except Exception:
            pass
