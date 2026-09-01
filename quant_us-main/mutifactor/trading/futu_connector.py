"""
富途连接管理器 - 连接池实现
QuoteCtx 用于获取行情/持仓数据
TrdCtx  用于下单平仓
"""
import threading
import queue
import logging
import time
from typing import Optional, Dict, Any
from contextlib import contextmanager

from mutifactor.data.futu_common import FUTU_AVAILABLE, APIError

logger = logging.getLogger(__name__)


class ConnectionHealth:
    """连接健康状态追踪"""

    def __init__(self, name: str):
        self.name = name
        self.last_used: float = 0
        self.use_count: int = 0
        self.error_count: int = 0
        self.last_error: str = ""
        self.alive: bool = True

    def record_use(self):
        self.last_used = time.time()
        self.use_count += 1

    def record_error(self, error: str):
        self.error_count += 1
        self.last_error = str(error)[:100]
        if self.error_count >= 3:
            self.alive = False
            logger.warning(f"[{self.name}] 连接错误 {self.error_count} 次，标记不可用: {self.last_error}")


class FutuConnectionPool:
    """
    富途连接池

    提供：
    - QuoteCtx 连接池（多个连接用于并发获取行情/持仓数据）
    - TrdCtx    连接池（多个连接用于并发下单）
    - 连接健康检测 + 自动重连
    - 线程安全
    """

    def __init__(self, host: str = '127.0.0.1', port: int = 11111,
                 quote_pool_size: int = 3, trade_pool_size: int = 2,
                 market: str = 'US'):
        self.host = host
        self.port = port
        self.market = market  # 'US' or 'HK'
        self.quote_pool_size = quote_pool_size
        self.trade_pool_size = trade_pool_size
        self._lock = threading.Lock()
        self._closed = False

        # 连接队列（用 Queue 实现池）
        self._quote_queue: queue.Queue = queue.Queue(maxsize=quote_pool_size)
        self._trade_queue: queue.Queue = queue.Queue(maxsize=trade_pool_size)

        # 健康状态记录
        self._quote_health: Dict[int, ConnectionHealth] = {}
        self._trade_health: Dict[int, ConnectionHealth] = {}

        # 已初始化的连接计数
        self._quote_count = 0
        self._trade_count = 0

        # 初始化连接池
        self._init_pool()

        logger.info(f"FutuConnectionPool 初始化: host={host}:{port} market={market} "
                   f"quote={quote_pool_size} trade={trade_pool_size}")

    def _create_quote_ctx(self):
        """创建单个 QuoteCtx 连接"""
        if not FUTU_AVAILABLE:
            raise ImportError("未安装 futu-api，请运行: pip install futu-api")

        from futu import OpenQuoteContext, RET_OK, Market

        ctx = OpenQuoteContext(host=self.host, port=self.port)

        # 订阅基础行情（US.ALL 或者 HK.HKEX）
        market = Market.US if self.market == 'US' else Market.HK
        ret, data = ctx.get_stock_basicinfo(market=market, stock_type=None)
        if ret != RET_OK:
            logger.warning(f"QuoteCtx 连接测试失败: {data}")
            ctx.close()
            raise ConnectionError(f"QuoteCtx 连接失败: {data}")

        return ctx

    def _create_trade_ctx(self):
        """创建单个 TrdCtx 连接（证券交易，非期货）"""
        if not FUTU_AVAILABLE:
            raise ImportError("未安装 futu-api，请运行: pip install futu-api")

        from futu import OpenSecTradeContext, TrdMarket

        trd_market = TrdMarket.US if self.market == 'US' else TrdMarket.HK
        ctx = OpenSecTradeContext(filter_trdmarket=trd_market,
                                  host=self.host, port=self.port)
        return ctx

    def _init_pool(self):
        """初始化连接池"""
        # 初始化 QuoteCtx 池
        for i in range(self.quote_pool_size):
            try:
                ctx = self._create_quote_ctx()
                self._quote_queue.put(ctx)
                self._quote_count += 1
                idx = self._quote_count
                self._quote_health[id(ctx)] = ConnectionHealth(f"Quote-{idx}")
                logger.info(f"QuoteCtx [{idx}] 入池")
            except Exception as e:
                logger.error(f"QuoteCtx [{i+1}] 初始化失败: {e}")

        # 初始化 TrdCtx 池
        for i in range(self.trade_pool_size):
            try:
                ctx = self._create_trade_ctx()
                self._trade_queue.put(ctx)
                self._trade_count += 1
                idx = self._trade_count
                self._trade_health[id(ctx)] = ConnectionHealth(f"Trd-{idx}")
                logger.info(f"TrdCtx [{idx}] 入池")
            except Exception as e:
                logger.error(f"TrdCtx [{i+1}] 初始化失败: {e}")

    @contextmanager
    def get_quote_ctx(self, timeout: float = 10.0):
        """获取一个 QuoteCtx（线程安全，用完自动归还）"""
        ctx = None
        try:
            ctx = self._quote_queue.get(timeout=timeout)
            health = self._quote_health.get(id(ctx))
            if health:
                health.record_use()
            yield ctx
        except queue.Empty:
            logger.warning("QuoteCtx 池耗尽，等待超时")
            raise ConnectionError("QuoteCtx 池耗尽")
        finally:
            if ctx is not None:
                health = self._quote_health.get(id(ctx))
                if health and health.alive:
                    try:
                        self._quote_queue.put(ctx, timeout=1.0)
                    except queue.Full:
                        ctx.close()
                        logger.warning("QuoteCtx 归还队列满，关闭连接")
                        if id(ctx) in self._quote_health:
                            del self._quote_health[id(ctx)]
                else:
                    # 健康检查失败，关闭并重建
                    try:
                        ctx.close()
                    except Exception:
                        pass
                    if id(ctx) in self._quote_health:
                        del self._quote_health[id(ctx)]
                    self._rebuild_quote_ctx()

    @contextmanager
    def get_trade_ctx(self, timeout: float = 10.0):
        """获取一个 TrdCtx（线程安全，用完自动归还）"""
        ctx = None
        try:
            ctx = self._trade_queue.get(timeout=timeout)
            health = self._trade_health.get(id(ctx))
            if health:
                health.record_use()
            yield ctx
        except queue.Empty:
            logger.warning("TrdCtx 池耗尽，等待超时")
            raise ConnectionError("TrdCtx 池耗尽")
        finally:
            if ctx is not None:
                health = self._trade_health.get(id(ctx))
                if health and health.alive:
                    try:
                        self._trade_queue.put(ctx, timeout=1.0)
                    except queue.Full:
                        ctx.close()
                        logger.warning("TrdCtx 归还队列满，关闭连接")
                        if id(ctx) in self._trade_health:
                            del self._trade_health[id(ctx)]
                        self._rebuild_trade_ctx()
                else:
                    try:
                        ctx.close()
                    except Exception:
                        pass
                    if id(ctx) in self._trade_health:
                        del self._trade_health[id(ctx)]
                    self._rebuild_trade_ctx()

    def _rebuild_quote_ctx(self):
        """重建一个 QuoteCtx"""
        try:
            ctx = self._create_quote_ctx()
            self._quote_queue.put(ctx)
            self._quote_count += 1
            idx = self._quote_count
            self._quote_health[id(ctx)] = ConnectionHealth(f"Quote-{idx}")
            logger.info(f"QuoteCtx [{idx}] 重建入池")
        except Exception as e:
            logger.error(f"QuoteCtx 重建失败: {e}")

    def _rebuild_trade_ctx(self):
        """重建一个 TrdCtx"""
        try:
            ctx = self._create_trade_ctx()
            self._trade_queue.put(ctx)
            self._trade_count += 1
            idx = self._trade_count
            self._trade_health[id(ctx)] = ConnectionHealth(f"Trd-{idx}")
            logger.info(f"TrdCtx [{idx}] 重建入池")
        except Exception as e:
            logger.error(f"TrdCtx 重建失败: {e}")

    def get_pool_status(self) -> Dict[str, Any]:
        """获取连接池状态"""
        return {
            'quote_available': self._quote_queue.qsize(),
            'quote_total': self._quote_pool_size,
            'trade_available': self._trade_queue.qsize(),
            'trade_total': self._trade_pool_size,
            'quote_health': {idx: {'use_count': h.use_count, 'error_count': h.error_count,
                                   'alive': h.alive}
                            for idx, h in self._quote_health.items()},
            'trade_health': {idx: {'use_count': h.use_count, 'error_count': h.error_count,
                                   'alive': h.alive}
                            for idx, h in self._trade_health.items()},
        }

    def close(self):
        """关闭所有连接"""
        with self._lock:
            self._closed = True
            closed = []
            while True:
                try:
                    ctx = self._quote_queue.get_nowait()
                    ctx.close()
                    closed.append('QuoteCtx')
                except queue.Empty:
                    break
            while True:
                try:
                    ctx = self._trade_queue.get_nowait()
                    ctx.close()
                    closed.append('TrdCtx')
                except queue.Empty:
                    break
            logger.info(f"连接池已关闭，共关闭 {len(closed)} 个连接")
