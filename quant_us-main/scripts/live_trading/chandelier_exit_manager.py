"""
美股双吊灯策略实盘管理器

架构：
  ┌─────────────────────────────────────────────────────┐
  │              ChandelierExitManager                   │
  │  ┌──────────────┐  ┌──────────────────────────────┐ │
  │  │ PositionSync  │  │ ExitCheckThreadPool           │ │
  │  │ Thread       │  │ (每持仓一个线程并行检查)       │ │
  │  │ 每5分钟同步   │  │                              │ │
  │  └──────────────┘  └──────────────────────────────┘ │
  │         ↕                    ↕                        │
  │  ┌──────────────┐  ┌──────────────────────────────┐ │
  │  │ ATR Cache     │  │ DualChandelierExitStrategy    │ │
  │  │ (小时ATR缓存) │  │ (止盈止损逻辑)                │ │
  │  │ 每小时刷新    │  │                              │ │
  │  └──────────────┘  └──────────────────────────────┘ │
  │         ↕                                              │
  │  ┌──────────────────────────────────────────────────┐ │
  │  │         FutuConnectionPool (连接池)              │ │
  │  │  QuoteCtx pool (并发获取行情/持仓)               │ │
  │  │  TrdCtx   pool (并发下单平仓)                    │ │
  │  └──────────────────────────────────────────────────┘ │
  └─────────────────────────────────────────────────────┘

配置项：
  - use_extended_hours: 是否使用盘前/盘后/夜盘数据计算ATR
  - check_interval: 平仓检查间隔（秒，默认 60）
  - atr_refresh_interval: ATR缓存刷新间隔（秒，默认 3600 = 1小时）
  - connection_pool_size: 连接池大小
  - ignore_hk_stocks: 是否忽略港股（true=忽略 / false=不忽略）
"""
import math
import sys
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE_DIR)

import threading
import queue
import logging
import time
import signal
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any, Set
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import yaml

# 自动加载配置（绝对路径，修复报错）
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

logger = logging.getLogger(__name__)


# ANSI 颜色代码
class Colors:
    GREEN = '\033[92m'  # 绿色 - 当前价格、盈利
    YELLOW = '\033[93m'  # 黄色 - 止损价
    BLUE = '\033[94m'  # 蓝色 - 止盈价
    CYAN = '\033[96m'  # 青色 - ATR
    MAGENTA = '\033[95m'  # 紫色 - 代码
    RED = '\033[91m'  # 红色 - 亏损、警告
    BOLD = '\033[1m'  # 粗体
    UNDERLINE = '\033[4m'  # 下划线
    RESET = '\033[0m'  # 重置所有属性


# ==============================================================================
# 连接池（完整原版，只修复富途接口）
# ==============================================================================
class FutuConnectionPool:
    def __init__(self, host: str = '127.0.0.1', port: int = 11111,
                 quote_pool_size: int = 3, trade_pool_size: int = 2, market: str = 'US'):
        self.host = host
        self.port = port
        self.market = market
        self.quote_pool = queue.Queue(maxsize=quote_pool_size)
        self.trade_pool = queue.Queue(maxsize=trade_pool_size)
        self._lock = threading.Lock()
        self._closed = False

        for _ in range(quote_pool_size):
            self.quote_pool.put(self._create_quote_ctx())
        for _ in range(trade_pool_size):
            self.trade_pool.put(self._create_trade_ctx())

    def _create_quote_ctx(self):
        from futu import OpenQuoteContext
        ctx = OpenQuoteContext(host=self.host, port=self.port)
        return ctx

    # ====================== 【唯一修复】最新富途美股接口 ======================
    def _create_trade_ctx(self):
        from futu import OpenSecTradeContext, TrdMarket
        ctx = OpenSecTradeContext(
            host=self.host,
            port=self.port,
            filter_trdmarket=TrdMarket.US
        )
        return ctx

    def get_quote_ctx(self, timeout: int = 10):
        class QuoteCtxWrapper:
            def __init__(self, pool, timeout):
                self.pool = pool
                self.timeout = timeout
                self.ctx = None

            def __enter__(self):
                if self.pool._closed:
                    raise RuntimeError("连接池已关闭")
                self.ctx = self.pool.quote_pool.get(timeout=self.timeout)
                return self.ctx

            def __exit__(self, exc_type, exc_val, exc_tb):
                if self.ctx and not self.pool._closed:
                    self.pool.quote_pool.put(self.ctx)

        return QuoteCtxWrapper(self, timeout)

    def get_trade_ctx(self, timeout: int = 10):
        class TradeCtxWrapper:
            def __init__(self, pool, timeout):
                self.pool = pool
                self.timeout = timeout
                self.ctx = None

            def __enter__(self):
                if self.pool._closed:
                    raise RuntimeError("连接池已关闭")
                self.ctx = self.pool.trade_pool.get(timeout=self.timeout)
                return self.ctx

            def __exit__(self, exc_type, exc_val, exc_tb):
                if self.ctx and not self.pool._closed:
                    self.pool.trade_pool.put(self.ctx)

        return TradeCtxWrapper(self, timeout)

    def close(self, timeout: int = 5):
        """优雅关闭连接池，等待所有连接归还"""
        with self._lock:
            if self._closed:
                return
            self._closed = True

        logger.info("🔌 开始关闭富途连接池...")
        # 等待队列中的连接归还（最多等待timeout秒）
        start_time = time.time()
        while (not self.quote_pool.empty() or not self.trade_pool.empty()) and \
                (time.time() - start_time) < timeout:
            time.sleep(0.1)

        # 强制关闭剩余连接
        while not self.quote_pool.empty():
            ctx = self.quote_pool.get()
            try:
                ctx.close()
                logger.debug("行情连接已关闭")
            except Exception as e:
                logger.warning(f"关闭行情连接失败: {e}")

        while not self.trade_pool.empty():
            ctx = self.trade_pool.get()
            try:
                ctx.close()
                logger.debug("交易连接已关闭")
            except Exception as e:
                logger.warning(f"关闭交易连接失败: {e}")

        logger.info("✅ 富途连接池已完全关闭")


