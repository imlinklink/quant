"""
配置模块
"""

from .base import BaseConfig
from .futu import FutuConfig
from .backtest import BacktestConfig

class Config:
    """综合配置类"""

    # 基础配置
    base = BaseConfig

    # 富途API配置
    futu = FutuConfig

    # 回测配置
    backtest = BacktestConfig


__all__ = [
    'Config',
    'BaseConfig',
    'FutuConfig',
    'BacktestConfig',
]
