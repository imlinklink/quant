"""
止盈止损策略工厂 - 支持多种止盈止损策略

可用策略：
- chandelier: ATR吊灯止损（纯止损策略，追踪趋势）
- trailing: 吊灯止损 + 移动止盈
"""

import logging
from abc import ABC, abstractmethod
from typing import Dict, Tuple
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ==================== 常量定义 ====================
# 退出原因常量
EXIT_REASON_STOP_LOSS = 'atr_stop_loss'
EXIT_REASON_TAKE_PROFIT = 'atr_take_profit'
EXIT_REASON_TIME_EXIT = 'time_exit'            # 超时强制平仓
EXIT_REASON_DECLINE_STOP = 'decline_stop'     # 阴跌加速止损
EXIT_REASON_RSRS_STOP = 'rsrs_stop'            # RSRS转负止损
EXIT_REASON_EARLY_HARD_STOP = 'early_hard_stop'  # 早期硬止损（持仓<15天跌破-12%）
EXIT_REASON_RSRS_VOL_TAKE_PROFIT = 'rsrs_vol_take_profit'  # RSRS转负+量比过热止盈

# ATR 默认参数
DEFAULT_ATR_PERIOD = 14
DEFAULT_ATR_MULTIPLIER = 2.0
DEFAULT_CHANDELIER_PERIOD = 22

# RSRS / 量比预警参数
DEFAULT_RSRS_PERIOD = 18
DEFAULT_RSRS_EXIT_THRESHOLD = 0.0    # RSRS转负强制止损
DEFAULT_RSRS_WARN_THRESHOLD = 5.0    # RSRS从高位快速回落到这个区间开始预警
DEFAULT_VOL_RATIO_WARN_NEW = 5.0     # 新股量比过热预警
DEFAULT_VOL_RATIO_WARN_OLD = 3.0     # 老股量比过热预警
DEFAULT_RSRS_DECLINE_DAYS = 5        # RSRS连续下降N天才收紧止损

# ATR 吊顶止盈默认参数
DEFAULT_TAKE_PROFIT_MULTIPLIER = 3.0
DEFAULT_STOP_LOSS_MULTIPLIER = 2.0

# 其他常量
STOP_LOSS_FALLBACK_RATIO = 0.9  # ATR计算失败时的止损回退比例

# 时间退出默认参数
DEFAULT_TIME_EXIT_ENABLED = True
DEFAULT_PHASE1_DAYS = 90
DEFAULT_PHASE2_DAYS = 150
DEFAULT_PHASE3_DAYS = 200
DEFAULT_PHASE1_PERIOD = 10
DEFAULT_PHASE2_PERIOD = 5
DEFAULT_PHASE2_MULTIPLIER = 1.0

# 阴跌加速止损默认参数
DEFAULT_DECLINE_ACCEL_ENABLED = True
DEFAULT_DECLINE_MIN_HOLDING_DAYS = 60
DEFAULT_DECLINE_LOSS_THRESHOLD = -0.05
DEFAULT_DECLINE_ATR_MULTIPLIER = 1.0
DEFAULT_DECLINE_HARD_STOP_PCT = 0.08


