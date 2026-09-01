"""
富途API共用工具
从 hk_fetcher 提取的限流器、重试装饰器、K线类型映射等通用逻辑
"""
import functools
import time
import logging
from typing import List

from mutifactor.data.base_fetcher import KLineType

# 尝试导入futu
try:
    from futu import KLType
    FUTU_AVAILABLE = True
except ImportError:
    FUTU_AVAILABLE = False

logger = logging.getLogger(__name__)


class RateLimitConfig:
    """API限流配置"""
    BASE_INTERVAL = 0.7
    MAX_RETRIES = 3
    BACKOFF_BASE = 1.0
    MAX_BACKOFF = 30.0
    MAX_REQUESTS_PER_WINDOW = 40
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
    """重试装饰器 - 支持指数退避"""
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
                    raise
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
        self.min_interval = min_interval
        self.max_requests_per_window = max_requests_per_window
        self.window_seconds = window_seconds
        self.last_request_time = 0.0
        self.request_timestamps: List[float] = []

    def wait_if_needed(self) -> None:
        """如果需要，等待以遵守限流规则"""
        current_time = time.time()
        self.request_timestamps = [
            ts for ts in self.request_timestamps
            if current_time - ts < self.window_seconds
        ]
        if len(self.request_timestamps) >= self.max_requests_per_window:
            wait_time = self.window_seconds - (current_time - self.request_timestamps[0])
            if wait_time > 0:
                logger.debug(f"达到限流限制（{self.max_requests_per_window}次/{self.window_seconds}秒），等待 {wait_time:.1f} 秒")
                time.sleep(wait_time)
                self.request_timestamps = []
        time_since_last = current_time - self.last_request_time
        if time_since_last < self.min_interval:
            sleep_time = self.min_interval - time_since_last
            time.sleep(sleep_time)
        self.last_request_time = time.time()
        self.request_timestamps.append(self.last_request_time)


def convert_kline_type(ktype: KLineType):
    """转换K线类型枚举（通用，HK/US共用）"""
    if not FUTU_AVAILABLE:
        return None
    mapping = {
        KLineType.MIN_1: KLType.K_1M,
        KLineType.MIN_5: KLType.K_5M,
        KLineType.MIN_15: KLType.K_15M,
        KLineType.MIN_30: KLType.K_30M,
        KLineType.MIN_60: KLType.K_60M,
        KLineType.DAY: KLType.K_DAY,
        KLineType.WEEK: KLType.K_WEEK,
        KLineType.MONTH: KLType.K_MON,
    }
    return mapping.get(ktype, KLType.K_DAY)
