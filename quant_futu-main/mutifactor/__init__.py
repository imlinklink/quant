"""
Mutifactor - 多因子量化交易策略框架
"""

__version__ = "1.0.0"
__author__ = "Quant Team"

from .strategies import MomentumStrategy
from .framework import StrategyFactory, BacktestRunner, ModelEvaluator, DataValidator
from .base import BaseStrategy
from .config import Config

__all__ = [
    'BaseStrategy',
    'MomentumStrategy',
    'StrategyFactory',
    'BacktestRunner',
    'ModelEvaluator',
    'DataValidator',
    'Config',
]