class ExitStrategy(ABC):
    """止盈止损策略基类"""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.logger = logging.getLogger(self.__class__.__name__)
        # 通用参数
        self.atr_period = self.config.get('atr_period', DEFAULT_ATR_PERIOD)
        self.chandelier_period = self.config.get('chandelier_period', DEFAULT_CHANDELIER_PERIOD)
        # 时间退出参数
        time_exit_cfg = self.config.get('time_exit', {})
        self.time_exit_enabled = time_exit_cfg.get('enabled', DEFAULT_TIME_EXIT_ENABLED)
        self.phase1_days = time_exit_cfg.get('phase1_days', DEFAULT_PHASE1_DAYS)
        self.phase2_days = time_exit_cfg.get('phase2_days', DEFAULT_PHASE2_DAYS)
        self.phase3_days = time_exit_cfg.get('phase3_days', DEFAULT_PHASE3_DAYS)
        self.phase1_period = time_exit_cfg.get('phase1_period', DEFAULT_PHASE1_PERIOD)
        self.phase2_period = time_exit_cfg.get('phase2_period', DEFAULT_PHASE2_PERIOD)
        self.phase2_multiplier = time_exit_cfg.get('phase2_multiplier', DEFAULT_PHASE2_MULTIPLIER)
        self.time_exit_profit_exempt_pct = time_exit_cfg.get('profit_exempt_pct', 0.30)
        # 阴跌加速止损参数
        decline_cfg = self.config.get('decline_acceleration', {})
        self.decline_accel_enabled = decline_cfg.get('enabled', DEFAULT_DECLINE_ACCEL_ENABLED)
        self.decline_min_holding_days = decline_cfg.get('min_holding_days', DEFAULT_DECLINE_MIN_HOLDING_DAYS)
        self.decline_loss_threshold = decline_cfg.get('loss_threshold', DEFAULT_DECLINE_LOSS_THRESHOLD)
        self.decline_atr_multiplier = decline_cfg.get('atr_multiplier_tight', DEFAULT_DECLINE_ATR_MULTIPLIER)
        self.decline_hard_stop_pct = decline_cfg.get('hard_stop_loss_pct', DEFAULT_DECLINE_HARD_STOP_PCT)
        
        # RSRS / 量比预警参数
        rsrs_cfg = self.config.get('rsrs_warn', {})
        self.rsrs_period = rsrs_cfg.get('period', DEFAULT_RSRS_PERIOD)
        self.rsrs_exit_threshold = rsrs_cfg.get('exit_threshold', DEFAULT_RSRS_EXIT_THRESHOLD)
        self.rsrs_warn_threshold = rsrs_cfg.get('warn_threshold', DEFAULT_RSRS_WARN_THRESHOLD)
        self.vol_ratio_warn_new = rsrs_cfg.get('vol_ratio_warn_new', DEFAULT_VOL_RATIO_WARN_NEW)
        self.vol_ratio_warn_old = rsrs_cfg.get('vol_ratio_warn_old', DEFAULT_VOL_RATIO_WARN_OLD)
        self.rsrs_decline_days = rsrs_cfg.get('decline_days', DEFAULT_RSRS_DECLINE_DAYS)
        # RSRS早期豁免参数（持仓前N天不触发RSRS止损，用硬止损替代）
        self.rsrs_early_exempt_days = rsrs_cfg.get('early_exempt_days', 15)
        self.early_hard_stop_pct = self.config.get('early_hard_stop_pct', 0.12)

    @abstractmethod
    def calculate_stop_loss(self, position: dict, df: pd.DataFrame) -> float:
        """计算止损价"""
        pass

    @abstractmethod
    def check_exit(self, position: dict, current_price: float,
                   df: pd.DataFrame) -> Tuple[bool, str, float, float, float]:
        """检查是否触发止盈止损

        Returns:
            (should_exit, reason, atr, take_profit_price, stop_loss_price) - 是否平仓、原因、ATR、止盈价、止损价
        """
        pass

    def _calculate_chandelier_stop_loss(self, position: dict, df: pd.DataFrame,
                                        atr_multiplier: float) -> float:
        """
        计算吊灯止损价（公共方法）

        止损价 = max(买入价, N日最高价) - ATR × 倍数
        止损价不能高于买入价，避免买入后立即触发止损
        """
        atr = self.calculate_atr(df, self.atr_period)
        if atr <= 0:
            return position['cost_price'] * STOP_LOSS_FALLBACK_RATIO

        cost_price = position['cost_price']

        # 获取过去N天的最高价
        lookback = min(self.chandelier_period, len(df))
        period_high = df['high'].tail(lookback).max()

        # 止损基准价不能高于买入价
        stop_base_price = min(period_high, cost_price)

        stop_price = stop_base_price - atr_multiplier * atr

        # 止损价只能上移，但不能超过成本价
        prev_stop = position.get('stop_price', 0)
        return min(max(stop_price, prev_stop), cost_price)

    def _update_highest_price(self, position: dict, current_price: float) -> float:
        """更新持仓最高价"""
        cost_price = position.get('cost_price', current_price)
        highest_price = position.get('highest_price', cost_price)
        if current_price > highest_price:
            position['highest_price'] = current_price
            highest_price = current_price
        return highest_price

    def _check_chandelier_stop_loss(self, position: dict, current_price: float,
                                    df: pd.DataFrame, stop_price: float, atr: float) -> Tuple[bool, str]:
        """
        检查吊灯止损

        Returns:
            (should_exit, reason) - 是否触发及原因
        """
        if current_price <= stop_price:
            cost_price = position['cost_price']
            profit_pct = (current_price - cost_price) / cost_price * 100
            self.logger.info(
                f"🔴 吊灯止损触发 | 当前:{current_price:.3f} | "
                f"止损价:{stop_price:.3f} | ATR:{atr:.3f} | "
                f"盈亏:{profit_pct:.1f}%"
            )
            return True, EXIT_REASON_STOP_LOSS
        return False, None

    def _get_holding_days(self, position: dict) -> int:
        """计算持仓天数"""
        buy_date = position.get('buy_date', '')
        if not buy_date:
            return 0
        try:
            buy_dt = pd.to_datetime(buy_date)
            today = pd.Timestamp.now().normalize()
            return (today - buy_dt).days
        except (ValueError, TypeError):
            return 0

    def _get_holding_days_backtest(self, position: dict, current_date: str) -> int:
        """回测中计算持仓天数（使用传入日期而非今天）"""
        buy_date = position.get('buy_date', '')
        if not buy_date or not current_date:
            return 0
        try:
            buy_dt = pd.to_datetime(buy_date)
            sell_dt = pd.to_datetime(current_date)
            return (sell_dt - buy_dt).days
        except (ValueError, TypeError):
            return 0

    def _check_time_exit(self, position: dict, current_price: float,
                         current_date: str = None) -> Tuple[bool, str]:
        """
        渐进式时间退出检查

        阶段1（>phase1_days）：缩短chandelier_period，止损更灵敏
        阶段2（>phase2_days）：进一步缩短 + ATR倍数减半
        阶段3（>phase3_days）：强制平仓（不豁免，硬性上限）

        Returns:
            (should_force_exit, phase) - 是否强制平仓及所处阶段
        """
        if not self.time_exit_enabled:
            return False, ''

        # 回测时从position获取日期，实盘时使用current_date或今天
        date_str = current_date or position.get('_current_date', '')
        holding_days = self._get_holding_days_backtest(position, date_str) if date_str else self._get_holding_days(position)

        # phase3 强制平仓，不豁免（设置硬性持仓上限）
        if holding_days > self.phase3_days:
            cost_price = position.get('cost_price', current_price)
            profit_pct = (current_price - cost_price) / cost_price if cost_price > 0 else 0
            self.logger.info(
                f"⏰ 时间退出触发 | 持仓{holding_days}天 > {self.phase3_days}天阈值 | "
                f"盈亏:{profit_pct*100:.1f}% | 强制平仓 | 当前:{current_price:.3f}"
            )
            return True, EXIT_REASON_TIME_EXIT

        return False, ''

    def _get_time_adjusted_stop_params(self, position: dict, current_date: str = None) -> Tuple[int, float]:
        """
        根据持仓天数获取调整后的止损参数

        Returns:
            (chandelier_period, atr_multiplier) - 调整后的参数
        """
        date_str = current_date or position.get('_current_date', '')
        holding_days = self._get_holding_days_backtest(position, date_str) if date_str else self._get_holding_days(position)

        if holding_days > self.phase2_days:
            return self.phase2_period, self.phase2_multiplier
        elif holding_days > self.phase1_days:
            return self.phase1_period, self.config.get('stop_loss_multiplier', DEFAULT_STOP_LOSS_MULTIPLIER)

        return self.chandelier_period, self.config.get('stop_loss_multiplier', DEFAULT_STOP_LOSS_MULTIPLIER)

    def _check_decline_acceleration(self, position: dict, current_price: float,
                                     df: pd.DataFrame, atr: float,
                                     current_date: str = None) -> Tuple[bool, str, float]:
        """
        阴跌趋势加速止损检测

        条件A: 持仓超过 min_holding_days 且浮亏超过 loss_threshold
        条件B: 价格低于 MA20（轻度收紧）
        条件C: MA20 < MA60（均线死叉，重度收紧）

        Returns:
            (should_exit, reason, adjusted_stop_price) - 是否退出、原因、调整后的止损价
            如果不触发加速，返回 (False, '', 0.0)
        """
        if not self.decline_accel_enabled:
            return False, '', 0.0

        cost_price = position['cost_price']
        profit_pct = (current_price - cost_price) / cost_price

        date_str = current_date or position.get('_current_date', '')
        holding_days = self._get_holding_days_backtest(position, date_str) if date_str else self._get_holding_days(position)

        # 条件A：持仓超过最小天数 且 浮亏超过阈值
        if holding_days <= self.decline_min_holding_days or profit_pct >= self.decline_loss_threshold:
            return False, '', 0.0

        # 计算均线（SMA更稳定）
        if len(df) < 60:
            return False, '', 0.0

        try:
            ma20 = df['close'].tail(20).mean()
            ma60 = df['close'].tail(60).mean()
        except (KeyError, IndexError):
            return False, '', 0.0

        # 条件C：MA20 < MA60（均线死叉，趋势确认走坏）→ 硬止损
        if ma20 < ma60:
            hard_stop_price = cost_price * (1 - self.decline_hard_stop_pct)
            if current_price <= hard_stop_price:
                self.logger.info(
                    f"📉 阴跌加速止损触发(死叉) | 持仓{holding_days}天 | "
                    f"亏损:{profit_pct*100:.1f}% | MA20:{ma20:.3f} < MA60:{ma60:.3f} | "
                    f"硬止损:{hard_stop_price:.3f} | 当前:{current_price:.3f}"
                )
                return True, EXIT_REASON_DECLINE_STOP, hard_stop_price
            # 记录调整后的止损价供日志展示（但不强制触发，等价格触及）
            return False, '', hard_stop_price

        # 条件B：价格低于 MA20（轻度收紧）→ 收紧ATR止损
        if current_price < ma20:
            tight_stop_price = current_price - self.decline_atr_multiplier * atr if atr > 0 else cost_price * 0.9
            self.logger.debug(
                f"📉 阴跌预警 | 持仓{holding_days}天 | 亏损:{profit_pct*100:.1f}% | "
                f"价格:{current_price:.3f} < MA20:{ma20:.3f} | "
                f"收紧止损:{tight_stop_price:.3f}"
            )
            return False, '', tight_stop_price

        return False, '', 0.0

    @staticmethod
    def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
        """计算 ATR（向量化实现）"""
        if len(df) < period + 1:
            return 0.0

        try:
            high = df['high'].values
            low = df['low'].values
            close = df['close'].values

            # 向量化计算真实波幅 (TR)
            # TR = max(high - low, |high - prev_close|, |low - prev_close|)
            tr1 = high - low
            tr2 = np.abs(high - np.roll(close, 1))
            tr3 = np.abs(low - np.roll(close, 1))
            tr = np.maximum(tr1, np.maximum(tr2, tr3))
            tr[0] = high[0] - low[0]  # 第一行没有前收盘价

            return np.mean(tr[-period:])
        except (ValueError, TypeError, IndexError) as e:
            logger.warning(f"ATR计算失败: {e}")
            return 0.0

    def calculate_rsrs_slope(self, df: pd.DataFrame, period: int = None) -> float:
        """计算RSRS斜率（与momentum.py逻辑一致）"""
        period = period or self.rsrs_period
        if len(df) < period:
            return 0.0
        recent = df.tail(period)
        try:
            x = np.arange(period)
            slope_high, _ = np.polyfit(x, recent['high'].values, 1)
            slope_low, _ = np.polyfit(x, recent['low'].values, 1)
            return (slope_high + slope_low) / 2
        except:
            return 0.0

    def calculate_volume_ratio(self, df: pd.DataFrame, window: int = 20) -> float:
        """计算成交量放大比"""
        if len(df) < window + 5:
            return 1.0
        recent_vol = df['volume'].tail(window).mean()
        prev_vol = df['volume'].tail(window + 5).head(window).mean()
        return recent_vol / prev_vol if prev_vol > 0 else 1.0

    def _get_rsrs_and_vol_ratio(self, position: dict, df: pd.DataFrame) -> Tuple[float, float, bool, bool]:
        """
        计算RSRS斜率和量比，返回(斜率, 量比, 是否过热, 是否RSRS预警)
        """
        rsrs = self.calculate_rsrs_slope(df)
        vol_ratio = self.calculate_volume_ratio(df)
        
        # 判断是否新股（上市不足90天）
        listing_date = position.get('listing_date')
        is_new_stock = False
        if listing_date:
            try:
                from datetime import datetime
                listing_dt = datetime.strptime(str(listing_date)[:10], '%Y-%m-%d')
                today = datetime.now()
                if (today - listing_dt).days <= 90:
                    is_new_stock = True
            except:
                pass
        
        # 量比过热阈值
        vol_warn_threshold = self.vol_ratio_warn_new if is_new_stock else self.vol_ratio_warn_old
        vol_overheated = vol_ratio >= vol_warn_threshold
        
        # RSRS预警：连续下降后转负或从高位快速回落
        # 注意：用 `or` 而不是 `dict.get(default)`，因为 position['_prev_rsrs'] = None 时 get 仍返回 None
        prev_rsrs = position.get('_prev_rsrs') or rsrs
        rsrs_declining = (rsrs < prev_rsrs and prev_rsrs > self.rsrs_warn_threshold and rsrs > 0)
        rsrs_negative = rsrs <= self.rsrs_exit_threshold
        
        position['_prev_rsrs'] = rsrs
        
        return rsrs, vol_ratio, vol_overheated, rsrs_negative or rsrs_declining

    def _check_rsrs_exit(self, position: dict, current_price: float,
                         rsrs: float, atr: float,
                         vol_overheated: bool, vol_ratio: float) -> Tuple[bool, str, float]:
        """
        RSRS退出检查

        条件：RSRS斜率 <= 阈值
        收紧：当RSRS转负或量比过热时，收紧止损价

        新增逻辑：持仓前 rsrs_early_exempt_days 天内不触发RSRS止损，
        改用 early_hard_stop_pct 硬止损保护（防崩盘）

        Returns:
            (should_exit, reason, adjusted_stop_price)
        """
        cost_price = position['cost_price']
        profit_pct = (current_price - cost_price) / cost_price if cost_price > 0 else 0

        # 计算持仓天数
        date_str = position.get('_current_date', '')
        holding_days = self._get_holding_days_backtest(position, date_str) if date_str else self._get_holding_days(position)

        # ========== 早期豁免：持仓前N天不触发RSRS，改用硬止损 ==========
        if holding_days < self.rsrs_early_exempt_days:
            # 早期硬止损：跌破买入价 early_hard_stop_pct 则无条件止损
            if profit_pct <= -self.early_hard_stop_pct:
                self.logger.info(
                    f"[早期硬止损] 🔴 持仓{holding_days}天 < {self.rsrs_early_exempt_days}天(免RSRS) | "
                    f"亏损:{profit_pct*100:.1f}% <= -{self.early_hard_stop_pct*100:.0f}% | "
                    f"当前:{current_price:.3f} | 成本:{cost_price:.3f}"
                )
                return True, EXIT_REASON_EARLY_HARD_STOP, current_price
            # 早期不触发RSRS，也不止损
            self.logger.debug(
                f"[早期豁免] 持仓{holding_days}天 < {self.rsrs_early_exempt_days}天，"
                f"RSRS豁免 | 亏损:{profit_pct*100:.1f}% | 硬止损线:-{self.early_hard_stop_pct*100:.0f}%"
            )
            # 仍需更新prev_rsrs，避免豁免期结束后跳变
            position['_prev_rsrs'] = rsrs
            return False, '', 0.0

        # 新股（上市<90天）跳过RSRS退出检查
        listing_date = position.get('listing_date')
        if listing_date:
            try:
                from datetime import datetime
                listing_dt = datetime.strptime(str(listing_date)[:10], '%Y-%m-%d')
                today = datetime.now()
                if (today - listing_dt).days <= 90:
                    return False, '', 0.0
            except:
                pass

        # 追踪RSRS前值（从持仓position中获取）
        prev_rsrs = position.get('_prev_rsrs') or rsrs

        # RSRS转负：直接退出
        if rsrs <= self.rsrs_exit_threshold:
            self.logger.info(
                f"[RSRS退出] 🔴 RSRS转负止损 | 当前:{current_price:.3f} | "
                f"RSRS:{rsrs:.4f} | 前值:{prev_rsrs:.4f} | 盈利:{profit_pct*100:.1f}%"
            )
            position['_prev_rsrs'] = rsrs
            return True, EXIT_REASON_RSRS_STOP, current_price

        # RSRS从高位连续回落（比如从5以上快速掉到0附近）
        # 条件：前值在高位，当前值也在正区间但快速下降
        rsrs_declining = (rsrs < prev_rsrs and prev_rsrs > self.rsrs_warn_threshold and rsrs > 0)
        if rsrs_declining:
            self.logger.info(
                f"[RSRS退出] 🔴 RSRS高位回落止损 | 当前:{current_price:.3f} | "
                f"RSRS:{rsrs:.4f} | 前值:{prev_rsrs:.4f} | 盈利:{profit_pct*100:.1f}%"
            )
            position['_prev_rsrs'] = rsrs
            return True, EXIT_REASON_RSRS_STOP, current_price

        # 正常更新prev_rsrs
        position['_prev_rsrs'] = rsrs
        return False, '', 0.0


