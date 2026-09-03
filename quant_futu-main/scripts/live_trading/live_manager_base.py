"""
实盘交易管理器基类
统一港股和美股实盘交易逻辑
"""
import logging
import signal
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, time as dt_time, timedelta
from enum import Enum, auto
from typing import Dict, List, Optional, Any, Tuple, Set

from mutifactor.trading import FutuTrader, ConnectionState

from .state_persistence import StatePersistence
from .price_fetcher import PriceFetcher
from .buy_timing import BuyTimingStrategy

logger = logging.getLogger(__name__)


class TradingState(Enum):
    """交易状态"""
    STOPPED = auto()
    RUNNING = auto()
    PAUSED = auto()
    ERROR = auto()


class LiveTradingManager(ABC):
    """实盘交易管理器基类"""

    def __init__(self, config: Dict, market_type: str):
        """
        初始化交易管理器

        Args:
            config: 配置字典
            market_type: 市场类型 ('HK' 或 'US')
        """
        self.config = config
        self.market_type = market_type
        self.state = TradingState.STOPPED
        self.connection_state = ConnectionState.DISCONNECTED

        # 初始化各模块
        self.state_persistence = StatePersistence()

        futu_config = config.get('trading', {}).get('futu', {})
        self.price_fetcher = PriceFetcher(
            host=futu_config.get('host', '127.0.0.1'),
            port=futu_config.get('port', 11111),
            price_cache_ttl=config.get('trading', {}).get('live_trading', {}).get('price_cache_ttl', 5)
        )

        self.buy_timing = BuyTimingStrategy(config)

        self.trader: Optional[FutuTrader] = None
        self.position_manager = None

        # 交易循环控制
        self.stop_event = threading.Event()
        self.buy_thread = None  # 买入线程
        self.position_check_thread = None  # 持仓检查线程

        # 选股缓存
        self.cached_selected_stocks: Optional[List[str]] = None
        self.last_selection_time = 0
        self.selection_cache_ttl = 300

        # K线数据缓存 - 选股时保存,止盈止损时复用
        self.kline_cache: Dict[str, 'pd.DataFrame'] = {}
        self.last_kline_update_date = None

        # 共享数据获取器 - 复用连接避免资源泄漏
        self._shared_fetcher = None

        # 每日状态
        self.last_trading_date = None
        
        # 每日持仓同步状态
        self.last_position_sync_date = None
        self.last_position_sync_timestamp = 0  # 上次持仓同步时间戳（用于15分钟间隔同步）

        # 每日数据同步状态（收盘后同步股票信息）
        self.last_data_sync_date = None

        # 重试计数
        self.retry_count = 0

        # 日志状态缓存
        self._last_buy_status = None
        self._last_trigger_reason = None
        self._last_buy_log_key = None

        # ==================== LLM 顾问模块初始化 ====================
        # 延迟导入，避免未安装 jsonschema 时 import 级报错
        self.llm_advisor = None
        self.llm_logger = None
        self.context_builder = None
        self.llm_shadow_mode = True
        self.llm_cooldown = {}  # {trigger: 下次可调用时间戳}
        self._init_llm()

        # 信号处理
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _init_llm(self):
        """初始化 LLM 顾问模块（可降级）"""
        llm_cfg = self.config.get('llm', {})
        if not llm_cfg.get('enabled', False):
            logger.info("[LLM] 未启用，跳过初始化")
            return

        try:
            from mutifactor.llm import LLMAdvisor, DecisionLogger, ContextBuilder
            self.llm_advisor = LLMAdvisor(llm_cfg)
            self.llm_logger = DecisionLogger()
            self.context_builder = ContextBuilder()
            self.llm_shadow_mode = llm_cfg.get('shadow_mode', True)
            self.llm_cooldown_seconds = llm_cfg.get('cooldown_seconds', {})
            self.llm_use_in = llm_cfg.get('use_in', {})
            self.llm_safety = llm_cfg.get('safety', {})
            logger.info(
                f"[LLM] 初始化完成: enabled=True, shadow_mode={self.llm_shadow_mode}, "
                f"use_in={self.llm_use_in}"
            )
        except Exception as e:
            logger.warning(f"[LLM] 初始化失败，降级为纯规则: {e}")
            self.llm_advisor = None

    def _llm_can_call(self, trigger: str) -> bool:
        """检查冷却期"""
        cooldown = self.llm_cooldown_seconds.get(trigger, 60)
        next_call = self.llm_cooldown.get(trigger, 0)
        if time.time() < next_call:
            return False
        self.llm_cooldown[trigger] = time.time() + cooldown
        return True

    def _handle_signal(self, signum, _frame):
        """处理退出信号"""
        if self.state == TradingState.STOPPED:
            logger.info(f"收到信号 {signum}, 但已在停止状态")
            return
        logger.info(f"收到信号 {signum}, 正在停止...")
        self.stop()

    @abstractmethod
    def _create_position_manager(self) -> Any:
        """创建持仓管理器（子类实现）"""
        pass

    @abstractmethod
    def _get_selection_time_window(self) -> Tuple[dt_time, dt_time]:
        """
        获取选股时间窗口（子类实现）

        Returns:
            (开始时间, 结束时间)
        """
        pass

    def _get_trading_time_window(self) -> Tuple[dt_time, dt_time]:
        """
        获取完整交易时间窗口（用于平仓检查）

        Returns:
            默认返回全天00:00-23:59，子类可覆盖
        """
        return dt_time(0, 0), dt_time(23, 59)

    @abstractmethod
    def _create_trader(self, futu_config: Dict, env) -> FutuTrader:
        """创建交易器（子类实现）"""
        pass

    @abstractmethod
    def _get_stock_name(self, stock_code: str) -> str:
        """获取股票名称（子类实现）"""
        pass

    @abstractmethod
    def _get_data_fetcher_class(self):
        """获取数据获取器类（子类实现）"""
        pass

    @abstractmethod
    def _calculate_buy_quantity(self, stock_code: str, per_stock_capital: float,
                                current_price: float, total_remaining: float) -> int:
        """
        计算买入数量（子类实现）

        Returns:
            买入数量，0 表示跳过
        """
        pass

    def _get_today_bought_stocks(self) -> Set[str]:
        """
        获取今日已买入的股票（子类可覆盖）

        Returns:
            今日已买入股票代码集合
        """
        return self.state_persistence.get_today_bought_stocks()

    def _save_selection_results_impl(self, stock_details: List[Dict]):
        """
        保存选股结果实现（子类可覆盖）

        Args:
            stock_details: 股票详情列表
        """
        self.state_persistence.save_selection_results(stock_details)

    def connect(self) -> bool:
        """连接到交易服务器"""
        try:
            self.connection_state = ConnectionState.CONNECTING
            logger.info(f"=" * 70)
            logger.info(f"正在连接{self.market_type}交易服务器...")
            logger.info(f"=" * 70)

            # ==================== 连接行情 ====================
            logger.info(f"[步骤1/7] 连接行情服务...")
            if not self.price_fetcher.connect():
                logger.error(f"❌ {self.market_type}行情服务连接失败")
                return False
            
            # 验证行情连接（测试获取价格）
            test_stock = 'HK.00700'
            test_price = self.price_fetcher.get_current_price(test_stock, force_refresh=True)
            if test_price is None or test_price <= 0:
                logger.error(f"❌ {self.market_type}行情服务验证失败（无法获取{test_stock}价格）")
                self.price_fetcher.disconnect()
                return False
            
            logger.info(f"✅ {self.market_type}行情服务连接成功（{test_stock} = {test_price:.2f}）")

            # ==================== 连接交易 ====================
            logger.info(f"[步骤2/7] 连接交易服务...")
            futu_config = self.config.get('trading', {}).get('futu', {})
            env_str = self.config.get('trading', {}).get('env', 'SIMULATE')

            from futu import TrdEnv
            env = TrdEnv.SIMULATE if env_str.upper() == 'SIMULATE' else TrdEnv.REAL

            self.trader = self._create_trader(futu_config, env)

            if not self.trader.connect():
                logger.error(f"❌ {self.market_type}交易服务连接失败")
                self.price_fetcher.disconnect()
                return False
            
            # 验证交易连接（测试查询账户）
            try:
                account_info = self.trader.get_account_info()
                if not account_info or account_info.get('cash') is None:
                    logger.error(f"❌ {self.market_type}交易服务验证失败（无法获取账户信息）")
                    self.trader.disconnect()
                    self.price_fetcher.disconnect()
                    return False
                logger.info(f"✅ {self.market_type}交易服务连接成功（总资产: {account_info['total_assets']:.2f}, 可用购买力: {account_info['cash']:.2f}）")
            except Exception as e:
                logger.error(f"❌ {self.market_type}交易服务验证失败: {e}")
                self.trader.disconnect()
                self.price_fetcher.disconnect()
                return False

            # ==================== 初始化共享数据获取器 ====================
            logger.info(f"[步骤3/7] 初始化共享数据获取器...")
            fetcher_class = self._get_data_fetcher_class()
            self._shared_fetcher = fetcher_class(
                host=futu_config.get('host', '127.0.0.1'),
                port=futu_config.get('port', 11111)
            )
            if not self._shared_fetcher.connect():
                logger.error(f"❌ {self.market_type}共享数据获取器连接失败")
                self.trader.disconnect()
                self.price_fetcher.disconnect()
                self._shared_fetcher = None
                return False
            logger.info(f"✅ {self.market_type}共享数据获取器连接成功")

            # ==================== 清空K线缓存（重启时强制刷新）====================
            logger.info(f"[步骤4/7] 清空日内K线缓存...")
            if self.buy_timing._intraday_kline_provider:
                self.buy_timing._intraday_kline_provider.clear_cache()
                logger.info(f"✅ {self.market_type}日内K线缓存已清空")

            # ==================== 同步日K线数据（启动时拉取最新）====================
            logger.info(f"[步骤5/7] 同步日K线数据...")
            try:
                current_time = datetime.now().time()
                trading_start, trading_end = self._get_trading_time_window()
                current_date = datetime.now().date()
                
                should_sync = False
                sync_reason = ""
                
                # 条件1：交易时段附近（开盘前1小时到收盘后2小时）
                near_trading_start = dt_time(trading_start.hour - 1 if trading_start.hour > 0 else 0, trading_start.minute)
                near_trading_end = dt_time(trading_end.hour + 2 if trading_end.hour + 2 < 24 else 23, 59)
                if near_trading_start <= current_time <= near_trading_end:
                    should_sync = True
                    sync_reason = "交易时段附近"
                
                # 条件2：收盘后（今日数据已完整，但数据库可能未同步）
                after_close = dt_time(trading_end.hour + 1 if trading_end.hour + 1 < 24 else 23, 0)
                if current_time >= after_close and self.last_data_sync_date != current_date:
                    should_sync = True
                    sync_reason = "收盘后首次启动"
                
                # 条件3：美股（跨日问题，美股收盘是北京次日凌晨5点）
                if self.market_type == 'US':
                    should_sync = True
                    sync_reason = "美股强制同步"
                
                if should_sync:
                    logger.info(f"→ 触发日K线同步: {sync_reason}")
                    self._daily_data_sync(require_latest=True)
                    self.last_data_sync_date = current_date
                    logger.info(f"✅ {self.market_type}日K线数据同步完成")
                else:
                    logger.info(f"⏭️ {self.market_type}跳过日K线同步（已在今日同步过）")
            except Exception as e:
                logger.warning(f"⚠️ {self.market_type}日K线同步失败: {e}（不影响启动）")

            # ==================== 初始化持仓 ====================
            logger.info(f"[步骤6/7] 初始化持仓管理器...")
            self.position_manager = self._create_position_manager()
            # 传递实盘管理器引用,用于获取K线缓存
            self.position_manager.live_manager = self
            self.position_manager.load_positions()

            # ==================== 同步持仓（仅在启动时执行一次）====================
            logger.info(f"[步骤7/7] 同步券商持仓（启动时同步）...")
            if not self.position_manager.sync_with_broker():
                logger.warning(f"⚠️  {self.market_type}持仓同步失败，使用本地持仓数据")
            else:
                logger.info(f"✅ {self.market_type}持仓同步成功")
                self.last_position_sync_date = datetime.now().date()
                self.last_position_sync_timestamp = time.time()

            self.connection_state = ConnectionState.CONNECTED
            self.retry_count = 0
            logger.info(f"=" * 70)
            logger.info(f"🎉 {self.market_type}交易服务器连接成功！")
            logger.info(f"=" * 70)
            return True

        except (OSError, IOError) as e:
            logger.error(f"❌ {self.market_type}连接失败 - 网络错误: {e}")
            self.connection_state = ConnectionState.DISCONNECTED
            return False
        except Exception as e:
            logger.error(f"❌ {self.market_type}连接失败: {type(e).__name__}: {e}", exc_info=True)
            self.connection_state = ConnectionState.DISCONNECTED
            return False

    def check_connection(self) -> bool:
        """检查连接状态"""
        if self.connection_state != ConnectionState.CONNECTED:
            wait_time = min(2 ** self.retry_count * 15, 300)
            self.retry_count += 1
            logger.warning(f"{self.market_type}连接断开, {wait_time}秒后重试...")
            time.sleep(wait_time)
            return self.connect()
        return True

    def run_buy_loop(self):
        """运行买入循环（选股 + 买入）"""
        thread_name = threading.current_thread().name
        logger.info(f"[{thread_name}] 启动{self.market_type}买入循环...")

        while not self.stop_event.is_set():
            try:
                # 检查连接状态
                if not self.check_connection():
                    time.sleep(5)
                    continue

                current_time = datetime.now().time()
                current_timestamp = time.time()
                current_date = datetime.now().date()

                # 重置每日状态
                if self.last_trading_date != current_date:
                    self.buy_timing.reset_daily_state()
                    self.last_trading_date = current_date
                    # 每日刷新K线缓存
                    self._refresh_kline_cache_daily(current_date)
                    # 标记为需要强制刷新K线（新的一天）
                    self._force_refresh_kline = True

                # 检查是否在交易时间窗口内
                trading_start_time, trading_end_time = self._get_trading_time_window()
                if not (trading_start_time <= current_time < trading_end_time):
                    # 非交易时段，休眠等待
                    time.sleep(60)
                    continue

                # 检查持仓是否已满
                current_positions_count = len(self.position_manager.strategy_positions)
                max_positions = self.config.get('momentum', {}).get('max_positions', 3)

                if current_positions_count >= max_positions:
                    # 持仓已满，跳过选股
                    if not hasattr(self, '_last_full_position_logged') or not self._last_full_position_logged:
                        logger.info(f"[{thread_name}] [{self.market_type}] 持仓已满 ({current_positions_count}/{max_positions})，跳过选股")
                        self._last_full_position_logged = True
                else:
                    # 持仓未满，执行选股
                    if hasattr(self, '_last_full_position_logged'):
                        self._last_full_position_logged = False
                    self._do_selection(current_time, current_timestamp, current_date)

                # 买入判断
                has_selection = self.cached_selected_stocks is not None and len(self.cached_selected_stocks) > 0

                if has_selection:
                    self._do_buy()

                # 买入循环的休眠逻辑（不影响持仓检查）
                current_positions = len(self.position_manager.strategy_positions)
                max_pos = self.config.get('momentum', {}).get('max_positions', 3)
                no_selections = self.cached_selected_stocks is None or len(self.cached_selected_stocks) == 0

                # 获取休眠配置
                sleep_config = self.config.get('trading', {}).get('live_trading', {})
                sleep_full = sleep_config.get('sleep_full_position', 60)
                sleep_no_pos = sleep_config.get('sleep_no_position_no_selection', 300)
                sleep_normal = sleep_config.get('sleep_normal', 5)

                if current_positions >= max_pos:
                    # 持仓已满，休眠较长时间（只检查是否卖出）
                    logger.debug(f"[{thread_name}] [{self.market_type}] 持仓已满，休眠{sleep_full}秒")
                    time.sleep(sleep_full)
                elif current_positions == 0 and no_selections:
                    # 无持仓无选股，休眠较长时间
                    logger.info(f"[{thread_name}] [{self.market_type}] 无持仓无选股，休眠{sleep_no_pos}秒")
                    time.sleep(sleep_no_pos)
                else:
                    # 正常情况按配置时间休眠
                    logger.debug(f"[{thread_name}] [{self.market_type}] 正常交易，休眠{sleep_normal}秒")
                    time.sleep(sleep_normal)

            except (OSError, IOError) as e:
                logger.error(f"[{thread_name}] {self.market_type}买入循环网络错误: {e}")
                time.sleep(5)
            except Exception as e:
                logger.error(f"[{thread_name}] {self.market_type}买入循环异常: {type(e).__name__}: {e}", exc_info=True)
                time.sleep(5)

        logger.info(f"[{thread_name}] {self.market_type}买入循环已停止")

    def _is_trading_day(self) -> bool:
        """
        判断今天是否是交易日
        
        策略：
        1. 周末（周六、周日）肯定不是交易日
        """
        from datetime import datetime
        
        now = datetime.now()
        
        # 1. 周末检查（周六=5, 周日=6）
        if now.weekday() == 5 or now.weekday() == 6:
            return False

        return True  # 异常时默认认为是交易日，避免阻塞正常交易

    def run_position_check_loop(self):
        """运行持仓检查循环（止盈止损）"""
        thread_name = threading.current_thread().name
        logger.info(f"[{thread_name}] 启动{self.market_type}持仓检查循环...")

        # 初始化上次检查时间
        check_interval = self.position_manager.position_check_interval
        last_check_time = 0
        last_trading_day_check = None
        is_trading_day = False

        while not self.stop_event.is_set():
            try:
                # 检查连接状态
                if not self.check_connection():
                    time.sleep(5)
                    continue

                current_time = datetime.now().time()
                current_timestamp = time.time()
                current_date = datetime.now().date()
                trading_start_time, trading_end_time = self._get_trading_time_window()

                # 每日检查一次是否是交易日（缓存结果）
                if last_trading_day_check != current_date:
                    is_trading_day = self._is_trading_day()
                    last_trading_day_check = current_date
                    if not is_trading_day:
                        logger.info(f"[{thread_name}] [{self.market_type}] 今天不是交易日，跳过持仓检查")

                # 持仓同步：每日一次 + 交易时段每15分钟一次（支持手动买入的股票被及时检测）
                position_sync_needed = False
                if self.last_position_sync_date != current_date:
                    # 每日首次同步
                    position_sync_needed = True
                    sync_reason = "每日首次同步"
                elif is_trading_day and trading_start_time <= current_time < trading_end_time:
                    # 交易时段每15分钟同步一次
                    if current_timestamp - self.last_position_sync_timestamp >= 900:  # 15分钟 = 900秒
                        position_sync_needed = True
                        sync_reason = "15分钟定时同步"

                if position_sync_needed:
                    logger.info(f"[{thread_name}] [{self.market_type}] 触发持仓同步 ({sync_reason})")
                    if self.position_manager.sync_with_broker():
                        self.last_position_sync_date = current_date
                        self.last_position_sync_timestamp = current_timestamp
                        logger.info(f"[{thread_name}] [{self.market_type}] 持仓同步完成")
                    else:
                        logger.warning(f"[{thread_name}] [{self.market_type}] 持仓同步失败")

                # 每日数据同步 - 收盘后16:30-17:30（同步当天K线）和开盘前9:00-9:30（确保最新数据）
                sync_time_close_start = dt_time(16, 30)
                sync_time_close_end = dt_time(17, 30)
                sync_time_open_start = dt_time(8, 30)
                sync_time_open_end = dt_time(9, 00)

                need_sync = False
                sync_type = None
                require_latest = False
                if (self.last_data_sync_date != current_date and
                        sync_time_close_start <= current_time <= sync_time_close_end):
                    need_sync = True
                    sync_type = "收盘同步"
                    require_latest = True  # 收盘后要求数据最新
                elif (self.last_data_sync_date != current_date and
                        sync_time_open_start <= current_time <= sync_time_open_end):
                    need_sync = True
                    sync_type = "开盘同步"
                    require_latest = True  # 开盘前也要求数据最新

                if need_sync:
                    logger.info(f"[{thread_name}] [{self.market_type}] 触发每日数据同步 ({sync_type})...")
                    try:
                        self._daily_data_sync(require_latest=require_latest)
                        self.last_data_sync_date = current_date
                        logger.info(f"[{thread_name}] [{self.market_type}] 每日数据同步完成")
                    except Exception as e:
                        logger.error(f"[{thread_name}] [{self.market_type}] 每日数据同步失败: {e}")

                # 获取交易时间窗口（用于平仓检查）
                trading_start_time, trading_end_time = self._get_trading_time_window()

                # 只在交易时间内执行平仓检查，且必须是交易日
                if is_trading_day and trading_start_time <= current_time < trading_end_time:
                    if current_timestamp - last_check_time >= check_interval:
                        logger.info(f"[{thread_name}] [{self.market_type}] 触发持仓检查 (每{check_interval}秒)")
                        self.position_manager.check_and_exit_positions()
                        last_check_time = current_timestamp
                else:
                    # 非交易时间或非交易日，重置检查时间戳（避免刚进入交易时间时立即触发检查）
                    last_check_time = current_timestamp

                # 持仓检查循环的休眠（固定间隔，不受其他逻辑影响）
                time.sleep(1)  # 每秒检查一次是否到检查时间

            except (OSError, IOError) as e:
                logger.error(f"[{thread_name}] {self.market_type}持仓检查循环网络错误: {e}")
                time.sleep(5)
            except Exception as e:
                logger.error(f"[{thread_name}] {self.market_type}持仓检查循环异常: {type(e).__name__}: {e}", exc_info=True)
                time.sleep(5)

        logger.info(f"[{thread_name}] {self.market_type}持仓检查循环已停止")

    def _daily_data_sync(self, require_latest=False):
        """每日数据同步 - 直接从 OpenD 刷新K线缓存

        Args:
            require_latest: 是否要求K线数据最新
        """
        from datetime import datetime, timedelta

        try:
            stock_codes = self._shared_fetcher.get_blue_chip_stocks()
            if not stock_codes:
                logger.warning(f"[{self.market_type}] 股票池为空，跳过同步")
                return

            logger.info(f"[{self.market_type}] 从 OpenD 同步 {len(stock_codes)} 只股票...")

            # 直接刷新K线缓存（已不依赖DB）
            end_date = datetime.now().date()
            self._refresh_kline_cache_daily(end_date, require_latest=require_latest)
            logger.info(f"[{self.market_type}] 每日数据同步完成")

        except Exception as e:
            logger.error(f"[{self.market_type}] 每日数据同步异常: {e}", exc_info=True)
            raise


    def _do_selection(self, current_time: dt_time, current_timestamp: float, current_date):
        """执行选股 - 复用 kline_cache，不重复拉日K线"""
        should_select = False
        start_time, end_time = self._get_selection_time_window()

        # 检查是否在选股时间窗口内
        if start_time <= current_time < end_time:
            if (self.cached_selected_stocks is None or
                self.last_trading_date != current_date or
                current_timestamp - self.last_selection_time > self.selection_cache_ttl):
                should_select = True

        if not should_select:
            return

        try:
            from mutifactor.strategies.momentum import MomentumStrategy

            # 优先复用 kline_cache（步骤5或每日重置时已拉取）
            stocks_data = self.kline_cache.copy() if self.kline_cache else {}

            # 缓存为空时才重新拉（理论上不应发生）
            if not stocks_data:
                logger.warning(f"[{self.market_type}] kline_cache 为空，重新拉取日K线（这不应发生）...")
                if not self._shared_fetcher:
                    logger.error(f"{self.market_type}共享数据获取器未初始化")
                    return
                stock_codes = self._shared_fetcher.get_blue_chip_stocks()
                if not stock_codes:
                    return
                end_date = datetime.now().date()
                start_date = end_date - timedelta(days=180)
                stocks_data = self._shared_fetcher.fetch_multiple_stocks(
                    stock_codes,
                    start_date=start_date.strftime('%Y-%m-%d'),
                    end_date=end_date.strftime('%Y-%m-%d')
                )
                if stocks_data:
                    self.kline_cache = stocks_data.copy()
                    self.last_kline_update_date = current_date

            if stocks_data and len(stocks_data) > 0:
                from mutifactor.data.base_fetcher import MarketType
                mt = MarketType.HK
                max_pos = self.config.get('momentum', {}).get('max_positions', 3)
                candidate_pool_size = self.config.get('momentum', {}).get('candidate_pool_size', max_pos * 2)
                strategy = MomentumStrategy(config=self.config, market_type=mt)
                selected = strategy.select_stocks(stocks_data, current_date.isoformat(),
                                                            pool_size=candidate_pool_size)

                if selected:
                    selected = self._llm_candidate_review(
                        selected=selected[:candidate_pool_size],
                        stocks_data=stocks_data,
                        current_date=current_date.isoformat(),
                    )
                    self.cached_selected_stocks: List[str] = selected
                    self.last_selection_time = current_timestamp
                    logger.info(f"选股完成: {self.cached_selected_stocks}")
                    self._save_selection_result()
                else:
                    logger.warning("选股完成: 没有符合条件的股票，1小时后重试")
                    self.cached_selected_stocks = []
                    self.last_selection_time = current_timestamp + 3600  # 休眠1小时

        except (OSError, IOError) as e:
            logger.error(f"{self.market_type}选股失败 - 网络错误: {e}")
        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"{self.market_type}选股失败 - 数据错误: {e}")
        except Exception as e:
            logger.error(f"{self.market_type}选股失败: {type(e).__name__}: {e}", exc_info=True)

    def _do_buy(self):
        """执行买入"""
        cached_stocks = self.cached_selected_stocks.copy() if self.cached_selected_stocks else []

        should_buy, reason, entry_mode = self.buy_timing.should_buy_now(
            cached_stocks,
            self.price_fetcher
        )

        if not should_buy:
            # 只在状态变化时输出，避免日志刷屏
            if not hasattr(self, '_last_buy_status') or self._last_buy_status != reason:
                self._last_buy_status = reason
            return

        # 重置状态记录
        self._last_buy_status = None
        # 触发买入也使用缓存避免重复
        if not hasattr(self, '_last_trigger_reason') or self._last_trigger_reason != reason:
            logger.info(f"触发买入: {reason}")
            self._last_trigger_reason = reason

        # 过滤已在持仓中的股票、今日已买入的股票、以及止损冷却期内的股票
        current_holdings = set(self.position_manager.strategy_positions.keys())
        today_bought = self._get_today_bought_stocks()

        # 清理过期的冷却期记录
        self._cleanup_stop_loss_cooldown()

        stocks_to_buy = []
        for code in cached_stocks:
            if code in current_holdings:
                logger.debug(f"  {code} 已在持仓中，跳过")
            elif code in today_bought:
                logger.debug(f"  {code} 今日已买入过，跳过")
            elif self.position_manager.is_in_cooldown(code):
                logger.debug(f"  {code} 止损冷却期内，跳过")
            else:
                stocks_to_buy.append(code)

        # 简化日志输出 - 使用状态缓存避免重复日志
        log_key = f"{reason}_{cached_stocks}_{list(current_holdings)}"
        if not hasattr(self, '_last_buy_log_key') or self._last_buy_log_key != log_key:
            if not stocks_to_buy:
                logger.info(f"选股结果: {cached_stocks} | 持仓: {list(current_holdings)} | 无需买入")
            else:
                logger.info(f"选股结果: {cached_stocks}")
                logger.info(f"当前持仓: {list(current_holdings)}")
                logger.info(f"今日已买: {list(today_bought)}")
                logger.info(f"待买入: {stocks_to_buy}")
            self._last_buy_log_key = log_key

        if not stocks_to_buy:
            logger.info("没有需要买入的股票，等待下次选股")
            return

        # Phase 1: 用K线分析对候选股票排序和过滤
        kline_cfg = self.config.get('trading', {}).get('live_trading', {}).get('buy_timing', {}).get('analysis', {})
        kline_enabled = kline_cfg.get('enabled', False)
        strict_mode = kline_cfg.get('strict_mode', True)  # 默认严格模式

        if kline_enabled and hasattr(self.buy_timing, '_get_intraday_kline_scores'):
            kline_results = self.buy_timing._get_intraday_kline_scores(stocks_to_buy, self.price_fetcher)
            if kline_results:
                logger.info(f"[K线分析] 候选{len(stocks_to_buy)}只，K线强买{sum(1 for r in kline_results if r['signal']=='strong_buy')}只")
                for r in kline_results[:5]:
                    rsi_str = f"{r['rsi']:.0f}" if r['rsi'] is not None else '-'
                    bb_str = f"{r['bb_position']:.0f}" if r['bb_position'] is not None else '-'
                    logger.info(f"  {r['stock_code']}: score={r['score']} signal={r['signal']} RSI={rsi_str} BB={bb_str}% {r['details']}")

                # 只选K线评分≥阈值的股票，最多取3只
                threshold = kline_cfg.get('strong_buy_threshold', 6)
                ranked = [r['stock_code'] for r in kline_results if r['score'] >= threshold]
                if ranked:
                    stocks_to_buy = ranked[:3]
                    logger.info(f"[K线排序] 选取Top3: {stocks_to_buy}")
                else:
                    # 严格模式：全不达标 → 跳过本次买入
                    if strict_mode:
                        logger.info(f"[K线拦截] 所有候选评分 < {threshold}，跳过本次买入")
                        return
                        stocks_to_buy = []
                    else:
                        logger.info("[K线排序] 无达标股票，保留原始候选（宽松模式）")
            else:
                logger.warning("[K线分析] 无K线数据，保留原始候选")

        # ==================== 接入点 B: LLM 买入前否决（影子模式）====================
        stocks_to_buy = self._llm_buy_veto(stocks_to_buy)
        if not stocks_to_buy:
            logger.info("LLM 否决买入，本轮跳过")
            return

        # 计算买入数量和资金
        strategy_remaining = self.position_manager.get_remaining_capital()
        max_ratio = float(self.config.get('risk', {}).get('max_single_position_ratio', 0.5))
        if not stocks_to_buy:
            logger.warning("[买入计算] stocks_to_buy为空，跳过买入")
            return
        per_stock_capital = min(strategy_remaining * max_ratio,
                               strategy_remaining / len(stocks_to_buy))

        logger.info(f"策略剩余资金: {strategy_remaining:.2f}, 每只股票资金: {per_stock_capital:.2f}")

        # 构建买入列表
        buy_list = []
        for stock_code in stocks_to_buy:
            current_price = self.price_fetcher.get_current_price(stock_code)
            logger.info(f"{stock_code} 当前价格: {current_price}")

            if not current_price or current_price <= 0:
                logger.warning(f"  {stock_code} 价格无效，跳过")
                continue

            total_remaining = self.position_manager.get_remaining_capital()
            quantity = self._calculate_buy_quantity(stock_code, per_stock_capital,
                                                   current_price, total_remaining)

            if quantity <= 0:
                continue

            buy_list.append({
                'code': stock_code,
                'quantity': quantity,
                'price': current_price,
                'entry_mode': entry_mode
            })

        logger.info(f"买入列表: {buy_list}")

        # 执行买入
        if buy_list:
            self.position_manager.execute_buy(buy_list)
        else:
            logger.warning("没有可买入的股票，买入流程结束")

    def _save_selection_result(self):
        """保存选股结果"""
        try:
            stock_details: List[Dict[str, Any]] = []
            for code in self.cached_selected_stocks or []:
                price = self.price_fetcher.get_current_price(code)
                name = self._get_stock_name(code)
                in_pos = code in self.position_manager.strategy_positions
                stock_details.append({
                    'code': str(code),
                    'name': str(name) if name else str(code),
                    'price': float(price) if price else None,
                    'in_position': bool(in_pos)
                })
            self._save_selection_results_impl(stock_details)
        except (OSError, IOError) as e:
            logger.error(f"保存选股结果失败 - IO错误: {e}")
        except (TypeError, ValueError) as e:
            logger.error(f"保存选股结果失败 - 数据错误: {e}")
        except Exception as e:
            logger.error(f"保存选股结果失败: {type(e).__name__}: {e}", exc_info=True)

    def start(self) -> bool:
        """启动交易，返回是否成功"""
        if self.state != TradingState.STOPPED:
            logger.warning(f"{self.market_type}交易已经启动")
            return False

        if not self.connect():
            logger.error(f"{self.market_type}启动失败")
            return False

        self.stop_event.clear()

        # ⚠️ 必须在启动线程之前完成数据同步！
        # 原因：_daily_data_sync 耗时约80秒（115只股 × 0.7s间隔），
        #       若在9:00前启动，两个线程会先触发自己的 _refresh_kline_cache_daily，
        #       与 main thread 的 _daily_data_sync 竞争拉同一批股票，导致永久卡死。
        try:
            logger.info(f"[{self.market_type}] 启动前预同步K线数据（{len(self._shared_fetcher.get_blue_chip_stocks())}只）...")
            self._daily_data_sync(require_latest=True)
            self.last_data_sync_date = datetime.now().date()
            self.last_kline_update_date = datetime.now().date()  # 和 _refresh_kline_cache_daily 保持一致，防止线程重复同步
            logger.info(f"[{self.market_type}] 预同步完成，双线程启动")
        except Exception as e:
            logger.warning(f"[{self.market_type}] 预同步失败（不影响交易）: {e}")

        # 启动买入线程（选股 + 买入）
        self.buy_thread = threading.Thread(target=self.run_buy_loop, name=f"{self.market_type}-Buy-Thread")
        self.buy_thread.daemon = True
        self.buy_thread.start()
        logger.info(f"{self.market_type}买入线程已启动")

        # 启动持仓检查线程（止盈止损）
        self.position_check_thread = threading.Thread(
            target=self.run_position_check_loop,
            name=f"{self.market_type}-Position-Check-Thread"
        )
        self.position_check_thread.daemon = True
        self.position_check_thread.start()
        logger.info(f"{self.market_type}持仓检查线程已启动")

        logger.info(f"{self.market_type}交易已启动（买入+持仓检查双线程）")

        return True

    def stop(self):
        """停止交易 - 优雅退出"""
        if self.state == TradingState.STOPPED:
            logger.warning(f"{self.market_type}交易已经处于停止状态")
            return

        logger.info(f"正在停止{self.market_type}交易...")
        self.state = TradingState.STOPPED

        # 通知所有线程停止
        self.stop_event.set()

        # 等待买入线程结束
        if self.buy_thread and self.buy_thread.is_alive():
            logger.info(f"等待{self.market_type}买入线程结束（最多30秒）...")
            self.buy_thread.join(timeout=30)
            if self.buy_thread.is_alive():
                logger.warning(f"{self.market_type}买入线程超时未结束")

        # 等待持仓检查线程结束
        if self.position_check_thread and self.position_check_thread.is_alive():
            logger.info(f"等待{self.market_type}持仓检查线程结束（最多30秒）...")
            self.position_check_thread.join(timeout=30)
            if self.position_check_thread.is_alive():
                logger.warning(f"{self.market_type}持仓检查线程在30秒内未能正常结束，强制终止")
            else:
                logger.info(f"{self.market_type}持仓检查线程已正常退出")

        # 检查并保存最终状态
        try:
            if self.position_manager and self.position_manager.strategy_positions:
                position_count = len(self.position_manager.strategy_positions)
                logger.warning(f"⚠️  退出时仍有 {position_count} 只持仓未平")
                self.position_manager.save_positions()
        except Exception as e:
            logger.error(f"保存最终持仓状态失败: {e}", exc_info=True)

        # 关闭交易连接
        try:
            if self.trader:
                logger.info(f"断开{self.market_type}交易连接...")
                self.trader.disconnect()
                logger.info(f"{self.market_type}交易连接已断开")
        except (OSError, IOError) as e:
            logger.warning(f"断开{self.market_type}交易连接时IO错误: {e}")
        except Exception as e:
            logger.warning(f"断开{self.market_type}交易连接时出错: {type(e).__name__}: {e}")

        # 关闭共享数据获取器
        try:
            if self._shared_fetcher:
                logger.info(f"断开{self.market_type}共享数据获取器...")
                self._shared_fetcher.disconnect()
                self._shared_fetcher = None
                logger.info(f"{self.market_type}共享数据获取器已断开")
        except (OSError, IOError) as e:
            logger.warning(f"断开{self.market_type}共享数据获取器时IO错误: {e}")
        except Exception as e:
            logger.warning(f"断开{self.market_type}共享数据获取器时出错: {type(e).__name__}: {e}")

        # 关闭行情连接
        try:
            if self.price_fetcher:
                logger.info(f"断开{self.market_type}行情连接...")
                self.price_fetcher.disconnect()
                logger.info(f"{self.market_type}行情连接已断开")
        except (OSError, IOError) as e:
            logger.warning(f"断开{self.market_type}行情连接时IO错误: {e}")
        except Exception as e:
            logger.warning(f"断开{self.market_type}行情连接时出错: {type(e).__name__}: {e}")

        logger.info(f"{self.market_type}交易已停止")

    def _cleanup_stop_loss_cooldown(self):
        """清理过期的止损冷却期记录"""
        try:
            self.position_manager.cleanup_cooldown()
        except Exception as e:
            logger.debug(f"[{self.market_type}] 清理止损冷却期记录时出错: {e}")

    # ==================== LLM 接入点实现 ====================

    def _llm_candidate_review(
        self,
        selected: List[str],
        stocks_data: Dict,
        current_date: str,
    ) -> List[str]:
        """
        接入点 A: 选股后 LLM 候选复核（影子模式下只记录不执行）

        即使 LLM 关了/挂了，也必须把 selected 原样返回。
        """
        if self.llm_advisor is None:
            return selected
        if not self.llm_use_in.get('candidate_review', False):
            return selected
        if not self._llm_can_call('candidate_review'):
            return selected

        snapshot = ''
        result = None
        latency_ms = 0
        # 这些在 finally 之前一定会被赋值（try 里有 return，finally 之后再 record）
        final_list = selected
        final_action = '未触发（降级）'
        adopted = False
        t0 = time.time()

        try:
            snapshot = self.context_builder.build_candidate_snapshot(
                selected, stocks_data, current_date
            )
            holdings = list(self.position_manager.strategy_positions.keys())
            result = self.llm_advisor.assess_candidates(
                candidates=selected,
                context_snapshot=snapshot,
                date=current_date,
                holdings=holdings,
            )
            latency_ms = int((time.time() - t0) * 1000)

            if result is None:
                logger.debug("[LLM] candidate_review 返回 None，按原规则结果执行")
                final_action = 'LLM 返回 None，降级回纯规则'
                final_list = selected
                adopted = False

            # 影子模式：只记录不修改
            elif self.llm_shadow_mode:
                verdict = result.get('verdict', 'pass')
                vetoed = result.get('vetoed', [])
                confidence = result.get('confidence', 0)
                logger.info(
                    f"[LLM-影子] 选股复核: verdict={verdict} "
                    f"vetoed={vetoed} confidence={confidence:.2f}"
                )
                final_action = f'影子模式 verdict={verdict} vetoed={vetoed}'
                final_list = selected
                adopted = False

            # Phase 2+: 真正执行否决
            else:
                final_list, _vetoed, final_action = self._llm_apply_candidate_veto(
                    selected, result
                )
                adopted = (len(final_list) != len(selected))  # 只有真的删了才 adopted

        except Exception as e:
            logger.warning(f"[LLM] candidate_review 异常，降级回纯规则: {e}")
            final_action = f'异常降级: {e}'
            final_list = selected
            adopted = False

        # 一次性写日志（走到这里所有变量都已是最新真实值）
        self.llm_logger.record(
            trigger='candidate_review',
            snapshot=snapshot or '(none)',
            prompt=f'候选股 {len(selected)} 只，日期 {current_date}',
            llm_output=result,
            latency_ms=latency_ms,
            adopted=adopted,
            final_action=final_action,
        )

        return final_list

    def _llm_apply_candidate_veto(self, selected: List[str], result: Dict):
        """
        Phase 2: 真正执行 LLM 候选否决 —— 带完整安全边界。

        Returns:
            (final_list, vetoed_list, final_action_str)
        """
        safety = self.llm_safety
        min_confidence = safety.get('min_confidence_to_veto', 0.5)
        max_veto_ratio = safety.get('max_veto_ratio', 0.5)

        confidence = result.get('confidence', 0)
        vetoed_raw = result.get('vetoed', [])

        # 信心度不够 → 不否决
        if confidence < min_confidence:
            logger.info(f"[LLM] 信心度 {confidence:.2f} < {min_confidence}，不否决")
            return selected, [], f'信心度不足 ({confidence:.2f}<{min_confidence})，放行'

        # 安全 3 修复: 单候选时 max_veto_count=0，禁止 100% 否决
        min_keep = max(1, len(selected) - int(len(selected) * max_veto_ratio))
        max_veto_count = len(selected) - min_keep
        if max_veto_count <= 0:
            logger.info(f"[LLM] 候选只有 {len(selected)} 只，安全边界禁止否决")
            return selected, [], f'候选={len(selected)} 只，安全边界禁止否决'

        vetoed = [c for c in vetoed_raw if c in selected][:max_veto_count]
        if not vetoed:
            return selected, [], 'LLM 未建议否决'

        final = [c for c in selected if c not in vetoed]
        logger.warning(
            f"[LLM] 真实否决: 原候选={selected} → 否决={vetoed} → 最终={final}"
        )
        return final, vetoed, f'真实否决 {vetoed}，最终 {final}'

    def _llm_buy_veto(self, stocks_to_buy: List[str]) -> List[str]:
        """
        接入点 B: 买入前 LLM 市场风险否决（影子模式下只记录不执行）

        修复点:
          - 安全 3: block 也过 confidence 门槛
          - 安全 4: 持仓盈亏实时算
          - 日志状态永远反映真实结果（不再提前写死 final_action）
        """
        if self.llm_advisor is None:
            return stocks_to_buy
        if not self.llm_use_in.get('buy_veto', False):
            return stocks_to_buy
        if not self._llm_can_call('buy_veto'):
            return stocks_to_buy

        result = None
        latency_ms = 0
        final_list = stocks_to_buy
        final_action = '未触发（降级）'
        adopted = False
        holdings_info_str = ''
        t0 = time.time()

        try:
            # 安全 4: 实时计算持仓盈亏，不再读不存在的 pnl_pct 字段
            holdings_raw = getattr(self.position_manager, 'strategy_positions', {}) or {}
            holdings_info = []
            for code, pos in list(holdings_raw.items())[:6]:
                try:
                    current_price = self.price_fetcher.get_current_price(code)
                    cost_price = pos.get('cost_price', pos.get('avg_price', 0))
                    if current_price and cost_price > 0:
                        pnl_pct = (current_price - cost_price) / cost_price
                    else:
                        pnl_pct = 0
                    holdings_info.append(f"{code}:{pnl_pct*100:+.1f}%")
                except Exception:
                    holdings_info.append(f"{code}")
            holdings_info_str = ', '.join(holdings_info)

            cash = getattr(self.position_manager, 'get_remaining_capital', lambda: 0)()

            result = self.llm_advisor.veto_buy(
                buy_list=stocks_to_buy,
                holdings=holdings_info,
                cash=cash,
            )
            latency_ms = int((time.time() - t0) * 1000)

            if result is None:
                final_action = 'LLM 返回 None，降级回纯规则'
                final_list = stocks_to_buy
                adopted = False

            # 影子模式
            elif self.llm_shadow_mode:
                verdict = result.get('verdict', 'allow')
                confidence = result.get('confidence', 0)
                logger.info(
                    f"[LLM-影子] 买入否决: verdict={verdict} "
                    f"risk={result.get('risk_level','LOW')} confidence={confidence:.2f}"
                )
                final_action = f'影子模式 verdict={verdict} confidence={confidence:.2f}'
                final_list = stocks_to_buy
                adopted = False

            # Phase 2+: 真实否决
            else:
                verdict = result.get('verdict', 'allow')
                confidence = result.get('confidence', 0)
                reason = result.get('reason', '')
                min_confidence = self.llm_safety.get('min_confidence_to_veto', 0.5)

                if verdict == 'block' and confidence >= min_confidence:
                    logger.warning(
                        f"[LLM] 真实否决买入 (conf={confidence:.2f}): "
                        f"{stocks_to_buy}, reason={reason}"
                    )
                    final_action = f'block {stocks_to_buy}: {reason}'
                    final_list = []
                    adopted = True
                elif verdict == 'block' and confidence < min_confidence:
                    final_action = f'block 但 confidence 不足 ({confidence:.2f}<{min_confidence})，放行'
                    final_list = stocks_to_buy
                    adopted = False
                elif verdict == 'delay':
                    # delay=轻仓；减仓逻辑尚未实现，先按原计划执行，记录原因供复盘
                    final_action = f'delay（轻仓未实现，按原计划执行）: {reason}'
                    final_list = stocks_to_buy
                    adopted = False
                else:
                    final_action = f'allow: {reason}'
                    final_list = stocks_to_buy
                    adopted = False

        except Exception as e:
            logger.warning(f"[LLM] buy_veto 异常，降级回纯规则: {e}")
            final_action = f'异常降级: {e}'
            final_list = stocks_to_buy
            adopted = False

        # 一次性写日志
        self.llm_logger.record(
            trigger='buy_veto',
            snapshot=f"待买入={stocks_to_buy}, 持仓={holdings_info_str}",
            prompt=f'buy_list={stocks_to_buy}',
            llm_output=result,
            latency_ms=latency_ms,
            adopted=adopted,
            final_action=final_action,
        )

        return final_list

    def _refresh_kline_cache_daily(self, current_date, require_latest=False):
        """每日刷新K线缓存 - 直接从 OpenD 拉，不走数据库

        OpenD 本地有 SQLite 缓存，重启后重新拉。

        Args:
            current_date: 当前日期
            require_latest: 是否要求数据最新（收盘后数据同步时设为True）
        """
        logger.info(f"[{self.market_type}] 每日刷新K线缓存 (require_latest={require_latest})")

        # 防止日内重复拉取（启动时 _daily_data_sync 已同步过）
        if self.last_kline_update_date == current_date and self.kline_cache:
            logger.info(f"[{self.market_type}] K线缓存已是今日（{self.last_kline_update_date}），跳过刷新")
            return

        try:
            if not self._shared_fetcher:
                logger.warning(f"[{self.market_type}] K线缓存刷新失败 - 共享数据获取器未初始化")
                return

            stock_codes = self._shared_fetcher.get_blue_chip_stocks()
            if not stock_codes:
                return

            end_date = current_date
            start_date = end_date - timedelta(days=180)

            logger.info(f"[{self.market_type}] 从 OpenD 拉取 {len(stock_codes)} 只股票的日K ({start_date} ~ {end_date})")
            stocks_data = self._shared_fetcher.fetch_multiple_stocks(
                stock_codes,
                start_date=start_date.strftime('%Y-%m-%d'),
                end_date=end_date.strftime('%Y-%m-%d')
            )
            logger.info(f"[{self.market_type}] K线获取完成，返回 {len(stocks_data)} 只")

            self.kline_cache = stocks_data
            self.last_kline_update_date = current_date
            logger.info(f"[{self.market_type}] K线缓存刷新完成 {len(stocks_data)} 只股票")

        except Exception as e:
            logger.error(f"[{self.market_type}] K线缓存刷新失败: {e}", exc_info=True)

    def get_cached_kline_data(self, stock_code: str):
        """获取缓存的K线数据"""
        return self.kline_cache.get(stock_code)

    def pause(self):
        """暂停交易"""
        if self.state == TradingState.RUNNING:
            self.state = TradingState.PAUSED
            logger.info(f"{self.market_type}交易已暂停")

    def resume(self):
        """恢复交易"""
        if self.state == TradingState.PAUSED:
            self.state = TradingState.RUNNING
            logger.info(f"{self.market_type}交易已恢复")
