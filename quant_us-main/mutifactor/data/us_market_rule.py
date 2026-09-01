"""
美股市场规则
"""
from typing import Dict
import pandas as pd
from mutifactor.data.base_fetcher import MarketRuleBase, MarketType


class USMarketRule(MarketRuleBase):
    """美股市场规则实现"""

    def __init__(self):
        super().__init__(MarketType.US)

    def format_stock_code(self, code: str) -> str:
        """
        格式化股票代码为统一格式
        例如: US.AAPL
        """
        if code.startswith('US.'):
            return code
        return f"US.{code}"

    def calculate_shares(self, cash: float, price: float, stock_code: str) -> int:
        """
        根据资金和价格计算可买入股数
        美股无手数限制，至少买1股
        """
        if price <= 0:
            return 0
        shares = int(cash / price)
        return max(shares, 0)

    def get_trading_cost_config(self, stock_code: str) -> Dict[str, float]:
        """
        获取交易成本配置
        返回包含: commission, stamp_duty, trading_fee, settlement_fee, slippage
        """
        return {
            'commission': 0.0005,
            'stamp_duty': 0.0,
            'trading_fee': 0.0001,
            'settlement_fee': 0.0,
            'slippage': 0.001
        }

    def is_trading_time(self, timestamp: pd.Timestamp) -> bool:
        """
        判断是否为美股交易时间
        美股常规交易时间: 9:30-16:00 ET
        """
        # 简化实现，回测中通常不需要精确判断
        return True