class ATRStopOnlyStrategy(ExitStrategy):
    """ATR纯止损策略（保守型）

    止损价 = N日最高价 - ATR × 倍数
    止损价只能上移，追踪趋势

    特点：
    - 给予趋势足够空间，避免被震出
    - 自动追踪盈利，锁定浮盈
    - 让利润奔跑
    - 只有止损，没有止盈（理论上可以无限持有）
    """

    def __init__(self, config: Dict = None):
        super().__init__(config)
        self.atr_multiplier = self.config.get('atr_multiplier', DEFAULT_ATR_MULTIPLIER)

    def calculate_stop_loss(self, position: dict, df: pd.DataFrame) -> float:
        """计算吊灯止损价"""
        return self._calculate_chandelier_stop_loss(position, df, self.atr_multiplier)

    def check_exit(self, position: dict, current_price: float,
                   df: pd.DataFrame) -> Tuple[bool, str, float, float, float]:
        # 更新最高价
        self._update_highest_price(position, current_price)

        # 计算吊灯止损价
        atr = self.calculate_atr(df, self.atr_period)
        stop_price = self.calculate_stop_loss(position, df)
        position['stop_price'] = stop_price
        position['atr'] = atr
        position['take_profit_price'] = 0.0  # 此策略无止盈价

        # 检查吊灯止损
        should_exit, reason = self._check_chandelier_stop_loss(
            position, current_price, df, stop_price, atr
        )
        if should_exit:
            return True, reason, atr, 0.0, stop_price

        return False, '', atr, 0.0, stop_price


