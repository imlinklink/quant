"""
港股实盘交易管理器 - 基于基类实现
"""
from datetime import time as dt_time
from typing import Dict, Tuple

from mutifactor.trading import FutuTrader
from mutifactor.data import get_hk_stock_name, FutuHKDataFetcher

from .live_manager_base import LiveTradingManager
from .hk_position_manager import HKPositionManager


class HKLiveTradingManager(LiveTradingManager):
    """港股实盘交易管理器"""

    def __init__(self, config: Dict):
        super().__init__(config, market_type='HK')

    def _create_position_manager(self) -> HKPositionManager:
        """创建港股持仓管理器"""
        return HKPositionManager(
            config=self.config,
            trader=self.trader,
            state_persistence=self.state_persistence,
            price_fetcher=self.price_fetcher
        )

    def _get_selection_time_window(self) -> Tuple[dt_time, dt_time]:
        """
        获取港股选股时间窗口

        9:30-16:00（完整交易时段），历史K线数据开盘即可算，
        提前选出候选池，让下午买入时机有更多机会。

        Returns:
            (9:30, 16:00)
        """
        return dt_time(9, 30), dt_time(16, 0)

    def _get_trading_time_window(self) -> Tuple[dt_time, dt_time]:
        """
        获取港股完整交易时间窗口（用于平仓检查）

        Returns:
            (9:30, 16:00) - 完整交易时间（包含午休）
        """
        return dt_time(9, 30), dt_time(16, 0)

    def _create_trader(self, futu_config: Dict, env) -> FutuTrader:
        """创建港股交易器"""
        return FutuTrader(
            host=futu_config.get('host', '127.0.0.1'),
            port=futu_config.get('port', 11111),
            env=env,
        )

    def _get_stock_name(self, stock_code: str) -> str:
        """获取港股名称"""
        return get_hk_stock_name(stock_code)

    def _get_data_fetcher_class(self):
        """获取港股数据获取器类"""
        return FutuHKDataFetcher

    def _calculate_buy_quantity(self, stock_code: str, per_stock_capital: float,
                                current_price: float, total_remaining: float) -> int:
        """
        计算港股买入数量（按手数调整）

        Returns:
            买入数量，0 表示跳过
        """
        lot_size = self.trader.get_lot_size(stock_code)
        raw_shares = int(per_stock_capital // current_price)
        quantity = (raw_shares // lot_size) * lot_size

        # 如果计算数量不足一手，但剩余资金足够买入一手，则买入一手
        if quantity < lot_size:
            one_lot_cost = lot_size * current_price
            if total_remaining >= one_lot_cost:
                quantity = lot_size
            else:
                return 0

        return quantity
