"""
市场适配器
为策略类提供统一的市场规则接口
"""

from mutifactor.data.base_fetcher import MarketRuleBase, MarketType
from mutifactor.data.hk_market_rule import HKMarketRule
import logging

logger = logging.getLogger(__name__)


class MarketAdapter:
    """
    市场适配器

    将策略类与具体市场规则解耦,提供统一的接口
    """

    def __init__(self, market_type: MarketType = MarketType.HK, data_fetcher=None):
        """
        初始化市场适配器

        参数:
            market_type: 市场类型,默认港股
            data_fetcher: 数据获取器，用于获取真实的市场数据（如每手股数）
        """
        self.market_type = market_type
        self._data_fetcher = data_fetcher
        self.market_rule = self._init_market_rule()
        logger.info(f"市场适配器初始化完成,市场类型: {market_type.value}")

    def _init_market_rule(self) -> MarketRuleBase:
        """初始化市场规则"""
        if self.market_type == MarketType.HK:
            return HKMarketRule(data_fetcher=self._data_fetcher)
        else:
            raise ValueError(f"不支持的市场类型: {self.market_type}")

    def calculate_trading_cost(self, amount: float, direction: str = 'buy',
                               stock_code: str = None) -> dict:
        """
        计算交易成本

        参数:
            amount: 交易金额
            direction: 方向 'buy' 或 'sell'
            stock_code: 股票代码

        返回:
            成本字典 {commission, stamp_duty, trading_fee, settlement_fee, slippage, total}
        """
        cost_config = self.market_rule.get_trading_cost_config(stock_code)

        # 佣金
        commission = max(
            amount * cost_config['commission_rate'],
            cost_config['commission_min']
        )

        # 印花税: 仅卖出收取
        if direction == 'sell':
            stamp_duty = amount * cost_config['stamp_duty_rate']
        else:
            stamp_duty = 0.0

        # 交易征费
        trading_fee = amount * cost_config['trading_fee_rate']

        # 结算费
        settlement_fee = amount * cost_config['settlement_fee_rate']

        # 滑点
        slippage = amount * cost_config['slippage_rate']

        total_cost = commission + stamp_duty + trading_fee + settlement_fee + slippage

        return {
            'commission': commission,
            'stamp_duty': stamp_duty,
            'trading_fee': trading_fee,
            'settlement_fee': settlement_fee,
            'slippage': slippage,
            'total': total_cost
        }

    def calculate_shares(self, cash: float, price: float,
                        stock_code: str) -> int:
        """
        根据资金和价格计算可买入股数

        参数:
            cash: 可用资金
            price: 股票价格
            stock_code: 股票代码

        返回:
            可买入股数
        """
        return self.market_rule.calculate_shares(cash, price, stock_code)

    def is_trading_time(self, timestamp) -> bool:
        """
        判断是否为交易时间

        参数:
            timestamp: 时间戳

        返回:
            是否为交易时间
        """
        return self.market_rule.is_trading_time(timestamp)

    def format_stock_code(self, code: str) -> str:
        """
        格式化股票代码

        参数:
            code: 股票代码

        返回:
            格式化后的代码
        """
        return self.market_rule.format_stock_code(code)