class ATRDynamicStrategy(ExitStrategy):
    """ATR动态止盈止损策略

    止盈：从最高价回撤 ATR × 倍数（吊顶止盈）
    止损：从最高价下跌 ATR × 倍数（吊灯止损）

    特点：
    - 完全基于ATR动态调整止盈止损
    - 让利润充分奔跑
    - 动态适应市场波动
    """

    def __init__(self, config: Dict = None):
        super().__init__(config)
        self.take_profit_multiplier = self.config.get('take_profit_multiplier', DEFAULT_TAKE_PROFIT_MULTIPLIER)
        self.stop_loss_multiplier = self.config.get('stop_loss_multiplier', DEFAULT_STOP_LOSS_MULTIPLIER)

    def calculate_stop_loss(self, position: dict, df: pd.DataFrame) -> float:
        """计算吊灯止损价"""
        return self._calculate_chandelier_stop_loss(position, df, self.stop_loss_multiplier)

    def _check_atr_take_profit(self, position: dict, current_price: float, atr: float) -> Tuple[bool, str]:
        """检查ATR吊顶止盈"""
        cost_price = position['cost_price']
        highest_price = position.get('highest_price', cost_price)

        # 从最高价回撤 ATR × 止盈倍数
        take_profit_price = highest_price - self.take_profit_multiplier * atr

        # 关键修复：只有盈利且价格跌破止盈线时才触发止盈
        profit_pct = (current_price - cost_price) / cost_price
        if profit_pct > 0 and current_price <= take_profit_price:
            profit_pct_display = profit_pct * 100
            self.logger.info(
                f"🟢 吊顶止盈触发 | 当前:{current_price:.3f} | "
                f"最高:{highest_price:.3f} | 止盈价:{take_profit_price:.3f} | "
                f"ATR:{atr:.3f} | 盈利:{profit_pct_display:.1f}%"
            )
            return True, EXIT_REASON_TAKE_PROFIT
        return False, None

    def check_exit(self, position: dict, current_price: float,
                   df: pd.DataFrame) -> Tuple[bool, str, float, float, float]:
        # 更新最高价
        self._update_highest_price(position, current_price)

        atr = self.calculate_atr(df, self.atr_period)
        position['atr'] = atr

        # ========== RSRS / 量比预警（仅记录，不强制退出）==========
        rsrs, vol_ratio, vol_overheated, rsrs_warn = self._get_rsrs_and_vol_ratio(position, df)
        position['rsrs'] = rsrs
        position['vol_ratio'] = vol_ratio
        
        cost_price = position['cost_price']
        profit_pct = (current_price - cost_price) / cost_price if cost_price > 0 else 0

        # ========== 优先级2：超时强制平仓 ==========
        should_force_exit, reason = self._check_time_exit(position, current_price)
        if should_force_exit:
            return True, reason, atr, 0.0, 0.0

        # ========== 吊顶止盈 ==========
        take_profit_price = 0.0
        if profit_pct > 0:
            should_exit, reason = self._check_atr_take_profit(position, current_price, atr)
            
            highest_price = position.get('highest_price', cost_price)
            take_profit_price = highest_price - self.take_profit_multiplier * atr
            position['take_profit_price'] = take_profit_price

            if should_exit:
                return True, reason, atr, take_profit_price, 0.0
            
            # 量比过热预警：提醒但不强制止盈
            if vol_overheated:
                self.logger.info(
                    f"🔥 量比过热预警 | 当前:{current_price:.3f} | 量比:{vol_ratio:.2f} | "
                    f"RSRS:{rsrs:.4f} | 盈利:{profit_pct*100:.1f}% | 可考虑止盈"
                )
        else:
            position['take_profit_price'] = 0.0

        # ========== 吊灯止损（带RSRS/量比收紧） ==========
        # 浮盈超过豁免比例时，不收紧止损（让大牛股有足够空间）
        if profit_pct > self.time_exit_profit_exempt_pct:
            adjusted_period, adjusted_multiplier = self.chandelier_period, self.stop_loss_multiplier
        else:
            # 根据持仓天数动态调整止损参数
            adjusted_period, adjusted_multiplier = self._get_time_adjusted_stop_params(position)
        if adjusted_period != self.chandelier_period or adjusted_multiplier != self.stop_loss_multiplier:
            # 使用调整后的参数计算止损
            old_period = self.chandelier_period
            old_multiplier = self.stop_loss_multiplier
            self.chandelier_period = adjusted_period
            self.stop_loss_multiplier = adjusted_multiplier
            stop_price = self.calculate_stop_loss(position, df)
            # 恢复原参数（避免影响其他逻辑）
            self.chandelier_period = old_period
            self.stop_loss_multiplier = old_multiplier
        else:
            stop_price = self.calculate_stop_loss(position, df)

        # ========== 阴跌加速止损 ==========
        decline_exit, decline_reason, decline_stop = self._check_decline_acceleration(
            position, current_price, df, atr
        )
        if decline_stop > 0:
            # 使用阴跌止损价和ATR止损价中较高的那个（更保守）
            stop_price = max(stop_price, decline_stop)
        if decline_exit:
            return True, decline_reason, atr, take_profit_price, stop_price

        # ========== RSRS退出检查 ==========
        rsrs_exit, rsrs_exit_reason, rsrs_exit_stop = self._check_rsrs_exit(
            position, current_price, rsrs, atr, vol_overheated, vol_ratio
        )
        if rsrs_exit:
            return True, rsrs_exit_reason, atr, take_profit_price, rsrs_exit_stop

        position['stop_price'] = stop_price

        should_exit, reason = self._check_chandelier_stop_loss(
            position, current_price, df, stop_price, atr
        )
        if should_exit:
            return True, reason, atr, take_profit_price, stop_price
        
        # RSRS/量比预警但未触发止损 → 记录到日志
        if rsrs_warn or vol_overheated:
            self.logger.debug(
                f"⚠️ 预警状态 | RSRS:{rsrs:.4f}({'预警' if rsrs_warn else '正常'}) | "
                f"量比:{vol_ratio:.2f}({'过热' if vol_overheated else '正常'}) | "
                f"止损价:{stop_price:.3f} | 当前:{current_price:.3f}"
            )

        return False, '', atr, take_profit_price, stop_price


