"""
策略模块
"""

from .momentum import MomentumStrategy
from .unified_exit_strategy import (
    UnifiedExitStrategy,
    check_exit_unified,
    create_position_dict,
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_TAKE_PROFIT,
    EXIT_REASON_TIME_EXIT,
    EXIT_REASON_DECLINE_STOP,
    EXIT_REASON_RSRS_STOP,
    EXIT_REASON_EARLY_HARD_STOP,
    EXIT_REASON_RSRS_VOL_TAKE_PROFIT,
)

__all__ = [
    'MomentumStrategy',
    'UnifiedExitStrategy',
    'check_exit_unified',
    'create_position_dict',
    'EXIT_REASON_STOP_LOSS',
    'EXIT_REASON_TAKE_PROFIT',
    'EXIT_REASON_TIME_EXIT',
    'EXIT_REASON_DECLINE_STOP',
    'EXIT_REASON_RSRS_STOP',
    'EXIT_REASON_EARLY_HARD_STOP',
    'EXIT_REASON_RSRS_VOL_TAKE_PROFIT',
]
