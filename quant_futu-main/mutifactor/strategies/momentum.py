"""
动量策略 - 基于加权线性回归和RSRS趋势过滤
简洁版本
"""
import logging
from datetime import datetime

import numpy as np
import pandas as pd
import math
import time
from typing import Dict, List, Tuple

from mutifactor.base import BaseStrategy
from mutifactor.data.base_fetcher import MarketType
from mutifactor.strategies.unified_exit_strategy import check_exit_unified
from mutifactor.position import BacktestPositionManager
from mutifactor.config.constants import TradingConstants

# 动量策略默认参数
DEFAULT_MOMENTUM_WINDOW = 25
DEFAULT_RSRS_WINDOW = 18
DEFAULT_RSRS_LONG_WINDOW = 60
DEFAULT_RSRS_ROLLING_WINDOW = 20
DEFAULT_STRONG_TREND_THRESHOLD = 0.05
DEFAULT_WEAK_TREND_THRESHOLD = 0.01
DEFAULT_MIN_MOMENTUM_SCORE = 0.02
DEFAULT_MIN_R2 = 0.5
DEFAULT_MAX_POSITIONS = 3

# 年化计算常量
ANNUAL_TRADING_DAYS = 252  # 年化交易日数

# 线性加权动量参数
LINEAR_WEIGHT_START = 1.0  # 线性权重起始值
LINEAR_WEIGHT_END = 2.0    # 线性权重结束值

# 选股过滤参数
DEFAULT_VOLUME_WINDOW = 20
DEFAULT_MIN_AVG_VOLUME = 1000000
DEFAULT_MA20_THRESHOLD = 0.98
DEFAULT_VOLUME_THRESHOLD = 0.8
DEFAULT_MIN_DATA_RATIO = 0.8  # 最小有效数据比例

logger = logging.getLogger(__name__)


def _calculate_rolling_slopes_vectorized(prices: np.ndarray, window: int) -> np.ndarray:
    """
    向量化计算滚动线性回归斜率

    使用最小二乘法公式: slope = (n*Σxy - Σx*Σy) / (n*Σx2 - (Σx)2)

    Args:
        prices: 价格数组
        window: 滚动窗口大小

    Returns:
        斜率数组
    """
    n = len(prices)
    if n < window:
        return np.array([])

    # 创建 x 数组 (0, 1, 2, ..., window-1)
    x = np.arange(window, dtype=np.float64)
    sum_x = np.sum(x)
    sum_x2 = np.sum(x * x)
    denominator = window * sum_x2 - sum_x * sum_x

    if denominator == 0:
        return np.array([])

    # 使用累积和计算滚动求和
    # Σy
    cumsum_y = np.cumsum(prices)
    sum_y = cumsum_y[window-1:] - np.concatenate(([0], cumsum_y[:-window]))

    # 计算 Σxy: sum of x * y
    indices = np.arange(len(prices), dtype=np.float64)
    weighted_prices = prices * indices
    cumsum_xy = np.cumsum(weighted_prices)
    sum_xy = cumsum_xy[window-1:] - np.concatenate(([0], cumsum_xy[:-window]))

    # 调整 sum_xy: 因为 x 应该是 0 到 window-1,而不是实际索引
    # sum_xy 需要减去 sum_y * (start_index) 来归一化
    start_indices = np.arange(len(sum_y))
    sum_xy_normalized = sum_xy - sum_y * start_indices

    # 计算斜率
    slopes = (window * sum_xy_normalized - sum_x * sum_y) / denominator

    return slopes


