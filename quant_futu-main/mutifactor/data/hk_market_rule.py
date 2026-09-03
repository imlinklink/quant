"""
港股市场规则实现
"""

import pandas as pd
from mutifactor.data.base_fetcher import MarketRuleBase, MarketType
from mutifactor.config.constants import TradingConstants


class HKMarketRule(MarketRuleBase):
    """港股市场规则"""

    # 港股最小交易单位(每手股数)映射
    # 根据价格区间确定每手股数
    LOT_SIZE_CONFIG = {
        'default': 100,  # 默认100股
    }

    # 港股交易成本配置
    TRADING_COST_CONFIG = {
        'commission_rate': TradingConstants.HK_COMMISSION_RATE,
        'commission_min': TradingConstants.MIN_COMMISSION_HKD,
        'stamp_duty_rate': TradingConstants.HK_STAMP_DUTY_RATE,
        'trading_fee_rate': TradingConstants.HK_TRADING_LEVY_RATE,
        'settlement_fee_rate': TradingConstants.HK_SETTLEMENT_FEE_RATE,
        'slippage_rate': TradingConstants.HK_SLIPPAGE_RATE,
    }

    def __init__(self, data_fetcher=None):
        """
        初始化港股市场规则

        参数:
            data_fetcher: 数据获取器，用于获取真实的每手股数（可选）
        """
        super().__init__(MarketType.HK)
        self._data_fetcher = data_fetcher

    def format_stock_code(self, code: str) -> str:
        """
        格式化股票代码为富途格式
        例如: '00700' -> 'HK.00700'
        """
        # 如果已经是富途格式,直接返回
        if code.startswith('HK.'):
            return code

        # 标准化代码(去除前导零)
        normalized_code = code.zfill(5) if len(code) < 5 else code

        return f"HK.{normalized_code}"

    def calculate_shares(self, cash: float, price: float, stock_code: str) -> int:
        """
        根据资金和价格计算可买入股数
        港股按手交易,每手股数从data_fetcher获取真实值

        参数:
            cash: 可用资金
            price: 股票价格
            stock_code: 股票代码

        返回:
            可买入股数(整手)
        """
        if price == 0:
            return 0

        # 获取每手股数（优先使用data_fetcher，其次使用缓存或默认值）
        lot_size = self._get_lot_size(stock_code)

        # 计算原始股数
        raw_shares = int(cash / price)

        # 向下取整到手的倍数
        shares = (raw_shares // lot_size) * lot_size

        return max(0, shares)

    def _get_lot_size(self, stock_code: str) -> int:
        """
        获取股票的每手股数

        参数:
            stock_code: 股票代码

        返回:
            每手股数
        """
        # 从数据库获取
        try:
            from mutifactor.infra.yaml_storage import yaml_storage
            lot_size = yaml_storage.get_lot_size(stock_code)
            if lot_size:
                return lot_size
        except Exception:
            pass

        # 从data_fetcher获取（实时API）
        if self._data_fetcher and hasattr(self._data_fetcher, 'get_lot_size'):
            try:
                lot_size = self._data_fetcher.get_lot_size(stock_code)
                return lot_size
            except Exception:
                pass

        # 使用默认值
        return self.LOT_SIZE_CONFIG.get('default', 100)

    def get_trading_cost_config(self, stock_code):
        """
        获取交易成本配置

        参数:
            stock_code: 股票代码

        返回:
            交易成本配置字典
        """
        config = {
            'commission_rate': self.TRADING_COST_CONFIG['commission_rate'],
            'commission_min': self.TRADING_COST_CONFIG['commission_min'],
            'stamp_duty_rate': self.TRADING_COST_CONFIG['stamp_duty_rate'],
            'trading_fee_rate': self.TRADING_COST_CONFIG['trading_fee_rate'],
            'settlement_fee_rate': self.TRADING_COST_CONFIG['settlement_fee_rate'],
            'slippage_rate': self.TRADING_COST_CONFIG['slippage_rate'],
        }

        return config

    def is_trading_time(self, timestamp: pd.Timestamp) -> bool:
        """
        判断是否为港股交易时间

        港股交易时间:
        - 上午: 09:30 - 12:00
        - 下午: 13:00 - 16:00
        - 周一至周五(节假日除外)

        参数:
            timestamp: 时间戳

        返回:
            是否为交易时间
        """
        # 检查是否为工作日
        if timestamp.dayofweek >= 5:  # 周六、周日
            return False

        # 检查交易时间段
        hour = timestamp.hour
        minute = timestamp.minute
        time_value = hour * 100 + minute

        # 上午 09:30 - 12:00
        morning_start = 930
        morning_end = 1200

        # 下午 13:00 - 16:00
        afternoon_start = 1300
        afternoon_end = 1600

        return (morning_start <= time_value <= morning_end) or \
               (afternoon_start <= time_value <= afternoon_end)
