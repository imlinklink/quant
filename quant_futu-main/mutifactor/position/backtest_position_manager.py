"""
回测持仓管理器 - 继承自统一基类

提供回测环境下的持仓管理实现。
"""

from typing import Dict, Optional, Tuple
from datetime import datetime
import logging

from ..position import BasePositionManager, Position, TradeRecord, _normalize_market_type
from ..base_market_adapter import MarketAdapter

logger = logging.getLogger(__name__)


class BacktestPositionManager(BasePositionManager):
    """
    回测持仓管理器
    
    特点:
    - 模拟交易，无实际网络IO
    - 使用 MarketAdapter 计算交易成本
    - 支持止损冷却期
    """
    
    def __init__(self, config: Dict, market_type: str, initial_capital: float = None):
        """
        初始化回测持仓管理器
        
        Args:
            config: 配置字典
            market_type: 市场类型 'HK'
            initial_capital: 初始资金
        """
        super().__init__(config, market_type, initial_capital)

        # 市场适配器（计算交易成本）- 使用标准化后的市场类型
        self.market_adapter = MarketAdapter(self.market_type)
        
        # 回测专用：记录买入信息（用于区分量化/手动）
        self.buy_records: Dict[str, Dict] = {}  # {stock_code: buy_info}
        
        # 回测不需要锁
        self._position_lock = None
    
    def execute_buy(self, stock_code: str, quantity: int, price: float,
                    stock_name: str = '', buy_date: str = None,
                    manual: bool = False, **kwargs) -> Tuple[bool, Optional[TradeRecord]]:
        """
        执行回测买入
        
        Args:
            stock_code: 股票代码
            quantity: 买入数量
            price: 买入价格
            stock_name: 股票名称
            buy_date: 买入日期 'YYYY-MM-DD'
            manual: 是否手动买入
            
        Returns:
            (success, trade_record)
        """
        if quantity <= 0:
            logger.warning(f"[回测] 买入数量无效: {quantity}")
            return False, None
        
        if buy_date is None:
            buy_date = datetime.now().strftime('%Y-%m-%d')
        
        # 计算交易成本
        amount = quantity * price
        trading_cost = self.market_adapter.calculate_trading_cost(amount, 'buy', stock_code)
        total_cost = amount + trading_cost['total']
        
        # 检查资金
        available = self.get_available_cash()
        if total_cost > available:
            logger.warning(f"[回测] 资金不足: 需要 {total_cost:.2f}, 可用 {available:.2f}")
            return False, None
        
        # 创建持仓
        position = Position(
            stock_code=stock_code,
            quantity=quantity,
            cost_price=price,
            buy_date=buy_date,
            highest_price=price,
            manual=manual,
            stock_name=stock_name
        )
        
        # 更新持仓
        if stock_code in self.positions:
            # 加仓：计算新的成本价
            old_pos = self.positions[stock_code]
            total_quantity = old_pos.quantity + quantity
            total_cost_amount = old_pos.cost_price * old_pos.quantity + price * quantity
            new_cost = total_cost_amount / total_quantity
            
            position.quantity = total_quantity
            position.cost_price = new_cost
            position.buy_date = old_pos.buy_date  # 保持原始买入日期
            position.highest_price = max(old_pos.highest_price, price)
        
        self.positions[stock_code] = position
        
        # 记录买入信息
        self.buy_records[stock_code] = {
            'buy_date': buy_date,
            'quantity': quantity,
            'price': price,
            'manual': manual
        }
        
        # 更新资金
        self.strategy_used_capital += total_cost
        
        # 创建交易记录
        trade_record = TradeRecord(
            stock_code=stock_code,
            stock_name=stock_name,
            quantity=quantity,
            price=price,
            direction='BUY',
            trade_time=datetime.strptime(buy_date, '%Y-%m-%d'),
            market_type=self.market_type_str,
            env='SIMULATE',
            trading_cost=trading_cost
        )
        self.record_trade(trade_record)
        
        logger.info(f"[{self.market_type_str}] 买入 {stock_code}: {quantity}股 @ {price:.3f}, 成本 {total_cost:.2f}")
        return True, trade_record
    
    def execute_sell(self, stock_code: str, quantity: int, price: float,
                     reason: str = '', sell_date: str = None,
                     **kwargs) -> Tuple[bool, Optional[TradeRecord]]:
        """
        执行回测卖出
        
        Args:
            stock_code: 股票代码
            quantity: 卖出数量
            price: 卖出价格
            reason: 卖出原因
            sell_date: 卖出日期 'YYYY-MM-DD'
            
        Returns:
            (success, trade_record)
        """
        if stock_code not in self.positions:
            logger.warning(f"[回测] 持仓不存在: {stock_code}")
            return False, None
        
        position = self.positions[stock_code]
        
        if quantity <= 0:
            quantity = position.quantity  # 全仓卖出
        
        if quantity > position.quantity:
            logger.warning(f"[回测] 卖出数量超过持仓: {quantity} > {position.quantity}")
            quantity = position.quantity
        
        if sell_date is None:
            sell_date = datetime.now().strftime('%Y-%m-%d')
        
        # 计算交易成本
        amount = quantity * price
        trading_cost = self.market_adapter.calculate_trading_cost(amount, 'sell', stock_code)
        net_amount = amount - trading_cost['total']
        
        # 计算盈亏
        cost_amount = quantity * position.cost_price
        gross_profit = amount - cost_amount
        net_profit = net_amount - cost_amount
        
        # 更新资金
        if not position.manual:
            # 量化持仓：更新已用资金和总资金
            self.strategy_used_capital -= cost_amount
            if self.strategy_used_capital < 0:
                self.strategy_used_capital = 0
            self.strategy_capital += net_profit
        
        # 更新或删除持仓
        remaining = position.quantity - quantity
        if remaining > 0:
            position.quantity = remaining
        else:
            del self.positions[stock_code]
            if stock_code in self.buy_records:
                del self.buy_records[stock_code]
        
        # 止损卖出 → 加入冷却期
        if self.is_stop_loss_reason(reason) and not position.manual:
            self.add_cooldown(stock_code, sell_date)
        
        # 创建交易记录
        trade_record = TradeRecord(
            stock_code=stock_code,
            stock_name=position.stock_name,
            quantity=quantity,
            price=price,
            direction='SELL',
            trade_time=datetime.strptime(sell_date, '%Y-%m-%d'),
            reason=reason,
            market_type=self.market_type_str,
            env='SIMULATE',
            trading_cost=trading_cost
        )
        self.record_trade(trade_record)

        profit_color = '+' if net_profit >= 0 else ''
        logger.info(
            f"[{self.market_type_str}] 卖出 {stock_code}: {quantity}股 @ {price:.3f}, "
            f"盈亏: {profit_color}{net_profit:.2f}, 原因: {reason}"
        )
        return True, trade_record
    
    def calculate_buy_quantity(self, stock_code: str, per_stock_capital: float,
                               current_price: float, total_remaining: float) -> int:
        """
        计算回测买入数量
        
        Args:
            stock_code: 股票代码
            per_stock_capital: 每只股票的目标资金
            current_price: 当前价格
            total_remaining: 剩余总资金
            
        Returns:
            买入股数
        """
        return self.market_adapter.calculate_shares(per_stock_capital, current_price, stock_code)
    
    def save_state(self):
        """保存回测状态（回测不需要持久化）"""
        # 回测状态保存在内存中，不需要额外保存
        pass
    
    def get_portfolio_summary(self, price_map: Dict[str, float] = None) -> Dict:
        """
        获取组合摘要
        
        Args:
            price_map: 当前价格映射 {stock_code: price}
            
        Returns:
            组合摘要字典
        """
        total_market_value = 0.0
        total_cost = 0.0
        unrealized_pnl = 0.0
        
        for code, pos in self.positions.items():
            cost = pos.quantity * pos.cost_price
            total_cost += cost
            
            if price_map and code in price_map:
                market_value = pos.quantity * price_map[code]
                unrealized_pnl += market_value - cost
            else:
                market_value = cost
            
            total_market_value += market_value
        
        return {
            'market_type': self.market_type_str,
            'initial_capital': self.initial_capital,
            'strategy_capital': self.strategy_capital,
            'strategy_used_capital': self.strategy_used_capital,
            'available_cash': self.get_available_cash(),
            'position_count': len(self.positions),
            'total_market_value': total_market_value,
            'total_cost': total_cost,
            'unrealized_pnl': unrealized_pnl,
            'total_assets': self.strategy_capital + total_market_value - total_cost,
        }