class MomentumStrategy(BaseStrategy):
    """动量策略 - 简洁版"""

    def __init__(self, initial_capital: float = None, config: Dict = None,
                 market_type: MarketType = MarketType.HK):
        super().__init__(initial_capital, market_type=market_type, config=config)
        self.config = config or {}

        # 动量参数 - 从配置文件读取,config.yaml 优先
        self.momentum_config = self.config.get('momentum', {})
        momentum_config = self.momentum_config
        self.momentum_window = momentum_config.get('momentum_window', DEFAULT_MOMENTUM_WINDOW)
        self.rsrs_window = momentum_config.get('rsrs_window', DEFAULT_RSRS_WINDOW)
        self.rsrs_long_window = momentum_config.get('rsrs_long_window', DEFAULT_RSRS_LONG_WINDOW)
        self.rsrs_rolling_window = momentum_config.get('rsrs_rolling_window', DEFAULT_RSRS_ROLLING_WINDOW)

        # 趋势强度阈值 - 从配置文件读取
        self.strong_trend_threshold = momentum_config.get('strong_trend_threshold', DEFAULT_STRONG_TREND_THRESHOLD)
        self.weak_trend_threshold = momentum_config.get('weak_trend_threshold', DEFAULT_WEAK_TREND_THRESHOLD)

        # 选股过滤阈值 - 从配置文件读取
        self.min_momentum_score = momentum_config.get('min_momentum_score', DEFAULT_MIN_MOMENTUM_SCORE)
        self.min_r2 = momentum_config.get('min_r2', DEFAULT_MIN_R2)

        # 动量计算方法 - 从配置文件读取
        self.momentum_method = momentum_config.get('momentum_method', 'exponential')

        # 仓位参数 - 从配置文件读取
        self.max_positions = momentum_config.get('max_positions', DEFAULT_MAX_POSITIONS)

        # 波动率调整参数
        self.volatility_adjustment_enabled = momentum_config.get('volatility_adjustment_enabled', True)
        self.volatility_window = momentum_config.get('volatility_window', 20)
        self.high_volatility_threshold = momentum_config.get('high_volatility_threshold', 0.40)
        self.low_volatility_threshold = momentum_config.get('low_volatility_threshold', 0.15)
        self.high_volatility_penalty = momentum_config.get('high_volatility_penalty', 0.7)  # 高波动惩罚系数

        # 成交量确认参数
        self.volume_confirmation_enabled = momentum_config.get('volume_confirmation_enabled', True)
        self.volume_short_window = momentum_config.get('volume_short_window', 5)
        self.volume_long_window = momentum_config.get('volume_long_window', 20)
        self.min_volume_ratio = momentum_config.get('min_volume_ratio', 1.5)  # 成交量放大阈值

        # RSRS缓存
        self.rsrs_cache = {}
        self.rsrs_cache_time = {}  # Track last access time

        # 股票信息缓存(上市日期、每手股数、名称)
        self.stock_info_cache = {}
        self._load_stock_info_cache()

        # 持仓管理器 - 使用统一的 BacktestPositionManager
        market_type_str = market_type.value if hasattr(market_type, 'value') else str(market_type)
        self.position_manager = BacktestPositionManager(
            config=self.config,
            market_type=market_type_str,
            initial_capital=self.initial_capital
        )

        logger.info(f"动量策略初始化完成")
        logger.info(f"参数配置: momentum_window={self.momentum_window}, rsrs_window={self.rsrs_window}, "
                   f"min_momentum_score={self.min_momentum_score}, min_r2={self.min_r2}, max_positions={self.max_positions}")

    def _load_stock_info_cache(self):
        """全量加载缓存,带TTL"""
        self._cache_info_time = time.time()
        self._cache_info_ttl = 600  # 10分钟过期
        try:
            from mutifactor.infra.yaml_storage import yaml_storage
            self.stock_info_cache = yaml_storage.get_all_stock_info('HK')
            logger.info(f"已加载 {len(self.stock_info_cache)} 只股票信息到缓存")
        except Exception as e:
            logger.warning(f"加载股票信息缓存失败: {e}")

    def _get_stock_info(self, stock_code: str) -> dict:
        """查询股票信息，缓存过期或miss时回源数据库"""
        # A股股票不需要读取港股股票信息，直接返回空
        if stock_code.startswith('SH.') or stock_code.startswith('SZ.'):
            return {}

        now = time.time()
        if now - self._cache_info_time > self._cache_info_ttl:
            self._load_stock_info_cache()

        if stock_code not in self.stock_info_cache:
            try:
                from mutifactor.infra.yaml_storage import yaml_storage
                listing_date = yaml_storage.get_listing_date(stock_code)
                if listing_date:
                    self.stock_info_cache[stock_code] = {'listing_date': listing_date}
                    logger.info(f"从数据库补充加载: {stock_code}")
            except Exception:
                pass

        return self.stock_info_cache.get(stock_code, {})

    def get_stock_listing_date(self, stock_code: str):
        """从缓存获取上市日期(缓存miss时回源数据库),返回 datetime.date 或 None"""
        listing_date_str = self._get_stock_info(stock_code).get('listing_date')
        if listing_date_str:
            # YAML 加载后是字符串,转成 date 对象
            if isinstance(listing_date_str, str):
                return datetime.strptime(listing_date_str, '%Y-%m-%d').date()
            else:
                return listing_date_str  # 已经是 date 对象
        return None

    def calculate_momentum_score_linear(self, df: pd.DataFrame) -> Tuple[float, float]:
        """
        线性加权动量得分计算

        特点:使用线性增长权重(1→2),对近期数据更激进
        适用场景:趋势强劲、追求高收益的市场环境
        """
        if len(df) < self.momentum_window:
            return 0.0, 0.0

        recent_df = df.tail(self.momentum_window)
        close_prices = recent_df['close'].values

        # Enhanced validation
        if not np.all(np.isfinite(close_prices)) or np.any(close_prices <= 0):
            return 0.0, 0.0

        valid_mask = (close_prices > 0) & np.isfinite(close_prices)
        if np.sum(valid_mask) < self.momentum_window * DEFAULT_MIN_DATA_RATIO:
            return 0.0, 0.0

        # 修复:根据有效数据长度重新生成权重,避免索引错位
        valid_prices = close_prices[valid_mask]
        n_valid = len(valid_prices)
        weights = np.linspace(LINEAR_WEIGHT_START, LINEAR_WEIGHT_END, n_valid)

        log_prices = np.log(valid_prices)
        x = np.arange(len(log_prices))

        try:
            slope, intercept = np.polyfit(x, log_prices, 1, w=weights)
            annual_return = math.exp(slope * ANNUAL_TRADING_DAYS) - 1

            y_pred = slope * x + intercept
            ss_res = np.sum((log_prices - y_pred) ** 2)
            ss_tot = np.sum((log_prices - np.mean(log_prices)) ** 2)
            r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

            return annual_return * r2, r2
        except (ValueError, TypeError, np.linalg.LinAlgError) as e:
            logger.debug(f"动量得分计算失败: {e}")
            return 0.0, 0.0

    def calculate_momentum_score_exponential(self, df: pd.DataFrame) -> Tuple[float, float]:
        """
        指数加权动量得分计算

        主要区别:
        1. 权重设计:指数衰减权重 exp(-1→0),比线性权重更平滑
        2. R2计算:使用加权残差平方和,考虑权重分布
        3. 拟合目标:更关注近期数据,但权重分布更均衡

        特点:权重平滑衰减,受单个异常值影响小,结果更稳定
        适用场景:波动较大、追求稳定性的市场环境
        """
        if len(df) < self.momentum_window:
            return 0.0, 0.0

        recent_df = df.tail(self.momentum_window)
        close_prices = recent_df['close'].values

        if not np.any(close_prices > 0):
            return 0.0, 0.0

        valid_mask = close_prices > 0
        if np.sum(valid_mask) < self.momentum_window * DEFAULT_MIN_DATA_RATIO:
            return 0.0, 0.0

        # JQ风格权重 - 指数衰减权重
        weights = np.exp(np.linspace(-1, 0, self.momentum_window))[valid_mask]
        log_prices = np.log(close_prices[valid_mask])
        x = np.arange(len(log_prices))

        try:
            # JQ风格的加权回归
            slope, intercept = np.polyfit(x, log_prices, 1, w=weights)
            annual_return = math.exp(slope * ANNUAL_TRADING_DAYS) - 1

            # JQ风格的R2计算
            y_pred = slope * x + intercept
            ss_res = np.sum(weights * (log_prices - y_pred) ** 2)
            ss_tot = np.sum(weights * (log_prices - np.mean(log_prices)) ** 2)
            r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

            return annual_return * r2, r2
        except (ValueError, TypeError, np.linalg.LinAlgError) as e:
            logger.debug(f"JQ动量得分计算失败: {e}")
            return 0.0, 0.0

    def _calculate_volatility_adjustment(self, df: pd.DataFrame) -> float:
        """
        计算波动率调整系数

        高波动股票降低动量权重,避免假突破

        Returns:
            调整系数 (0.0 ~ 1.0)
        """
        if not self.volatility_adjustment_enabled or len(df) < self.volatility_window:
            return 1.0

        try:
            # 计算年化波动率
            returns = df['close'].pct_change().dropna()
            if len(returns) < self.volatility_window:
                return 1.0

            recent_returns = returns.tail(self.volatility_window)
            volatility = recent_returns.std() * np.sqrt(ANNUAL_TRADING_DAYS)

            if volatility <= self.low_volatility_threshold:
                # 低波动,不调整
                return 1.0
            elif volatility >= self.high_volatility_threshold:
                # 高波动,应用惩罚
                return self.high_volatility_penalty
            else:
                # 线性插值
                ratio = (volatility - self.low_volatility_threshold) / (
                    self.high_volatility_threshold - self.low_volatility_threshold
                )
                return 1.0 - ratio * (1.0 - self.high_volatility_penalty)
        except Exception as e:
            logger.debug(f"波动率调整计算失败: {e}")
            return 1.0

    def _check_volume_confirmation(self, df: pd.DataFrame) -> float:
        """
        检查成交量确认

        动量突破时要求成交量放大,过滤虚假信号

        Returns:
            成交量确认系数 (0.0 ~ 1.0)
        """
        if not self.volume_confirmation_enabled:
            return 1.0

        if 'volume' not in df.columns or len(df) < self.volume_long_window:
            return 1.0

        try:
            recent_volume = df['volume'].tail(self.volume_short_window).mean()
            baseline_volume = df['volume'].tail(self.volume_long_window).mean()

            if baseline_volume <= 0:
                return 1.0

            volume_ratio = recent_volume / baseline_volume

            if volume_ratio >= self.min_volume_ratio:
                # 成交量放大,确认信号
                return 1.0
            elif volume_ratio >= 1.0:
                # 成交量略有增加,部分确认
                return 0.8
            else:
                # 成交量萎缩,信号打折
                return 0.6
        except Exception as e:
            logger.debug(f"成交量确认计算失败: {e}")
            return 1.0

    def calculate_momentum_acceleration(self, df: pd.DataFrame) -> Tuple[float, float]:
        """
        动量加速判断:对最近 momentum_accel_short_window 天收盘价做对数回归,斜率 > 0 表示动量在加速

        Returns:
            (slope, 0.0)
            - slope > 0 → 动量加速,入场信号
            - slope <= 0 → 动量衰减或横盘,应回避
        """
        momentum_config = self.config.get('momentum', {})
        short_window = momentum_config.get('momentum_accel_short_window', 5)
        min_data_ratio = momentum_config.get('min_data_ratio', 0.7)

        if len(df) < short_window:
            return 0.0, 0.0

        short_df = df.tail(short_window)
        prices = short_df['close'].values
        valid = prices[prices > 0]

        if len(valid) < short_window * min_data_ratio:
            return 0.0, 0.0

        log_prices = np.log(valid)
        x = np.arange(len(log_prices))

        try:
            slope, _ = np.polyfit(x, log_prices, 1)
        except (ValueError, TypeError, np.linalg.LinAlgError):
            return 0.0, 0.0

        return slope, 0.0

    def calculate_momentum_score_with_enhancements(self, df: pd.DataFrame) -> Tuple[float, float]:
        """
        增强版动量得分计算(带波动率调整和成交量确认)

        Returns:
            (adjusted_momentum_score, r2)
        """
        # 基础动量得分
        if self.momentum_method == 'linear':
            base_score, r2 = self.calculate_momentum_score_linear(df)
        else:
            base_score, r2 = self.calculate_momentum_score_exponential(df)

        if base_score <= 0 or r2 < self.min_r2:
            return base_score, r2

        # 应用波动率调整
        volatility_factor = self._calculate_volatility_adjustment(df)

        # 应用成交量确认
        volume_factor = self._check_volume_confirmation(df)

        # 综合调整
        adjusted_score = base_score * volatility_factor * volume_factor

        # 记录调整信息(调试用)
        if adjusted_score != base_score:
            logger.debug(f"动量得分调整: {base_score:.4f} -> {adjusted_score:.4f} "
                        f"(波动率系数: {volatility_factor:.2f}, 成交量系数: {volume_factor:.2f})")

        return adjusted_score, r2

    def calculate_rsrs_slope(self, df: pd.DataFrame, window: int) -> float:
        """计算RSRS斜率"""
        if len(df) < window:
            return 0.0

        cache_key = (hash(df['close'].tail(self.rsrs_window).values.tobytes()), window)
        current_time = time.time()
        if cache_key in self.rsrs_cache:
            self.rsrs_cache_time[cache_key] = current_time  # Update access time
            return self.rsrs_cache[cache_key]

        recent_df = df.tail(window)
        try:
            x = np.arange(window)
            slope_high, _ = np.polyfit(x, recent_df['high'].values, 1)
            slope_low, _ = np.polyfit(x, recent_df['low'].values, 1)
            result = (slope_high + slope_low) / 2

            current_time = time.time()
            self.rsrs_cache[cache_key] = result
            self.rsrs_cache_time[cache_key] = current_time

            # Cleanup if cache is full or entries are too old
            if len(self.rsrs_cache) > 1000 or current_time - min(self.rsrs_cache_time.values()) > 3600:
                self.rsrs_cache.clear()
                self.rsrs_cache_time.clear()

            return result
        except (ValueError, TypeError, np.linalg.LinAlgError) as e:
            logger.debug(f"RSRS斜率计算失败: {e}")
            return 0.0

    def check_trend_filter(self, df: pd.DataFrame, current_price: float,
                           is_new_stock: bool = False) -> Tuple[bool, str]:
        """RSRS趋势过滤(向量化实现)

        Args:
            df: K线数据
            current_price: 当前价格
            is_new_stock: 是否是新股(上市不足150天)。新股用自己的全部数据
                         做RSRS验证,不要求rsrs_long_window(250天)
        """
        # 新股用自己的数据量作为long_window,不要求250天
        effective_long_window = len(df) if is_new_stock else self.rsrs_long_window

        if len(df) < effective_long_window:
            return True, "数据不足"

        recent_slope = self.calculate_rsrs_slope(df, self.rsrs_window)

        # 向量化计算历史斜率
        long_df = df.tail(effective_long_window)
        high_values = long_df['high'].values
        low_values = long_df['low'].values

        # 使用向量化函数计算 high 和 low 的滚动斜率
        slopes_high = _calculate_rolling_slopes_vectorized(high_values, self.rsrs_window)
        slopes_low = _calculate_rolling_slopes_vectorized(low_values, self.rsrs_window)

        # 合并斜率(取平均)
        min_len = min(len(slopes_high), len(slopes_low))
        if min_len == 0:
            return True, "历史数据不足"

        slopes = (slopes_high[:min_len] + slopes_low[:min_len]) / 2

        # 计算滚动平均
        if len(slopes) < self.rsrs_rolling_window:
            return True, "滚动数据不足"

        rolling_slopes = np.convolve(slopes, np.ones(self.rsrs_rolling_window)/self.rsrs_rolling_window, mode='valid')

        if len(rolling_slopes) == 0:
            return True, "滚动数据不足"

        mean_slope = np.mean(rolling_slopes)
        std_slope = np.std(rolling_slopes)
        beta = mean_slope - 2 * std_slope

        if recent_slope > beta:
            strength = (recent_slope - beta) / (std_slope + 1e-10)
            if strength > self.strong_trend_threshold:
                return True, "强趋势"
            if strength > self.weak_trend_threshold:
                ma5 = df['close'].tail(5).mean()
                if current_price >= ma5:
                    return True, "弱趋势MA5支撑"
            ma10 = df['close'].tail(10).mean()
            if current_price >= ma10:
                return True, "MA10支撑"
            return False, "未通过趋势强度测试"
        return False, "未通过趋势基准测试"

    def check_stock_quality(self, stock_code: str, df: pd.DataFrame) -> Tuple[bool, str]:
        """基本股票质量检查"""
        if len(df) < 30:
            return False, "数据不足"

        current_price = df['close'].iloc[-1]

        # 排除流动性差的股票（日均成交额）
        if 'volume' in df.columns:
            avg_volume = df['volume'].tail(DEFAULT_VOLUME_WINDOW).mean() * current_price
            if avg_volume < DEFAULT_MIN_AVG_VOLUME:
                return False, f"流动性差(日均成交额{avg_volume/1e6:.1f}M)"

        # 排除换手率过低的股票（日均换手率低于阈值说明无人问津）
        if 'turnover_rate' in df.columns:
            avg_turnover_rate = df['turnover_rate'].tail(DEFAULT_VOLUME_WINDOW).mean()
            min_turnover_rate = self.momentum_config.get('min_turnover_rate', 0.15)  # 从config读取
            if avg_turnover_rate < min_turnover_rate:
                return False, f"换手率过低(日均{avg_turnover_rate:.3f}%)"

        return True, "质量检查通过"

    def check_entry_confirmation(self, df: pd.DataFrame) -> bool:
        """入场确认"""
        if len(df) < DEFAULT_VOLUME_WINDOW:
            return False

        current_price = df['close'].iloc[-1]

        # 价格在MA20上方
        if len(df) >= DEFAULT_VOLUME_WINDOW:
            ma20 = df['close'].tail(DEFAULT_VOLUME_WINDOW).mean()
            if current_price < ma20 * DEFAULT_MA20_THRESHOLD:
                return False

        # 成交量确认
        recent_vol = df['volume'].iloc[-1]
        avg_vol = df['volume'].tail(DEFAULT_VOLUME_WINDOW).mean()
        if recent_vol < avg_vol * DEFAULT_VOLUME_THRESHOLD:
            return False

        # 波动率适中
        returns = df['close'].pct_change().tail(DEFAULT_VOLUME_WINDOW).dropna()
        if len(returns) > 0:
            volatility = returns.std() * np.sqrt(ANNUAL_TRADING_DAYS)
            if (volatility < TradingConstants.MIN_VOLATILITY or
                volatility > TradingConstants.MAX_VOLATILITY):
                return False

        return True

    def select_stocks(self, stocks_data: Dict, current_date: str, pool_size: int = None) -> List:
        """选股流程

        Args:
            stocks_data: 股票K线数据
            current_date: 当前日期
            pool_size: 候选池大小,默认为 max_positions - 当前持仓数。
                        传入更大的值可扩大候选池,让买入时机选择有更多选择。
        """
        """选股流程"""
        from datetime import datetime

        candidates = []
        filtered_stats = {
            'total': len(stocks_data),
            'data_insufficient': 0,
            'already_in_position': 0,
            'lockup_skip': 0,
            'quality_fail': 0,
            'momentum_fail': 0,
            'trend_fail': 0,
            'entry_fail': 0,
            'new_stock_selected': 0,
            'passed': 0
        }

        for stock_code, df in stocks_data.items():
            listing_date = self.get_stock_listing_date(stock_code)
            new_stock_params = self.get_new_stock_params(listing_date, current_date)

            if df is None or len(df) < DEFAULT_VOLUME_WINDOW:
                filtered_stats['data_insufficient'] += 1
                continue

            if stock_code in self.positions:
                filtered_stats['already_in_position'] += 1
                continue

            # 解禁期前30天检查(150-180天):什么情况都不买
            if listing_date:
                # 修复:listing_date 从 YAML 读出是字符串,需要转成 date 对象
                if isinstance(listing_date, str):
                    listing_date = datetime.strptime(listing_date, '%Y-%m-%d').date()
                days_since_listing = (datetime.strptime(current_date, '%Y-%m-%d').date() - listing_date).days
                if 150 <= days_since_listing < 180:
                    filtered_stats['lockup_skip'] += 1
                    logger.debug(f"{stock_code} 跳过: 解禁期前30天(上市{days_since_listing}天)")
                    continue

            try:
                current_price = df['close'].iloc[-1]

                # 基本质量检查(新股放宽)
                if not new_stock_params['is_new_stock']:
                    quality_ok, _ = self.check_stock_quality(stock_code, df)
                    if not quality_ok:
                        filtered_stats['quality_fail'] += 1
                        continue

                # 动量得分过滤 - 新股使用调整后的阈值
                momentum_score, r2 = self.calculate_momentum_score_with_enhancements(df)

                # 【新】动量加速过滤:最近N天斜率 > 0 才入场
                # 新股(上市不足150天)跳过此过滤--短期波动不代表趋势
                slope, _ = self.calculate_momentum_acceleration(df)
                momentum_config = self.config.get('momentum', {})
                min_slope = momentum_config.get('momentum_accel_min_slope', 0.0)
                if not new_stock_params['is_new_stock'] and slope <= min_slope:
                    filtered_stats['momentum_fail'] += 1
                    logger.debug(f"{stock_code} 动量衰减过滤: score={momentum_score:.4f}, "
                               f"slope={slope:.4f}<={min_slope}")
                    continue

                # 新股用调整后的动量阈值
                min_momentum = new_stock_params['min_momentum']
                if momentum_score <= min_momentum or r2 < self.min_r2:
                    filtered_stats['momentum_fail'] += 1
                    logger.debug(f"{stock_code} 动量过滤失败: score={momentum_score:.4f}, min={min_momentum:.4f}, r2={r2:.4f}")
                    continue

                # 趋势过滤
                # - 新股(<150天):用自己的数据做RSRS短周期验证
                # - 解禁期前(150-180天):已在上面跳过,此处不会到达
                is_new = new_stock_params['is_new_stock']
                trend_pass, trend_reason = self.check_trend_filter(df, current_price, is_new_stock=is_new)
                if not trend_pass:
                    filtered_stats['trend_fail'] += 1
                    logger.debug(f"{stock_code} 趋势过滤失败: {trend_reason}")
                    continue

                # 入场确认 - 新股跳过
                if not is_new:
                    if not self.check_entry_confirmation(df):
                        filtered_stats['entry_fail'] += 1
                        continue

                if new_stock_params['is_new_stock']:
                    filtered_stats['new_stock_selected'] += 1
                    logger.info(f"新股选股: {stock_code} 上市天数={(datetime.strptime(current_date, '%Y-%m-%d').date() - listing_date).days if listing_date else 'N/A'} 动量={momentum_score:.4f}")

                filtered_stats['passed'] += 1
                candidates.append((stock_code, momentum_score))

            except (KeyError, ValueError, TypeError, IndexError) as e:
                logger.debug(f"{stock_code} 选股数据错误: {e}")
                continue
            except Exception as e:
                logger.error(f"{stock_code} 选股未知错误: {e}", exc_info=True)
                raise

        # 输出过滤统计
        logger.info(f"选股过滤统计: 总计={filtered_stats['total']}, "
                   f"数据不足={filtered_stats['data_insufficient']}, "
                   f"已持仓={filtered_stats['already_in_position']}, "
                   f"解禁期跳过={filtered_stats['lockup_skip']}, "
                   f"质量检查失败={filtered_stats['quality_fail']}, "
                   f"动量过滤失败={filtered_stats['momentum_fail']}, "
                   f"趋势过滤失败={filtered_stats['trend_fail']}, "
                   f"入场确认失败={filtered_stats['entry_fail']}, "
                   f"新股选股={filtered_stats['new_stock_selected']}, "
                   f"通过={filtered_stats['passed']}")

        # 按得分排序
        candidates.sort(key=lambda x: x[1], reverse=True)

        logger.info(f"选股详细结果={candidates}")
        # 行业分散控制
        momentum_config = self.config.get('momentum', {})
        max_same_sector = momentum_config.get('max_same_sector', 2)
        sector_counts = {}

        # 获取已持仓股票的行业
        for pos_code in self.positions:
            sector = self._get_stock_sector(pos_code)
            sector_counts[sector] = sector_counts.get(sector, 0) + 1

        # 选择股票(考虑行业分散)
        available_slots = self.max_positions - len(self.positions)
        # 候选池大小:传入的 pool_size 或 默认的 available_slots
        target_size = pool_size if pool_size is not None else available_slots
        selected_long = []

        for stock_code, score in candidates:
            if len(selected_long) >= target_size:
                break

            sector = self._get_stock_sector(stock_code)
            current_sector_count = sector_counts.get(sector, 0)

            if current_sector_count >= max_same_sector:
                logger.debug(f"{stock_code} 跳过: 行业{sector}已有{current_sector_count}只,超过限制{max_same_sector}")
                continue

            selected_long.append(stock_code)
            sector_counts[sector] = current_sector_count + 1

        # 记录行业分布
        if selected_long:
            sector_distribution = {}
            for code in selected_long:
                sector = self._get_stock_sector(code)
                sector_distribution[sector] = sector_distribution.get(sector, 0) + 1
            logger.info(f"选股行业分布: {sector_distribution}")

        return selected_long

    def _get_stock_sector(self, stock_code: str) -> str:
        """
        获取股票所属行业

        从配置或数据库中查找行业信息
        """
        # 尝试从配置中获取
        sector_map = self.config.get('sector_mapping', {})
        if stock_code in sector_map:
            return sector_map[stock_code]

        # 尝试从数据库获取
        try:
            from mutifactor.infra.yaml_storage import yaml_storage
            sector = yaml_storage.get_stock_sector(stock_code)
            if sector:
                return sector
        except Exception:
            pass

        # 使用股票代码前缀作为行业代理(简化处理)
        # 实际项目中应该有完整的行业映射表
        code_prefix = stock_code.split('.')[-1][:2]
        sector_proxy = {
            '00': '金融', '01': '地产', '02': '基建', '03': '能源',
            '05': '制造', '06': '制造', '07': '科技', '08': '消费',
            '09': '医药', '10': '科技', '11': '消费', '12': '金融',
            '13': '地产', '18': '科技', '19': '制造', '20': '消费',
            '23': '医药', '36': '科技', '39': '金融', '99': '科技'
        }
        return sector_proxy.get(code_prefix, '其他')

    def get_new_stock_params(self, listing_date, current_date):
        """
        新股动态参数调整

        根据上市天数返回不同的策略参数

        Args:
            listing_date: 上市日期 (date 或 str)
            current_date: 当前日期 (date 或 str)

        Returns:
            dict: {
                'min_momentum': 最小动量阈值,
                'use_fixed_vol': 是否使用固定波动率,
                'fixed_vol': 固定波动率值,
                'is_new_stock': 是否是新股
            }
        """
        from datetime import datetime

        # 解析日期
        if listing_date is None:
            return {
                'min_momentum': self.min_momentum_score,
                'use_fixed_vol': False,
                'fixed_vol': None,
                'is_new_stock': False
            }

        try:
            if isinstance(listing_date, str):
                listing_date = datetime.strptime(listing_date, '%Y-%m-%d').date()
            if isinstance(current_date, str):
                current_date = datetime.strptime(current_date, '%Y-%m-%d').date()
        except:
            return {
                'min_momentum': self.min_momentum_score,
                'use_fixed_vol': False,
                'fixed_vol': None,
                'is_new_stock': False
            }

        days_since_listing = (current_date - listing_date).days

        if days_since_listing <  150:
            # 上市小于150天(解禁期前30天):激进,跳过RSRS长周期
            return {
                'min_momentum': 0.0,  # 零动量也买(只要求不跌)
                'use_fixed_vol': True,
                'fixed_vol': 0.1,  # 固定8%日波动
                'is_new_stock': True
            }
        else:
            # 正常股票(上市超过150天或无上市日期)
            return {
                'min_momentum': self.min_momentum_score,
                'use_fixed_vol': False,
                'fixed_vol': None,
                'is_new_stock': False
            }

    def calculate_position_size(self, stock_code: str, price: float, available_cash: float) -> int:
        """计算仓位大小"""
        # 注意:available_cash 已经在 base.py 中处理过仓位比例
        return self.market_adapter.calculate_shares(available_cash, price, stock_code)

    def check_exit_signal(self, stock_code: str, position: dict, current_price: float, current_date: str):
        """检查平仓信号 - 使用统一退出策略接口"""
        try:
            # 获取该股票的历史数据用于计算ATR
            if not hasattr(self, '_stock_data_cache'):
                self._stock_data_cache = {}

            # 从缓存中获取数据(需要在run_backtest中设置)
            df = self._stock_data_cache.get(stock_code)

            # 使用统一的退出策略接口
            should_exit, reason, atr, tp_price, sl_price = check_exit_unified(
                config=self.config,
                position=position,
                current_price=current_price,
                kline_df=df,
                current_date=current_date,
                mode='backtest'
            )

            if should_exit:
                return reason

            return None
        except (KeyError, ValueError, TypeError, IndexError) as e:
            logger.warning(f"检查平仓信号数据错误 {stock_code}: {e}")
            return None
        except Exception as e:
            logger.error(f"检查平仓信号未知错误 {stock_code}: {e}", exc_info=True)
            raise


# 注册策略
from mutifactor.framework import StrategyFactory
StrategyFactory.register_strategy('momentum', MomentumStrategy)