class ExitStrategyFactory:
    """止盈止损策略工厂"""

    _strategies = {
        'atr_dynamic': ATRDynamicStrategy,
        'atr_stop_only': ATRStopOnlyStrategy,
    }

    @classmethod
    def register(cls, name: str, strategy_class: type):
        """注册新策略"""
        cls._strategies[name] = strategy_class

    @classmethod
    def create(cls, strategy_type: str = 'atr_dynamic',
               config: Dict = None) -> ExitStrategy:
        """创建策略实例

        Args:
            strategy_type: 策略类型 ('atr_dynamic', 'atr_stop_only')
            config: 策略配置

        Returns:
            ExitStrategy 实例
        """
        if strategy_type not in cls._strategies:
            logger.warning(f"未知策略类型 '{strategy_type}'，使用默认 'atr_dynamic'")
            strategy_type = 'atr_dynamic'

        strategy_class = cls._strategies[strategy_type]
        instance = strategy_class(config)

        logger.info(f"创建止盈止损策略: {strategy_type} ({strategy_class.__name__})")
        return instance

    @classmethod
    def list_strategies(cls) -> list:
        """列出所有可用策略"""
        return list(cls._strategies.keys())

    @classmethod
    def check_exit_with_dataframe(cls, position: dict, current_price: float,
                                   config: Dict, kline_df: pd.DataFrame,
                                   today_high: float = None, today_low: float = None) -> Tuple[bool, str, float, float, float]:
        """
        使用缓存的DataFrame检查止盈止损

        Args:
            position: 持仓信息 {'stock_code', 'quantity', 'cost_price', 'highest_price'}
            current_price: 当前价格
            config: 配置字典
            kline_df: 缓存的K线数据DataFrame
            today_high: 当天最高价（用于融合当日实时行情）
            today_low: 当天最低价（用于融合当日实时行情）

        Returns:
            (should_exit, reason, atr, take_profit_price, stop_loss_price)
        """
        # 检查配置是否有效
        if config is None:
            raise ValueError("配置文件缺失，无法执行止盈止损检查")

        stock_code = position.get('stock_code', '')
        risk_config = config.get('risk', {})
        exit_strategy_type = risk_config.get('exit_strategy', 'atr_dynamic')

        # 检查DataFrame是否有效
        if kline_df is None or len(kline_df) < 22:
            logger.warning(f"{stock_code} K线数据不足22条，使用简化止损")
            return cls.check_exit_simple(position, current_price, config)

        # 融合当天的实时行情数据
        kline_df = cls._merge_today_realtime_data(kline_df, current_price, today_high, today_low)

        # 使用策略检查
        if exit_strategy_type in ['atr_dynamic', 'atr_stop_only']:
            exit_strategy = cls.create(exit_strategy_type, config)
            return exit_strategy.check_exit(position, current_price, kline_df)

        # 回退到简化止损逻辑
        should_exit, reason, _, _, _ = cls.check_exit_simple(position, current_price, config)
        # 补充返回3个默认值（简化止损没有ATR、止盈价、止损价）
        return should_exit, reason, 0.0, 0.0, 0.0

    @classmethod
    def check_exit_simple(cls, position: dict, current_price: float,
                          config: Dict) -> Tuple[bool, str, float, float, float]:
        """
        移动止损检查（K线数据不足时使用）

        规则：
        - 只要历史最高点相对成本价浮盈达到5%，就进入跟踪模式
        - 跟踪模式下，最高点回撤2%即止盈
        - 亏损超过12%硬止损

        Args:
            position: 持仓信息 {'stock_code', 'cost_price', 'highest_price'}
            current_price: 当前价格
            config: 配置字典

        Returns:
            (should_exit, reason, atr, take_profit_price, stop_loss_price)
        """
        stock_code = position.get('stock_code', '')
        cost_price = position.get('cost_price', 0.0)
        highest_price = position.get('highest_price', current_price)

        if cost_price <= 0:
            return False, '', 0.0, 0.0, 0.0

        # 更新最高点
        if current_price > highest_price:
            highest_price = current_price

        # 计算历史最高点相对成本价的浮盈
        peak_profit_pct = (highest_price - cost_price) / cost_price

        # 只要历史最高浮盈达到5%，就进入移动止损模式
        if peak_profit_pct >= 0.05:
            # 移动止损：最高点回撤2%止盈
            # 用 >= 并加容差处理浮点数精度
            stop_threshold = highest_price * 0.98
            if current_price <= stop_threshold + 0.001:
                actual_pct = (current_price - cost_price) / cost_price
                reason = f'trailing_stop 峰值{peak_profit_pct*100:.1f}% 回撤至{actual_pct*100:.1f}%'
                logger.info(f"🟢 {stock_code} {reason}")
                return True, reason, 0.0, stop_threshold, 0.0

        # 硬止损（亏损超过12%必须出）
        loss_pct = (current_price - cost_price) / cost_price
        risk_config = config.get('risk', {})
        early_hard_stop_pct = risk_config.get('early_hard_stop_pct', 0.12)
        if loss_pct <= -early_hard_stop_pct:
            reason = f'hard_stop 亏损{abs(loss_pct)*100:.1f}%'
            logger.info(f"🔴 {stock_code} {reason}")
            stop_price = cost_price * (1 - early_hard_stop_pct)
            return True, reason, 0.0, 0.0, stop_price

        return False, '', 0.0, 0.0, 0.0

    @classmethod
    def _merge_today_realtime_data(cls, df: pd.DataFrame, current_price: float,
                                    today_high: float = None, today_low: float = None) -> pd.DataFrame:
        """
        将当天的实时行情数据融合到 K 线数据中

        这样 ATR 计算才能反映当天的真实波动情况。

        Args:
            df: 历史 K 线数据
            current_price: 当前价格（作为当天的收盘价）
            today_high: 当天最高价（可选，如果提供则使用）
            today_low: 当天最低价（可选，如果提供则使用）

        Returns:
            融合后的 K 线数据
        """
        if df is None or len(df) == 0:
            return df

        # 复制避免修改原数据
        df = df.copy()

        # 获取今天的日期
        today = pd.Timestamp.now().normalize()

        # 检查最后一根 K 线是否是今天
        last_date = df['date'].iloc[-1]
        if isinstance(last_date, str):
            last_date = pd.to_datetime(last_date)
        elif isinstance(last_date, pd.Timestamp):
            last_date = last_date.normalize()
        # 如果是 datetime.date，直接使用

        # 确定当天的最高价和最低价
        if today_high is None:
            today_high = current_price
        if today_low is None:
            today_low = current_price

        if pd.Timestamp(last_date) == today:
            # 最后一根 K 线是今天，更新当天数据
            df.loc[df.index[-1], 'high'] = max(df['high'].iloc[-1], today_high, current_price)
            df.loc[df.index[-1], 'low'] = min(df['low'].iloc[-1], today_low, current_price)
            df.loc[df.index[-1], 'close'] = current_price
            logger.debug(f"更新今日K线: high={df['high'].iloc[-1]:.2f}, low={df['low'].iloc[-1]:.2f}, close={current_price:.2f}")
        else:
            # 需要追加今天的 K 线数据
            new_row = {
                'date': today,
                'open': current_price,  # 无法获取今日开盘价，用当前价近似
                'high': max(today_high, current_price),
                'low': min(today_low, current_price),
                'close': current_price,
                'volume': 0  # 成交量未知
            }
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            logger.debug(f"追加今日K线: high={new_row['high']:.2f}, low={new_row['low']:.2f}, close={current_price:.2f}")

        return df



