"""
统一退出策略接口 - 回测和实盘共用

提供统一的止盈止损检查接口，屏蔽回测和实盘的差异。
"""

import logging
from typing import Dict, Tuple, Optional
from datetime import datetime
import pandas as pd

from .exit_strategy import (
    ExitStrategyFactory,
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_TAKE_PROFIT,
    EXIT_REASON_TIME_EXIT,
    EXIT_REASON_DECLINE_STOP,
    EXIT_REASON_RSRS_STOP,
    EXIT_REASON_EARLY_HARD_STOP,
    EXIT_REASON_RSRS_VOL_TAKE_PROFIT,
)

logger = logging.getLogger(__name__)

# 导出所有退出原因常量
__all__ = [
    'UnifiedExitStrategy',
    'check_exit_unified',
    'EXIT_REASON_STOP_LOSS',
    'EXIT_REASON_TAKE_PROFIT',
    'EXIT_REASON_TIME_EXIT',
    'EXIT_REASON_DECLINE_STOP',
    'EXIT_REASON_RSRS_STOP',
    'EXIT_REASON_EARLY_HARD_STOP',
    'EXIT_REASON_RSRS_VOL_TAKE_PROFIT',
]


class UnifiedExitStrategy:
    """
    统一退出策略 - 支持回测和实盘两种模式
    
    使用方式:
    # 回测模式
    strategy = UnifiedExitStrategy(config, mode='backtest')
    should_exit, reason = strategy.check_exit(position, current_price, kline_df, current_date)
    
    # 实盘模式  
    strategy = UnifiedExitStrategy(config, mode='live')
    should_exit, reason = strategy.check_exit(position, current_price, kline_df)
    """
    
    def __init__(self, config: Dict, mode: str = 'backtest'):
        """
        初始化统一退出策略
        
        Args:
            config: 配置字典
            mode: 运行模式，'backtest' 或 'live'
        """
        self.config = config or {}
        self.mode = mode
        self._exit_strategy = None  # 懒加载
        
    def _get_exit_strategy(self):
        """懒加载退出策略实例"""
        if self._exit_strategy is None:
            risk_config = self.config.get('risk', {})
            strategy_type = risk_config.get('exit_strategy', 'atr_dynamic')
            self._exit_strategy = ExitStrategyFactory.create(strategy_type, self.config)
        return self._exit_strategy
        
    def check_exit(self,
                   position: Dict,
                   current_price: float,
                   kline_df: Optional[pd.DataFrame] = None,
                   current_date: Optional[str] = None,
                   today_high: Optional[float] = None,
                   today_low: Optional[float] = None) -> Tuple[bool, str, float, float, float]:
        """
        统一退出检查接口
        
        Args:
            position: 持仓信息
                - stock_code: 股票代码
                - quantity: 数量
                - cost_price: 成本价
                - highest_price: 最高价（用于追踪止盈）
                - buy_date: 买入日期（回测需要）
                - stop_price: 止损价（可选）
            current_price: 当前价格
            kline_df: K线数据（优先使用，回测和实盘都支持）
            current_date: 当前日期（回测需要，格式 'YYYY-MM-DD'）
            today_high: 当日最高价（实盘优先使用）
            today_low: 当日最低价（实盘优先使用）
            
        Returns:
            (should_exit, reason, atr, take_profit_price, stop_loss_price)
            - should_exit: 是否触发退出
            - reason: 退出原因（如 'atr_stop_loss'）
            - atr: ATR值
            - take_profit_price: 止盈价格
            - stop_loss_price: 止损价格
        """
        # 补充当前日期到position（时间退出需要）
        if current_date:
            position['_current_date'] = current_date
        elif self.mode == 'backtest' and '_current_date' not in position:
            logger.warning("回测模式需要提供 current_date 参数")
            
        # 优先使用K线数据（回测和实盘都支持）
        if kline_df is not None and len(kline_df) >= 30:
            return self._check_with_kline(position, current_price, kline_df)
        
        # 实盘模式：尝试用当日高低价构建最小K线
        if self.mode == 'live' and (today_high is not None or today_low is not None):
            return self._check_with_intraday(position, current_price, today_high, today_low)
        
        # 降级到简化止损
        return self._check_simple(position, current_price)
    
    def _check_with_kline(self, position: Dict, current_price: float, 
                          kline_df: pd.DataFrame) -> Tuple[bool, str, float, float, float]:
        """
        使用K线数据检查（最准确）
        
        这是推荐的方式，回测和实盘都应该尽量提供K线数据。
        """
        strategy = self._get_exit_strategy()
        return strategy.check_exit(position, current_price, kline_df)

def check_exit_unified(config: Dict,
                       position: Dict,
                       current_price: float,
                       kline_df: Optional[pd.DataFrame] = None,
                       current_date: Optional[str] = None,
                       mode: str = 'live') -> Tuple[bool, str, float, float, float]:
    """
    便捷函数 - 统一退出检查
    
    这是一个便捷的顶层函数，用于快速调用退出检查，无需创建类实例。
    
    Args:
        config: 配置字典
        position: 持仓信息
        current_price: 当前价格
        kline_df: K线数据（可选）
        current_date: 当前日期（回测需要）
        mode: 运行模式，'backtest' 或 'live'
        
    Returns:
        (should_exit, reason, atr, take_profit_price, stop_loss_price)
        
    Example:
        # 回测中使用
        should_exit, reason, atr, tp, sl = check_exit_unified(
            config, position, current_price, kline_df=df, current_date='2024-01-15', mode='backtest'
        )
        
        # 实盘中使用
        should_exit, reason, atr, tp, sl = check_exit_unified(
            config, position, current_price, kline_df=df, mode='live'
        )
    """
    strategy = UnifiedExitStrategy(config, mode=mode)
    return strategy.check_exit(
        position, current_price, 
        kline_df=kline_df, 
        current_date=current_date
    )


def create_position_dict(stock_code: str,
                         quantity: int,
                         cost_price: float,
                         buy_date: str,
                         highest_price: Optional[float] = None,
                         **extra_fields) -> Dict:
    """
    创建标准化的持仓字典
    
    这是一个辅助函数，用于创建符合退出策略要求的持仓字典。
    
    Args:
        stock_code: 股票代码
        quantity: 持仓数量
        cost_price: 成本价
        buy_date: 买入日期（格式 'YYYY-MM-DD'）
        highest_price: 最高价（默认为成本价）
        **extra_fields: 其他字段（如 manual, listing_date 等）
        
    Returns:
        标准化的持仓字典
        
    Example:
        position = create_position_dict(
            stock_code='HK.00700',
            quantity=100,
            cost_price=350.0,
            buy_date='2024-01-15',
            manual=False
        )
    """
    position = {
        'stock_code': stock_code,
        'quantity': quantity,
        'cost_price': cost_price,
        'buy_date': buy_date,
        'highest_price': highest_price or cost_price,
    }
    position.update(extra_fields)
    return position