# ==============================================================================
# 双吊灯策略（使用独立的正式策略模块）
# ==============================================================================
import sys
import os
BASE_DIR_STRAT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE_DIR_STRAT)

try:
    from mutifactor.strategies.dual_chandelier import DualChandelierExitStrategy as _StrategyBase
    _STRATEGY_OK = True
except ImportError:
    _STRATEGY_OK = False
    _StrategyBase = object  # fallback base


# ==============================================================================
# ATR 缓存（完整原版，一行没少）
# ==============================================================================
class ATRCache:
    def __init__(self, pool, config: Dict[str, Any]):
        self.pool = pool
        self.atr_period = config.get('atr_period', 14)
        self.use_extended = config.get('use_extended_hours', True)
        self._cache: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._last_refresh: Optional[datetime] = None
        self._codes = []
        self._stopped = False

    def stop(self):
        self._stopped = True

    def set_stocks(self, codes: List[str]):
        if self._stopped:
            return
        with self._lock:
            self._codes = codes

    def get_atr(self, code: str) -> Optional[float]:
        with self._lock:
            return self._cache.get(code)

    def get_all_atr(self):
        with self._lock:
            return self._cache.copy()

    def refresh(self):
        if self._stopped:
            return {}

        import pandas as pd
        from futu import KLType, RET_OK

        results = {}
        for code in self._codes:
            try:
                with self.pool.get_quote_ctx() as ctx:
                    ktype = KLType.K_60M if self.use_extended else KLType.K_HOUR
                    ret, data, _ = ctx.request_history_kline(
                        code=code,
                        start=(datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d"),
                        end=datetime.now().strftime("%Y-%m-%d"),
                        ktype=ktype,
                        autype='qfq',
                        extended_time=True,  # 包含美股盘前4:00-9:30 + 盘后16:00-20:00 小时线
                    )
                    if ret != RET_OK or len(data) < self.atr_period:
                        continue

                    high = data['high'].astype(float)
                    low = data['low'].astype(float)
                    close = data['close'].astype(float)
                    tr1 = high - low
                    tr2 = abs(high - close.shift(1))
                    tr3 = abs(low - close.shift(1))
                    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                    atr = tr.rolling(window=self.atr_period).mean().iloc[-1]

                    if pd.notna(atr) and atr > 0:
                        results[code] = float(atr)
            except Exception as e:
                logger.debug(f"{code} ATR计算异常: {e}")

        with self._lock:
            self._cache = results
            self._last_refresh = datetime.now()
        return results


# ==============================================================================
# 持仓状态管理（适配外部策略模块）
# ==============================================================================
class PositionStateManager:
    def __init__(self, config: Dict[str, Any]):
        if _STRATEGY_OK:
            self.strategy = _StrategyBase(config)
        else:
            self.strategy = _DummyStrategy(config)
        self._lock = threading.RLock()
        self._positions: Dict[str, Dict] = {}  # code -> {entry_price, direction, qty, stop_price, profit_price}
        self._stopped = False
        self.ignore_hk = config.get('ignore_hk_stocks', True)

    def stop(self):
        self._stopped = True

    def sync_positions(self, positions: List[Dict]):
        if self._stopped:
            return set()

        with self._lock:
            new_codes = {p['code'] for p in positions if p.get('code')}
            old_codes = set(self._positions.keys())

            for p in positions:
                code = p.get('code')
                if not code:
                    continue

                # ========== 港股开关过滤 ==========
                if self.ignore_hk and code.startswith('HK.'):
                    continue

                if code in self._positions:
                    continue
                self._positions[code] = {
                    'entry_price': p.get('average_cost', 0),
                    'direction': 'long' if p.get('position_side', 'LONG') in ('LONG', 'long') else 'short',
                    'qty': abs(p.get('qty', 0))
                }

            for code in old_codes - new_codes:
                self.strategy.on_exit(code)
                if code in self._positions:
                    del self._positions[code]

            return new_codes

    def get_codes_needing_atr(self):
        with self._lock:
            return [c for c in self._positions if c not in self.strategy.positions]

    def register(self, code, atr, price):
        if self._stopped:
            return

        with self._lock:
            pos = self._positions.get(code)
            if not pos:
                return
            self.strategy.on_entry(
                code,
                pos['entry_price'] or price,
                atr,
                pos['direction']
            )

    def all_codes(self):
        with self._lock:
            return list(self._positions.keys())

    def get_stop_profit(self, code: str):
        """获取止损价和止盈价（从策略状态）"""
        with self._lock:
            info = self.strategy.get_position_info(code)
            if not info:
                return None, None
            return info.get('stop_line'), info.get('profit_line')


# ==============================================================================
# 兜底策略（当外部模块导入失败时使用）
# ==============================================================================
class _DummyStrategy:
    """当 dual_chandelier.py 导入失败时的兜底实现（混合策略：固定%+Trailing+ATR）"""

    def __init__(self, config: Dict):
        self.config = config
        self.fixed_stop_pct = config.get('fixed_stop_pct', 0.05)
        self.breakeven_pct = config.get('breakeven_pct', 0.03)
        self.trailing_activate_pct = config.get('trailing_activate_pct', 0.06)
        self.trailing_pullback_pct = config.get('trailing_pullback_pct', 0.03)
        self.atr_threshold_pct = config.get('atr_threshold_pct', 0.20)
        self.atr_trailing_mult = config.get('atr_trailing_mult', 2.0)
        self.positions = {}

    def on_entry(self, stock_code: str, entry_price: float, current_atr: float, direction: str):
        if direction == 'long':
            stop = round(entry_price * (1 - self.fixed_stop_pct), 4)
        else:
            stop = round(entry_price * (1 + self.fixed_stop_pct), 4)
        self.positions[stock_code] = {
            'entry_price': entry_price,
            'atr': current_atr,
            'direction': direction,
            'qty': 0,
            'stop_price': stop,
            'profit_price': None,
            'highest_price': entry_price,
            'lowest_price': entry_price,
            'breakeven_moved': False,
            'trailing_activated': False,
            'atr_mode_active': False,
        }

    def on_tick(self, code: str, price: float, atr: float):
        pos = self.positions.get(code)
        if not pos:
            return False, "", 0.0
        ep = pos['entry_price']
        direction = pos['direction']

        # 更新极值
        pos['highest_price'] = max(pos['highest_price'], price)
        pos['lowest_price'] = min(pos['lowest_price'], price)

        if direction == 'long':
            pnl_pct = (price - ep) / ep

            # ===== Phase 4: ATR Trailing (≥+20%) =====
            if pnl_pct >= self.atr_threshold_pct:
                if not pos['atr_mode_active']:
                    pos['atr_mode_active'] = True
                    logger.info(f"  [DUMMY-LONG] ★ 切换ATR-Trailing @ {price:.2f}")
                new_stop = round(pos['highest_price'] - atr * self.atr_trailing_mult, 4)
                if new_stop > pos['stop_price']:
                    pos['stop_price'] = new_stop
                pos['profit_price'] = pos['stop_price']

            # ===== Phase 3: 固定% Trailing (+6%~+19%) =====
            elif pnl_pct >= self.trailing_activate_pct:
                if not pos['trailing_activated']:
                    pos['trailing_activated'] = True
                    logger.info(f"  [DUMMY-LONG] Trailing激活 @ {price:.2f}")
                new_stop = round(pos['highest_price'] * (1 - self.trailing_pullback_pct), 4)
                if new_stop > pos['stop_price']:
                    pos['stop_price'] = new_stop
                pos['profit_price'] = pos['stop_price']

            # ===== Phase 2: 保本 (+3%~+5%) =====
            elif pnl_pct >= self.breakeven_pct and not pos['breakeven_moved']:
                pos['stop_price'] = round(ep, 4)
                pos['breakeven_moved'] = True
                logger.info(f"  [DUMMY-LONG] 止损移至保本 @ {ep:.2f}")

            stop_hit = price <= pos['stop_price']
            profit_hit = bool(
                (pos['trailing_activated'] or pos['atr_mode_active'])
                and pos['profit_price'] and price <= pos['profit_price'])

        else:
            pnl_pct = (ep - price) / ep

            # ===== Phase 4: ATR Trailing (≥+20%) =====
            if pnl_pct >= self.atr_threshold_pct:
                if not pos['atr_mode_active']:
                    pos['atr_mode_active'] = True
                    logger.info(f"  [DUMMY-SHORT] ★ 切换ATR-Trailing @ {price:.2f}")
                new_stop = round(pos['lowest_price'] + atr * self.atr_trailing_mult, 4)
                if new_stop < pos['stop_price']:
                    pos['stop_price'] = new_stop
                pos['profit_price'] = pos['stop_price']

            elif pnl_pct >= self.trailing_activate_pct:
                if not pos['trailing_activated']:
                    pos['trailing_activated'] = True
                    logger.info(f"  [DUMMY-SHORT] Trailing激活 @ {price:.2f}")
                new_stop = round(pos['lowest_price'] * (1 + self.trailing_pullback_pct), 4)
                if new_stop < pos['stop_price']:
                    pos['stop_price'] = new_stop
                pos['profit_price'] = pos['stop_price']

            elif pnl_pct >= self.breakeven_pct and not pos['breakeven_moved']:
                pos['stop_price'] = round(ep, 4)
                pos['breakeven_moved'] = True
                logger.info(f"  [DUMMY-SHORT] 止损移至保本 @ {ep:.2f}")
                pos['profit_price'] = pos['stop_price']

            stop_hit = price >= pos['stop_price']
            profit_hit = bool(
                (pos['trailing_activated'] or pos.get('atr_mode_active', False)) and pos['profit_price']
                and price >= pos['profit_price'])

        if stop_hit:
            return True, "STOP_LOSS", price
        if profit_hit:
            return True, "TAKE_PROFIT", price
        return False, "", price

    def on_exit(self, code: str):
        if code in self.positions:
            del self.positions[code]

    def get_all_positions(self):
        return self.positions

    def get_position_info(self, code: str):
        if code not in self.positions:
            return None
        pos = self.positions[code]
        return {
            'stop_line': pos['stop_price'],
            'profit_line': pos.get('profit_price'),
            'direction': pos['direction'],
        }


# ==============================================================================
# 价格订阅（官方状态机严谨版：彻底告别瞎猜！）
# ==============================================================================
class FutuTickSubscriber:
    PRICE_EPSILON = 0.0001

    def __init__(self, pool, config):
        self.pool = pool
        self.interval = config.get("tick_poll_interval", 2)
        self._running = False
        self._thread = None
        self._prices: Dict[str, float] = {}
        # 缓存市场状态，避免每次循环都去请求API耗费资源
        self._market_state_cache = {'time': 0, 'state': 2}  # 默认盘中(2)
        self._lock = threading.RLock()
        self.callback = None
        self.ignore_hk = config.get('ignore_hk_stocks', True)
        self._subscribed_codes = set()

    def set_callback(self, cb):
        self.callback = cb

    def start(self, codes):
        if self._running:
            return

        filtered = [c for c in codes if not (self.ignore_hk and c.startswith('HK.'))]
        with self._lock:
            self._prices = {c: 0.0 for c in filtered}

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info(f"📡 价格订阅器【官方状态机版】启动，监控 {len(filtered)} 只股票")

    def _get_us_market_state(self, ctx) -> int:
        """
        🚀 核心：直接问富途API当前美股处于什么状态
        富途旧版API: get_global_state() 无参数，返回 int
        状态定义: 0(未知), 1(开盘前/盘前), 2(交易中/盘中), 3(收盘/盘后)
        """
        now_ts = time.time()
        # 缓存60秒，减少API压力
        if now_ts - self._market_state_cache['time'] < 60:
            return self._market_state_cache['state']

        try:
            gs = ctx.get_global_state()

            # 兼容新版富途API：返回 (ret, dict)
            if isinstance(gs, tuple) and len(gs) == 2:
                _, gs_data = gs
                market_us = gs_data.get('market_us', '')
                logger.info(f"📡 美股市场状态: {market_us}")
                if market_us == 'MORNING':
                    state_code = 1  # 盘前
                elif market_us in ('AFTERNOON', 'AFTERNOON_CLOSING'):
                    state_code = 2  # 盘中
                else:
                    state_code = 3  # 盘后
                self._market_state_cache = {'time': now_ts, 'state': state_code}
                logger.info(f"🕒 映射状态码: {state_code}")
                return state_code
            elif isinstance(gs, int):
                self._market_state_cache = {'time': now_ts, 'state': gs}
                logger.info(f"🕒 美股官方状态码(旧格式): {gs}")
                return gs
            else:
                logger.warning(f"⚠️ 无法识别市场状态: {type(gs)} = {gs}")

        except Exception as e:
            logger.warning(f"⚠️ 获取美股市场状态失败: {e}")

        return 2  # 出现异常默认按盘中处理，防止策略罢工

    def get_stock_price(self, ctx, code):
        """
        🚀 严谨逻辑：盘中用 last_price，盘前/盘后用 prev_close_price 兜底。
        get_stock_quote 需要先订阅推送才能用，这里直接用 get_market_snapshot。
        """
        from futu import RET_OK, SubType

        # 1. 确保订阅
        if code not in self._subscribed_codes:
            try:
                ret, err_msg = ctx.subscribe([code], [SubType.QUOTE], subscribe_push=True)
                if ret == RET_OK:
                    self._subscribed_codes.add(code)
            except Exception as e:
                logger.error(f"💥 [{code}] 订阅异常: {e}")
                return None

        # 2. 获取快照（last_price / open_price / prev_close_price 都有）
        ret, data = ctx.get_market_snapshot([code])
        if ret != RET_OK:
            logger.warning(f"[{code}] get_market_snapshot 失败, ret={ret}")
            return None
        if data.empty:
            logger.warning(f"[{code}] get_market_snapshot 返回空数据")
            return None

        row = data.iloc[0]
        market_state = self._get_us_market_state(ctx)

        # 3. 根据时段，精准拿价
        target_price = 0.0
        prev_close = float(row.get('prev_close_price', 0) or 0)
        last = float(row.get('last_price', 0) or 0)

        if market_state == 1:
            # 🟢 盘前时段：盘中价格还在，用昨收作锚
            target_price = prev_close if prev_close > 0 else last
            logger.debug(f"🟢 [{code}] 盘前时段 | 采用 prev_close: {target_price}")

        elif market_state == 2:
            # 🟡 盘中时段：用最新成交价
            target_price = last
            logger.debug(f"🟡 [{code}] 盘中时段 | 采用 last_price: {target_price}")

        elif market_state == 3:
            # 🔴 盘后时段：最后成交价就是盘后价
            target_price = last if last > 0 else prev_close
            logger.debug(f"🔴 [{code}] 盘后时段 | 采用 last_price: {target_price}")

        else:
            # ⚪️ 未知状态：保守拿最新价
            target_price = last

        return target_price if target_price > 0 else None

    def _run(self):
        logger.info("📡 价格订阅线程已运行，开始拉取实时价格...")
        while self._running:
            codes = []
            with self._lock:
                codes = list(self._prices.keys())

            if not codes:
                time.sleep(self.interval)
                continue

            try:
                with self.pool.get_quote_ctx() as ctx:
                    for code in codes:
                        price = self.get_stock_price(ctx, code)
                        if price is None:
                            continue

                        with self._lock:
                            old = self._prices.get(code, 0)
                            self._prices[code] = price

                        if self.callback and abs(price - old) >= self.PRICE_EPSILON:
                            try:
                                self.callback(code, price)
                            except Exception as e:
                                logger.error(f"回调异常: {e}", exc_info=True)
            except Exception as e:
                logger.error(f"价格拉取异常: {e}", exc_info=True)

            time.sleep(self.interval)

    def get_price(self, code):
        """对外提供价格：永远返回有意义的值，不会返回0"""
        with self._lock:
            p = self._prices.get(code, 0.0)
            return p if p > 0 else 0.0

    def add_codes(self, codes: List[str]):
        with self._lock:
            for c in codes:
                if self.ignore_hk and c.startswith('HK.'):
                    continue
                if c not in self._prices:
                    self._prices[c] = 0.0
                    logger.info(f"📡 新增监控股票: {c}")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            logger.info("✅ 价格订阅线程已安全退出")
# ==============================================================================
# 止损止盈单管理器（ATR驱动自动挂单/改单）
# ==============================================================================
class StopOrderTracker:
    """
    跟踪每个持仓的富途止损/止盈条件单，ATR变化时自动改单

    订单类型：
      做多止损 → STOP_LIMIT      (aux_price=触发价, price=限价比触发价低一点)
      做多止盈 → LIMIT_IF_TOUCHED (aux_price=触及价, price=限价)
      做空止损 → STOP_LIMIT
      做空止盈 → LIMIT_IF_TOUCHED

    改单策略：ATR刷新后对比上次的挂单价，有变化则 modify_order(NORMAL)
    """

    # 价格最小变动精度（避免微小波动频繁改单）
    PRICE_EPSILON = 0.01

    def __init__(self, pool, live_cfg, dry_run=False):
        self.pool = pool
        self.live_cfg = live_cfg
        self.dry_run = dry_run
        self._lock = threading.RLock()
        self._orders: Dict[str, Dict] = {}
        self._stopped = False

    def stop(self):
        self._stopped = True

    def remove_position(self, code: str):
        """持仓平仓后清理跟踪状态"""
        with self._lock:
            if code in self._orders:
                del self._orders[code]

    def _get_trd_env(self):
        from futu import TrdEnv
        trd_env_str = self.live_cfg.get('trd_env', 'SIMULATE')
        return TrdEnv.SIMULATE if trd_env_str == 'SIMULATE' else TrdEnv.REAL

    def _place_order(self, code: str, direction: str, order_type, trd_side,
                     price: float, qty: float, aux_price: float = None,
                     remark: str = "") -> Optional[int]:
        """下单，返回order_id或None"""
        if self.dry_run:
            logger.info(f"[DRY-RUN 挂单] {code} | 类型:{order_type} "
                       f"方向:{trd_side} 价格:{price:.2f} 触发价:{aux_price} 数量:{int(qty)} 备注:{remark}")
            return f"DRYRUN_{code}_{remark}"

        from futu import RET_OK, OrderType, TimeInForce, TrdSide, TrdEnv
        trd_env = self._get_trd_env()
        try:
            with self.pool.get_trade_ctx() as ctx:
                ret, data = ctx.place_order(
                    price=price,
                    qty=qty,
                    code=code,
                    trd_side=trd_side,
                    order_type=order_type,
                    trd_env=trd_env,
                    time_in_force=TimeInForce.DAY,
                    remark=remark,
                    aux_price=aux_price,
                    fill_outside_rth=True,
                )
                if ret == RET_OK and len(data) > 0:
                    oid = data.iloc[0]['order_id']
                    logger.info(f"[挂单成功] {code} [{remark}] order_id={oid} "
                               f"价格={price:.2f} 触发={aux_price}")
                    return oid
                else:
                    logger.warning(f"[挂单失败] {code} [{remark}] ret={ret} data={data}")
                    return None
        except Exception as e:
            logger.error(f"[挂单异常] {code} [{remark}] {e}", exc_info=True)
            return None

    def _modify_order(self, order_id, new_price: float, new_qty: float,
                      aux_price: float = None) -> bool:
        if self.dry_run:
            logger.info(f"[DRY-RUN 改单] order_id={order_id} "
                       f"新价格={new_price:.2f} 新触发={aux_price}")
            return True

        from futu import RET_OK, ModifyOrderOp
        trd_env = self._get_trd_env()
        try:
            with self.pool.get_trade_ctx() as ctx:
                ret, data = ctx.modify_order(
                    modify_order_op=ModifyOrderOp.NORMAL,
                    order_id=order_id,
                    qty=new_qty,
                    price=new_price,
                    aux_price=aux_price,
                    trd_env=trd_env,
                )
                if ret == RET_OK:
                    return True
                else:
                    logger.warning(f"[改单失败] order_id={order_id} ret={ret} msg={data}")
                    return False
        except Exception as e:
            logger.error(f"[改单异常] order_id={order_id} {e}", exc_info=True)
            return False

    def _cancel_order(self, order_id) -> bool:
        if self.dry_run:
            logger.info(f"[DRY-RUN 撤单] order_id={order_id}")
            return True

        from futu import RET_OK, ModifyOrderOp
        trd_env = self._get_trd_env()
        try:
            with self.pool.get_trade_ctx() as ctx:
                ret, data = ctx.modify_order(
                    modify_order_op=ModifyOrderOp.CANCEL,
                    order_id=order_id,
                    qty=0,
                    price=0,
                    trd_env=trd_env,
                )
                if ret == RET_OK:
                    logger.debug(f"[撤单成功] order_id={order_id}")
                    return True
                else:
                    logger.warning(f"[撤单失败] order_id={order_id} ret={ret}")
                    return False
        except Exception as e:
            logger.warning(f"[撤单异常] order_id={order_id} {e}")
            return False

    def update_orders(self, code: str, direction: str, stop_line: float,
                      profit_line: Optional[float], current_price: float,
                      highest_price: float, lowest_price: float, qty: float,
                      atr: float, entry_price: float = None):
        """
        ATR刷新后调用：对比上次价格，有变化则改单

        返回：(stop_changed, profit_changed, info_dict)
        """
        from futu import OrderType, TrdSide

        with self._lock:
            tracked = self._orders.get(code)

            if not tracked:
                stop_oid = self._place_stop_order(code, direction, stop_line, qty)
                profit_oid = None
                if profit_line is not None:
                    profit_oid = self._place_profit_order(
                        code, direction, profit_line, qty, entry_price, current_price)

                self._orders[code] = {
                    'stop_oid': stop_oid,
                    'profit_oid': profit_oid,
                    'last_stop': stop_line,
                    'last_profit': profit_line or 0.0,
                    'direction': direction,
                    'qty': qty,
                }
                return True, profit_line is not None, self._orders[code]

            stop_changed = False
            profit_changed = False

            if abs(stop_line - tracked['last_stop']) >= self.PRICE_EPSILON:
                if tracked['stop_oid']:
                    ok = self._modify_order(tracked['stop_oid'],
                                           self._stop_limit_price(direction, stop_line),
                                           qty,
                                           aux_price=round(stop_line, 2))
                    if ok:
                        stop_changed = True
                        tracked['last_stop'] = stop_line
                        tracked['qty'] = qty
                        logger.info(f"  {Colors.YELLOW}[止损改单]{Colors.RESET} {code} "
                                   f"{tracked['last_stop']:.4f} → {stop_line:.4f} "
                                   f"(atr={atr:.4f}) oid={tracked['stop_oid']}")
                    else:
                        logger.warning(f"[止损改单失败] {code}, 尝试撤旧挂新...")
                        self._cancel_order(tracked['stop_oid'])
                        tracked['stop_oid'] = self._place_stop_order(
                            code, direction, stop_line, qty)
                        if tracked['stop_oid']:
                            stop_changed = True
                            tracked['last_stop'] = stop_line

            if profit_line is not None:
                old_profit = tracked.get('last_profit', 0)
                if abs(profit_line - old_profit) >= self.PRICE_EPSILON:
                    if tracked.get('profit_oid'):
                        ok = self._modify_order(
                            tracked['profit_oid'],
                            self._profit_limit_price(direction, profit_line),
                            qty,
                            aux_price=round(profit_line, 2))
                        if ok:
                            profit_changed = True
                            tracked['last_profit'] = profit_line
                            logger.info(f"  {Colors.BLUE}[止盈改单]{Colors.RESET} {code} "
                                       f"{old_profit:.4f} → {profit_line:.4f} "
                                       f"oid={tracked['profit_oid']}")
                        else:
                            logger.warning(f"[止盈改单失败] {code}, 尝试撤旧挂新...")
                            self._cancel_order(tracked['profit_oid'])
                            tracked['profit_oid'] = self._place_profit_order(
                                code, direction, profit_line, qty, entry_price, current_price)
                            if tracked['profit_oid']:
                                profit_changed = True
                                tracked['last_profit'] = profit_line
                    elif not tracked.get('profit_oid'):
                        profit_oid = self._place_profit_order(
                            code, direction, profit_line, qty, entry_price, current_price)
                        if profit_oid:
                            tracked['profit_oid'] = profit_oid
                            tracked['last_profit'] = profit_line
                            profit_changed = True
            elif tracked.get('profit_oid') and not tracked.get('profit_line_active', False):
                pass

            return stop_changed, profit_changed, tracked

    def _place_stop_order(self, code: str, direction: str, stop_line: float,
                          qty: float) -> Optional[int]:
        """挂止损单"""
        from futu import OrderType, TrdSide
        if direction == 'long':
            trd_side = TrdSide.SELL
            limit_p = round(stop_line * 0.999, 2)
            return self._place_order(
                code, direction, OrderType.STOP_LIMIT, trd_side,
                price=limit_p, qty=qty, aux_price=round(stop_line, 2),
                remark="CHandelier_STOP"
            )
        else:
            trd_side = TrdSide.BUY
            limit_p = round(stop_line * 1.001, 2)
            return self._place_order(
                code, direction, OrderType.STOP_LIMIT, trd_side,
                price=limit_p, qty=qty, aux_price=round(stop_line, 2),
                remark="CHandelier_STOP_SHORT"
            )

    def _place_profit_order(self, code: str, direction: str, profit_line: float,
                            qty: float, entry_price: float = None,
                            current_price: float = None) -> Optional[int]:
        """挂止盈单"""
        from futu import OrderType, TrdSide
        if direction == 'long' and entry_price and profit_line < entry_price:
            logger.info(f"[止盈跳过] {code} 止盈线{profit_line:.2f}<入场价{entry_price:.2f}，暂不挂单")
            return None
        if direction == 'short' and entry_price and profit_line > entry_price:
            logger.info(f"[止盈跳过] {code} 止盈线{profit_line:.2f}>入场价{entry_price:.2f}，暂不挂单")
            return None

        if current_price and current_price > 0:
            if direction == 'long' and profit_line <= current_price:
                logger.warning(
                    f"[止盈跳过] {code} 止盈线{profit_line:.2f}<=当前市价{current_price:.2f}，"
                    f"LIMIT_IF_TOUCHED 需触发价>市价，暂不挂单")
                return None
            if direction == 'short' and profit_line >= current_price:
                logger.warning(
                    f"[止盈跳过] {code} 止盈线{profit_line:.2f}>=当前市价{current_price:.2f}，"
                    f"LIMIT_IF_TOUCHED 需触发价<市价，暂不挂单")
                return None

        if direction == 'long':
            trd_side = TrdSide.SELL
            return self._place_order(
                code, direction, OrderType.LIMIT_IF_TOUCHED, trd_side,
                price=round(profit_line, 2), qty=qty,
                aux_price=round(profit_line, 2),
                remark="CHandelier_PROFIT"
            )
        else:
            trd_side = TrdSide.BUY
            return self._place_order(
                code, direction, OrderType.LIMIT_IF_TOUCHED, trd_side,
                price=round(profit_line, 2), qty=qty,
                aux_price=round(profit_line, 2),
                remark="CHandelier_PROFIT_SHORT"
            )

    @staticmethod
    def _stop_limit_price(direction: str, stop_line: float) -> float:
        """止损限价单的限价（略差于触发价确保成交）"""
        if direction == 'long':
            return round(stop_line * 0.999, 2)
        return round(stop_line * 1.001, 2)

    @staticmethod
    def _profit_limit_price(direction: str, profit_line: float) -> float:
        """止盈限价单的限价"""
        return round(profit_line, 2)

    def cancel_all_for(self, code: str):
        """平仓后撤销该股票的所有止损止盈单"""
        with self._lock:
            tracked = self._orders.get(code)
            if not tracked:
                return
            oids_to_cancel = []
            if tracked.get('stop_oid'):
                oids_to_cancel.append(('止损', tracked['stop_oid']))
            if tracked.get('profit_oid'):
                oids_to_cancel.append(('止盈', tracked['profit_oid']))
            for label, oid in oids_to_cancel:
                self._cancel_order(oid)
            del self._orders[code]
            if oids_to_cancel:
                logger.info(f"[撤单完成] {code} 已撤销{len(oids_to_cancel)}个订单: "
                           f"{[(l, o) for l, o in oids_to_cancel]}")

    def get_status(self, code: str) -> Optional[Dict]:
        with self._lock:
            return self._orders.get(code)


# ==============================================================================
# 主管理器（优化优雅退出）
# ==============================================================================
class ChandelierExitManager:
    def __init__(self, dry_run=False, config=None):
        if config is None:
            config = yaml.safe_load(open(CONFIG_PATH, 'r', encoding='utf-8'))

        self.dry_run = dry_run
        self.config = config
        self.futu_cfg = config.get("futu", {})
        self.live_cfg = config.get("live_manager", {})
        self.chandelier_cfg = config.get("chandelier", {})
        self.ignore_hk = self.live_cfg.get("ignore_hk_stocks", True)

        logger.info(f"✅ 港股过滤开关: {'开启(忽略港股)' if self.ignore_hk else '关闭(监控港股)'}")

        self.pool = FutuConnectionPool(
            host=self.futu_cfg.get("host", "127.0.0.1"),
            port=self.futu_cfg.get("port", 11111),
            quote_pool_size=self.live_cfg["quote_size"],
            trade_pool_size=self.live_cfg["trd_size"]
        )

        self.atr_cache = ATRCache(self.pool, {
            "atr_period": self.chandelier_cfg.get("atr_period", 14),
            "use_extended_hours": self.live_cfg.get("use_extended_hours", True)
        })

        self.position_mgr = PositionStateManager({
            **self.chandelier_cfg,
            "ignore_hk_stocks": self.ignore_hk
        })

        self.ticker = FutuTickSubscriber(self.pool, {
            **self.live_cfg,
            "ignore_hk_stocks": self.ignore_hk
        })

        self.ticker.set_callback(self._on_tick)
        self.stop_tracker = StopOrderTracker(self.pool, self.live_cfg, dry_run=self.dry_run)

        self._executor = None
        self._running = False
        self._stop_event = threading.Event()
        self._position_thread = None
        self._register_signals()

    def _register_signals(self):
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        if hasattr(signal, 'SIGBREAK'):
            signal.signal(signal.SIGBREAK, self._handle_signal)

    def _handle_signal(self, sig, frame):
        signal_name = signal.Signals(sig).name if sig in signal.Signals.__members__.values() else f"信号 {sig}"
        logger.info(f"\n🛑 收到退出信号: {signal_name}")
        self.stop()

    def _on_tick(self, code, price):
        if not self._running or self._stop_event.is_set():
            return

        atr = self.atr_cache.get_atr(code)
        if not atr:
            return

        saved_state = None
        if code in self.position_mgr.strategy.positions:
            saved_state = self.position_mgr.strategy.positions[code]

        hit, reason, exit_price = self.position_mgr.strategy.on_tick(code, price, atr)
        if hit:
            logger.debug(f"[触发平仓] {code} | {reason} @ {exit_price:.2f}")
            if self._close_position(code, exit_price):
                logger.debug(f"[平仓确认] {code} 策略状态已清理")
            else:
                logger.warning(f"[平仓失败] {code} 恢复策略状态")
                if saved_state:
                    self.position_mgr.strategy.on_entry(
                        code,
                        saved_state.entry_price,
                        atr,
                        saved_state.direction
                    )

    def _sync_positions(self):
        if self._stop_event.is_set():
            return []

        from futu import RET_OK, TrdEnv
        try:
            trd_env_str = self.live_cfg.get('trd_env', 'SIMULATE')
            trd_env = TrdEnv.SIMULATE if trd_env_str == 'SIMULATE' else TrdEnv.REAL
            with self.pool.get_trade_ctx() as ctx:
                ret, data = ctx.position_list_query(trd_env=trd_env)
                if ret != RET_OK:
                    logger.error(f"持仓查询失败，错误码: {ret}")
                    return []
                data = data[data['qty'] != 0]
                return data.to_dict('records')
        except RuntimeError as e:
            if "连接池已关闭" in str(e):
                logger.info("📋 持仓同步：连接池已关闭，停止持仓查询")
        except Exception as e:
            logger.warning(f"持仓同步失败: {e}", exc_info=True)
        return []

    def _calculate_pnl(self, pos: Dict, current_price: float) -> Tuple[float, str, str]:
        qty = pos.get('qty', 0.0)
        average_cost = pos.get('average_cost', 0.0)
        position_side = pos.get('position_side', 'LONG')

        if average_cost <= 0 or current_price <= 0:
            return 0.0, "0.00%", f"{Colors.CYAN}--{Colors.RESET}"

        if position_side == 'LONG':
            pnl_amount = (current_price - average_cost) * abs(qty)
            pnl_percent = ((current_price - average_cost) / average_cost) * 100
        else:
            pnl_amount = (average_cost - current_price) * abs(qty)
            pnl_percent = ((average_cost - current_price) / average_cost) * 100

        pnl_percent_str = f"{pnl_percent:+.2f}%"
        currency = pos.get('currency', 'USD')
        sym = {'HKD': 'HK$', 'USD': 'US$', 'CNY': '¥'}.get(currency, '$')

        if pnl_amount > 0:
            colored = f"{Colors.GREEN}{Colors.BOLD}{sym}{pnl_amount:.2f} ({pnl_percent_str}){Colors.RESET}"
        elif pnl_amount < 0:
            colored = f"{Colors.RED}{Colors.BOLD}{sym}{abs(pnl_amount):.2f} ({pnl_percent_str}){Colors.RESET}"
        else:
            colored = f"{Colors.CYAN}--{Colors.RESET}"
        return pnl_amount, pnl_percent_str, colored

    def _position_worker(self):
        while not self._stop_event.is_set():
            try:
                pos = self._sync_positions()
                codes = self.position_mgr.sync_positions(pos)
                self.atr_cache.set_stocks(list(codes))
                atr_data = self.atr_cache.refresh()

                # ===================== 【关键修复】等待价格加载 =====================
                # 刚启动时，等待价格订阅器把价格拉回来，不拿到有效价格不往下走
                has_valid_price = False
                # 只检查价格订阅器实际监控的股票（过滤港股）
                ticker_codes = [c for c in codes if not (self.ignore_hk and c.startswith('HK.'))]
                codes_str = ', '.join(ticker_codes) if ticker_codes else '无'
                logger.info(f"⌛ 等待价格就绪: [{codes_str}] (忽略港股)")
                for i in range(20):  # 最多等 20 轮（20秒, 因ticker间隔2秒）
                    if all(self.ticker.get_price(code) > 0 for code in ticker_codes):
                        has_valid_price = True
                        break
                    if i == 0:
                        logger.info(f"⏳ 等待实时价格加载（共20秒）...")
                    elif i % 5 == 0:
                        prices_now = {c: f"{self.ticker.get_price(c):.2f}" for c in ticker_codes}
                        logger.info(f"⏳ 仍在等待 ({i+1}/20): {prices_now}")
                    time.sleep(1)
                if not has_valid_price:
                    prices_final = {c: f"{self.ticker.get_price(c):.2f}" for c in ticker_codes}
                    logger.warning(f"⏭️ 价格未就绪 [{prices_final}]，跳过本轮执行（1分钟后重试）")
                    time.sleep(60)
                    continue
                # ====================================================================

                if pos and atr_data:
                    for p in pos:
                        code = p.get('code', '')
                        if not code:
                            continue
                        if self.ignore_hk and code.startswith('HK.'):
                            continue

                        atr_val = atr_data.get(code)
                        if not atr_val:
                            continue

                        current_price = self.ticker.get_price(code)
                        if current_price <= 0:
                            logger.info(f"无法获取实时价格: {code}, 尝试通过 get_stock_price 重新获取...")
                            try:
                                with self.pool.get_quote_ctx() as ctx:
                                    price = self.ticker.get_stock_price(ctx, code)
                                    if price and price > 0:
                                        current_price = price
                                        with self.ticker._lock:
                                            self.ticker._prices[code] = current_price
                            except Exception:
                                pass

                        direction = 'long' if p.get('position_side', 'LONG') in ('LONG', 'long') else 'short'
                        qty = abs(p.get('qty', 0))
                        average_cost = p.get('average_cost', 0)

                        logger.info(
                            f"{Colors.MAGENTA}[ATR更新]{Colors.RESET} {code} | "
                            f"ATR: {Colors.CYAN}{atr_val:.4f}{Colors.RESET} | "
                            f"当前价: {Colors.GREEN}{current_price:.2f}{Colors.RESET}"
                        )

                        if code not in self.position_mgr.strategy.positions:
                            self.position_mgr.register(code, atr_val, current_price)

                        state = self.position_mgr.strategy.positions.get(code)
                        if state:
                            state.recompute(atr=atr_val, current_price=current_price)
                            new_stop = state.stop_line
                            new_profit = state.profit_line
                            highest = state.highest_price
                            lowest = state.lowest_price

                            dir_label = 'LONG' if direction == 'long' else 'SHORT'
                            logger.info(
                                f"  {dir_label} | 最高:{highest:.2f} 最低:{lowest:.2f} | "
                                f"止损: {Colors.YELLOW}{new_stop:.2f}{Colors.RESET}"
                                + (f" | 止盈: {Colors.BLUE}{new_profit:.2f}{Colors.RESET}" if new_profit else " | 止盈: 未激活")
                            )

                            self.stop_tracker.update_orders(
                                code, direction, new_stop, new_profit,
                                current_price, highest, lowest, qty, atr_val, average_cost
                            )

                if pos:
                    logger.info("📌 当前持仓状态:")
                    for p in pos:
                        code = p.get('code', '')
                        if not code:
                            continue
                        if self.ignore_hk and code.startswith('HK.'):
                            continue

                        current_price = self.ticker.get_price(code) or 0.0
                        if current_price <= 0:
                            try:
                                with self.pool.get_quote_ctx() as ctx:
                                    price = self.ticker.get_stock_price(ctx, code)
                                    if price:
                                        current_price = price
                            except Exception:
                                pass

                        _, _, colored_pnl = self._calculate_pnl(p, current_price)
                        stop_price, profit_price = self.position_mgr.get_stop_profit(code)

                        # 安全格式化，修复 None 崩溃
                        atr_str = f"{atr_data.get(code, 0):.2f}" if code in atr_data else "无"
                        current_str = f"{current_price:.2f}" if current_price else "--"
                        stop_str = f"{stop_price:.2f}" if stop_price is not None else "无"
                        profit_str = f"{profit_price:.2f}" if profit_price is not None else "无"

                        logger.info(
                            f"  - {Colors.MAGENTA}{Colors.BOLD}{code}{Colors.RESET} | "
                            f"当前价:{Colors.GREEN}{current_str}{Colors.RESET} | "
                            f"盈亏:{colored_pnl} | "
                            f"止损:{Colors.YELLOW}{stop_str}{Colors.RESET} | "
                            f"止盈:{Colors.BLUE}{profit_str}{Colors.RESET} | "
                            f"ATR:{Colors.CYAN}{atr_str}{Colors.RESET}"
                        )
                else:
                    logger.info("📌 当前无持仓")

                current_codes = self.position_mgr.all_codes()
                self.ticker.add_codes(current_codes)

                for c in self.position_mgr.get_codes_needing_atr():
                    atr = atr_data.get(c)
                    p = self.ticker.get_price(c)
                    if atr and p:
                        self.position_mgr.register(c, atr, p)

                for code in list(self.position_mgr.strategy.positions.keys()):
                    atr = atr_data.get(code)
                    price = self.ticker.get_price(code)
                    if not atr or not price:
                        continue
                    hit, reason, exit_price = self.position_mgr.strategy.on_tick(code, price, atr)
                    if hit:
                        logger.warning(f"[兜底检查] {code} | {reason} @ {exit_price:.2f}")
                        self._close_position(code, exit_price)

            except Exception as e:
                logger.error(f"线程异常: {e}", exc_info=True)
            time.sleep(60)

    def start(self):
        if self._running:
            return
        logger.info("🚀 启动双吊灯实盘监控...")
        self._running = True
        self._executor = ThreadPoolExecutor(max_workers=self.live_cfg.get("thread_pool_size", 4))
        self._position_thread = threading.Thread(target=self._position_worker, daemon=True)
        self._position_thread.start()
        time.sleep(2)
        codes = self.position_mgr.all_codes()
        self.ticker.start(codes)
        logger.info("✅ 监控已启动")

    def stop(self):
        if not self._running:
            return
        logger.info("\n📤 优雅退出...")
        self._running = False
        self._stop_event.set()
        self.ticker.stop()
        self.position_mgr.stop()
        self.atr_cache.stop()
        self.stop_tracker.stop()
        if self._executor:
            self._executor.shutdown(wait=True)
        if self._position_thread:
            self._position_thread.join(timeout=10)
        self.pool.close()
        logger.info("🎉 已安全退出")

    def _close_position(self, code: str, exit_price: float = 0.0) -> bool:
        trd_env_str = self.live_cfg.get('trd_env', 'SIMULATE')
        try:
            from futu import RET_OK, OrderType, TrdSide, TrdEnv
            trd_env = TrdEnv.SIMULATE if trd_env_str == 'SIMULATE' else TrdEnv.REAL

            with self.pool.get_trade_ctx() as ctx:
                ret, pos_data = ctx.position_list_query(trd_env=trd_env, code=code)
            if ret != RET_OK or pos_data.empty:
                return False

            pos = pos_data.iloc[0]
            side = pos['position_side']
            qty = pos['can_sell_qty'] if side == 'LONG' else pos['can_buy_qty']
            if qty <= 0:
                return False

            trd_side = TrdSide.SELL if side == 'LONG' else TrdSide.BUY
            if self.dry_run:
                logger.info(f"[DRY-RUN] 平仓 {code} x {qty}")
                return True

            with self.pool.get_trade_ctx() as ctx:
                ret, _ = ctx.place_order(
                    price=0, qty=qty, code=code, trd_side=trd_side,
                    order_type=OrderType.MARKET, trd_env=trd_env
                )

            if ret == RET_OK:
                self.stop_tracker.cancel_all_for(code)
                logger.info(f"[平仓成功] {code}")
                return True
            return False
        except Exception as e:
            logger.warning(f"平仓异常 {code}: {e}")
            return False


if __name__ == "__main__":
    manager = ChandelierExitManager(dry_run=True)
    try:
        manager.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        manager.stop()