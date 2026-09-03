"""
实盘持仓管理器基类 - 继承自统一基类

提供实盘环境下的持仓管理实现，港股和美股分别继承。
"""

from typing import Dict, Optional, Tuple, List
from datetime import datetime
from threading import Lock
import logging

from ..position import BasePositionManager, Position, TradeRecord, _normalize_market_type
from ..base_market_adapter import MarketAdapter

logger = logging.getLogger(__name__)


class LivePositionManager(BasePositionManager):
    """
    实盘持仓管理器基类
    
    特点:
    - 实际交易，有网络IO
    - 使用 MarketAdapter 计算交易成本
    - 支持止损冷却期
    - 线程安全（使用锁）
    - 与券商同步持仓
    
    子类需要实现:
    - _sync_with_broker_impl: 与券商同步持仓的具体实现
    """
    
    def __init__(self, config: Dict, market_type: str, trader, initial_capital: float = None):
        """
        初始化实盘持仓管理器
        
        Args:
            config: 配置字典
            market_type: 市场类型 'HK'
            trader: 交易器实例（FutuTrader）
            initial_capital: 初始资金
        """
        super().__init__(config, market_type, initial_capital)

        self.trader = trader
        self.market_adapter = MarketAdapter(self.market_type)  # 使用标准化后的枚举
        
        # 线程锁（实盘需要）
        self._position_lock = Lock()
        
        # 订单超时
        self.order_timeout = config.get('trading', {}).get('order_timeout', 30)
        
        # 手动持仓代码集合（从数据库加载）
        self.manual_position_codes: set = set()
    
    def execute_buy(self, stock_code: str, quantity: int, price: float,
                    stock_name: str = '', **kwargs) -> Tuple[bool, Optional[TradeRecord]]:
        """
        执行实盘买入
        
        Args:
            stock_code: 股票代码
            quantity: 买入数量
            price: 买入价格（用于计算金额，实际成交价格可能不同）
            stock_name: 股票名称
            
        Returns:
            (success, trade_record)
        """
        if quantity <= 0:
            logger.error(f"[{self.market_type_str}] 买入数量无效: {quantity}")
            return False, None
        
        try:
            # 调用交易器下单
            order_id, avg_price, dealt_qty = self.trader.place_order(
                stock_code=stock_code,
                quantity=quantity,
                order_type=kwargs.get('order_type', 'MARKET'),
                side='buy',
                timeout=self.order_timeout
            )
            
            if dealt_qty <= 0:
                logger.warning(f"[{self.market_type_str}] 买入 {stock_code} 成交数量为0")
                return False, None
            
            # 计算实际成本
            actual_amount = dealt_qty * avg_price
            trading_cost = self.market_adapter.calculate_trading_cost(actual_amount, 'buy', stock_code)
            total_cost = actual_amount + trading_cost['total']
            
            # 更新持仓（在锁内）
            with self._position_lock:
                if stock_code in self.positions:
                    # 加仓
                    old_pos = self.positions[stock_code]
                    total_qty = old_pos.quantity + dealt_qty
                    total_cost_amount = old_pos.cost_price * old_pos.quantity + avg_price * dealt_qty
                    new_cost = total_cost_amount / total_qty
                    
                    old_pos.quantity = total_qty
                    old_pos.cost_price = new_cost
                    old_pos.highest_price = max(old_pos.highest_price, avg_price)
                else:
                    # 新建持仓
                    buy_date = datetime.now().strftime('%Y-%m-%d')
                    self.positions[stock_code] = Position(
                        stock_code=stock_code,
                        quantity=dealt_qty,
                        cost_price=avg_price,
                        buy_date=buy_date,
                        highest_price=avg_price,
                        stock_name=stock_name,
                        manual=False
                    )
                
                # 更新资金
                self.strategy_used_capital += total_cost
                
                # 保存状态
                self.save_state()
            
            # 创建交易记录
            trade_record = TradeRecord(
                stock_code=stock_code,
                stock_name=stock_name,
                quantity=dealt_qty,
                price=avg_price,
                direction='BUY',
                order_id=order_id,
                market_type=self.market_type_str,
                env='REAL',
                trading_cost=trading_cost
            )
            self.record_trade(trade_record)
            
            logger.info(
                f"[{self.market_type_str}] 买入完成: {stock_code}, "
                f"成交 {dealt_qty}股 @ {avg_price:.3f}, 总成本 {total_cost:.2f}"
            )
            return True, trade_record
            
        except Exception as e:
            logger.error(f"[{self.market_type_str}] 买入失败 {stock_code}: {e}", exc_info=True)
            return False, None
    
    def execute_sell(self, stock_code: str, quantity: int, price: float,
                     reason: str = '', **kwargs) -> Tuple[bool, Optional[TradeRecord]]:
        """
        执行实盘卖出
        
        Args:
            stock_code: 股票代码
            quantity: 卖出数量（0表示全仓）
            price: 卖出价格（参考）
            reason: 卖出原因
            
        Returns:
            (success, trade_record)
        """
        with self._position_lock:
            if stock_code not in self.positions:
                logger.warning(f"[{self.market_type_str}] 卖出时持仓不存在: {stock_code}")
                return False, None
            
            position = self.positions[stock_code]
            is_manual = position.manual
            cost_price = position.cost_price
            stock_name = position.stock_name
            
            if quantity <= 0:
                quantity = position.quantity
            
            if quantity > position.quantity:
                quantity = position.quantity
            
            # 标记为正在卖出
            position._selling = True
        
        # 在锁外执行卖出（网络IO）
        try:
            order_id, avg_price, dealt_qty = self.trader.place_order(
                stock_code=stock_code,
                quantity=quantity,
                order_type=kwargs.get('order_type', 'MARKET'),
                side='sell',
                timeout=self.order_timeout
            )
            
            if dealt_qty <= 0:
                logger.warning(f"[{self.market_type_str}] 卖出 {stock_code} 成交数量为0")
                with self._position_lock:
                    if stock_code in self.positions:
                        self.positions[stock_code].__dict__.pop('_selling', None)
                return False, None
            
            # 计算实际成交
            actual_amount = dealt_qty * avg_price
            trading_cost = self.market_adapter.calculate_trading_cost(actual_amount, 'sell', stock_code)
            net_amount = actual_amount - trading_cost['total']
            
            actual_cost = dealt_qty * cost_price
            gross_profit = actual_amount - actual_cost
            net_profit = net_amount - actual_cost
            
        except Exception as e:
            logger.error(f"[{self.market_type_str}] 卖出失败 {stock_code}: {e}", exc_info=True)
            with self._position_lock:
                if stock_code in self.positions:
                    self.positions[stock_code].__dict__.pop('_selling', None)
            return False, None
        
        # 在锁内更新状态
        with self._position_lock:
            try:
                position = self.positions.get(stock_code)
                if position is None:
                    logger.warning(f"[{self.market_type_str}] 卖出后持仓已不存在: {stock_code}")
                    return False, None
                
                # 更新资金
                if not is_manual:
                    self.strategy_used_capital -= actual_cost
                    if self.strategy_used_capital < 0:
                        self.strategy_used_capital = 0
                    self.strategy_capital += net_profit
                
                # 更新或删除持仓
                remaining = position.quantity - dealt_qty
                if remaining > 0:
                    position.quantity = remaining
                else:
                    del self.positions[stock_code]
                
                # 止损卖出 → 加入冷却期
                if self.is_stop_loss_reason(reason) and not is_manual:
                    self.add_cooldown(stock_code)
                
                # 保存状态
                self.save_state()
                
            except Exception as e:
                logger.error(f"[{self.market_type_str}] 更新持仓状态失败 {stock_code}: {e}", exc_info=True)
                return False, None
        
        # 创建交易记录
        trade_record = TradeRecord(
            stock_code=stock_code,
            stock_name=stock_name,
            quantity=dealt_qty,
            price=avg_price,
            direction='SELL',
            order_id=order_id,
            reason=reason,
            market_type=self.market_type,
            env='REAL',
            trading_cost=trading_cost
        )
        self.record_trade(trade_record)
        
        profit_color = '+' if net_profit >= 0 else ''
        logger.info(
            f"[{self.market_type_str}] 卖出完成: {stock_code}, "
            f"成交 {dealt_qty}股 @ {avg_price:.3f}, "
            f"净盈亏: {profit_color}{net_profit:.2f}, 原因: {reason}"
        )
        return True, trade_record
    
    def calculate_buy_quantity(self, stock_code: str, per_stock_capital: float,
                               current_price: float, total_remaining: float) -> int:
        """
        计算实盘买入数量
        
        子类可以覆盖此方法以实现市场特定的逻辑（如港股的手数调整）。
        
        Args:
            stock_code: 股票代码
            per_stock_capital: 每只股票的目标资金
            current_price: 当前价格
            total_remaining: 剩余总资金
            
        Returns:
            买入股数
        """
        # 默认实现：直接计算股数
        raw_shares = int(per_stock_capital // current_price)
        return max(0, raw_shares)
    
    def save_state(self):
        """
        保存状态到数据库
        
        子类应该覆盖此方法以实现具体的数据库操作。
        """
        # 基类提供空实现，子类覆盖
        logger.debug(f"[{self.market_type_str}] 保存状态（基类空实现）")
    
    def sync_with_broker(self) -> bool:
        """
        与券商同步持仓
        
        从券商获取实际持仓，更新本地状态。
        
        Returns:
            是否同步成功
        """
        try:
            broker_positions = self.trader.get_positions()
            
            # 过滤已平仓
            active_positions = [p for p in broker_positions if p.get('quantity', 0) > 0]
            broker_codes = {p['stock_code'] for p in active_positions}
            
            with self._position_lock:
                local_codes = set(self.positions.keys())
                
                # 找出差异
                to_remove = local_codes - broker_codes
                to_add = broker_codes - local_codes
                to_update = local_codes & broker_codes
                
                # 删除已平仓的
                for code in to_remove:
                    if not self.positions[code].manual:
                        logger.info(f"[{self.market_type_str}] 同步: 删除已平仓 {code}")
                        del self.positions[code]
                
                # 添加新持仓
                for pos in active_positions:
                    code = pos['stock_code']
                    if code in to_add:
                        self.positions[code] = Position(
                            stock_code=code,
                            quantity=pos['quantity'],
                            cost_price=pos.get('cost_price', pos.get('price', 0)),
                            buy_date=pos.get('buy_date', datetime.now().strftime('%Y-%m-%d')),
                            highest_price=pos.get('price', 0),
                            manual=code in self.manual_position_codes
                        )
                        logger.info(f"[{self.market_type_str}] 同步: 添加新持仓 {code}")
                
                # 更新现有持仓
                for pos in active_positions:
                    code = pos['stock_code']
                    if code in to_update:
                        self.positions[code].quantity = pos['quantity']
                        # 可选：更新成本价等
            
            logger.info(f"[{self.market_type_str}] 持仓同步完成: 券商 {len(active_positions)} 只, 本地 {len(self.positions)} 只")
            return True
            
        except Exception as e:
            logger.error(f"[{self.market_type_str}] 持仓同步失败: {e}", exc_info=True)
            return False
    
    def get_position_summary(self) -> Dict:
        """获取持仓摘要"""
        with self._position_lock:
            total_quantity = sum(p.quantity for p in self.positions.values())
            manual_count = sum(1 for p in self.positions.values() if p.manual)
            
            return {
                'market_type': self.market_type,
                'position_count': len(self.positions),
                'total_quantity': total_quantity,
                'manual_count': manual_count,
                'strategy_capital': self.strategy_capital,
                'strategy_used_capital': self.strategy_used_capital,
                'available_cash': self.get_available_cash(),
                'cooldown_count': len(self.recently_stopped),
            }
