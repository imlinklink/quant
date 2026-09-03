"""
统一持仓管理基类 - 回测和实盘共用接口

提供持仓管理的抽象接口，回测和实盘分别继承实现。
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field
from datetime import datetime
import logging

from ..data.base_fetcher import MarketType

logger = logging.getLogger(__name__)


def _normalize_market_type(market_type: Union[str, MarketType]) -> MarketType:
    """标准化市场类型（支持字符串和枚举）"""
    if isinstance(market_type, MarketType):
        return market_type
    if isinstance(market_type, str):
        market_str = market_type.upper()
        if market_str == 'HK':
            return MarketType.HK
    raise ValueError(f"不支持的市场类型: {market_type}")


@dataclass
class Position:
    """持仓数据模型"""
    stock_code: str
    quantity: int
    cost_price: float
    buy_date: str
    highest_price: float = field(default=None)
    stop_price: float = field(default=0.0)
    take_profit_price: float = field(default=0.0)
    atr: float = field(default=0.0)
    manual: bool = field(default=False)
    stock_name: str = field(default='')
    listing_date: Optional[str] = field(default=None)
    
    def __post_init__(self):
        if self.highest_price is None:
            self.highest_price = self.cost_price
    
    def to_dict(self) -> Dict:
        """转换为字典（用于序列化）"""
        return {
            'stock_code': self.stock_code,
            'quantity': self.quantity,
            'cost_price': self.cost_price,
            'buy_date': self.buy_date,
            'highest_price': self.highest_price,
            'stop_price': self.stop_price,
            'take_profit_price': self.take_profit_price,
            'atr': self.atr,
            'manual': self.manual,
            'stock_name': self.stock_name,
            'listing_date': self.listing_date,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'Position':
        """从字典创建"""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
    
    @property
    def market_value(self) -> float:
        """持仓市值（需要当前价格，这里返回成本市值）"""
        return self.quantity * self.cost_price
    
    @property
    def profit_pct(self, current_price: float = None) -> float:
        """盈亏百分比（需要提供当前价格）"""
        if current_price is None:
            return 0.0
        return (current_price - self.cost_price) / self.cost_price


@dataclass
class TradeRecord:
    """交易记录"""
    stock_code: str
    stock_name: str
    quantity: int
    price: float
    direction: str  # 'BUY' 或 'SELL'
    trade_time: datetime = field(default_factory=datetime.now)
    order_id: Optional[str] = field(default=None)
    reason: Optional[str] = field(default=None)  # 卖出原因
    market_type: str = field(default='HK')  # 'HK'
    env: str = field(default='SIMULATE')  # 'REAL' 或 'SIMULATE'
    trading_cost: Dict = field(default_factory=dict)  # 交易成本明细
    
    def __post_init__(self):
        if isinstance(self.trade_time, str):
            self.trade_time = datetime.fromisoformat(self.trade_time)


class BasePositionManager(ABC):
    """
    持仓管理基类 - 回测和实盘共用逻辑
    
    子类需要实现:
    - execute_buy: 执行买入
    - execute_sell: 执行卖出
    - save_state: 保存状态
    """
    
    def __init__(self, config: Dict, market_type: Union[str, MarketType], initial_capital: float = None):
        """
        初始化持仓管理器
        
        Args:
            config: 配置字典
            market_type: 市场类型 'HK' 或 MarketType 枚举
            initial_capital: 初始资金（可选，从config读取）
        """
        self.config = config or {}
        self.market_type = _normalize_market_type(market_type)
        
        # 资金配置
        self.initial_capital = initial_capital or self.config.get('strategy', {}).get('initial_capital', 100000.0)
        self.strategy_capital = self.initial_capital
        self.strategy_used_capital = 0.0
        
        # 持仓数据
        self.positions: Dict[str, Position] = {}  # {stock_code: Position}
        self.recently_stopped: Dict[str, str] = {}  # 止损冷却期 {stock_code: stop_date}
        self.stop_loss_cooldown_days = self.config.get('risk', {}).get('stop_loss_cooldown_days', 20)
        
        # 交易记录
        self.trade_records: List[TradeRecord] = []
        
        # 锁（实盘需要，回测可忽略）
        self._position_lock = None  # 子类初始化
        
    # ==================== 持仓查询 ====================
    
    def get_position(self, stock_code: str) -> Optional[Position]:
        """获取单个持仓"""
        return self.positions.get(stock_code)
    
    def get_all_positions(self) -> Dict[str, Position]:
        """获取所有持仓"""
        return self.positions.copy()
    
    def get_position_codes(self) -> List[str]:
        """获取所有持仓代码"""
        return list(self.positions.keys())
    
    def has_position(self, stock_code: str) -> bool:
        """是否有持仓"""
        pos = self.positions.get(stock_code)
        return pos is not None and pos.quantity > 0
    
    def get_position_count(self) -> int:
        """持仓数量"""
        return len(self.positions)
    
    def get_total_market_value(self, price_map: Dict[str, float] = None) -> float:
        """
        获取总市值
        
        Args:
            price_map: 价格映射 {stock_code: current_price}
        """
        total = 0.0
        for code, pos in self.positions.items():
            if price_map and code in price_map:
                total += pos.quantity * price_map[code]
            else:
                total += pos.market_value
        return total
    
    def get_available_cash(self) -> float:
        """获取可用资金"""
        return self.strategy_capital - self.strategy_used_capital
    
    # ==================== 冷却期管理 ====================
    
    def is_in_cooldown(self, stock_code: str, current_date: str = None) -> bool:
        """
        检查是否在止损冷却期内
        
        Args:
            stock_code: 股票代码
            current_date: 当前日期（回测需要，格式 'YYYY-MM-DD'）
        """
        if stock_code not in self.recently_stopped:
            return False
        
        stop_date = self.recently_stopped[stock_code]
        
        if current_date:
            # 回测模式
            stop_dt = datetime.strptime(stop_date, '%Y-%m-%d')
            current_dt = datetime.strptime(current_date, '%Y-%m-%d')
            days_since = (current_dt - stop_dt).days
        else:
            # 实盘模式
            days_since = (datetime.now() - datetime.strptime(stop_date, '%Y-%m-%d')).days
        
        return days_since < self.stop_loss_cooldown_days
    
    def add_cooldown(self, stock_code: str, date_str: Optional[str] = None):
        """
        添加冷却期
        
        Args:
            stock_code: 股票代码
            date_str: 日期字符串（格式 'YYYY-MM-DD'），默认今天
        """
        if date_str is None:
            date_str = datetime.now().strftime('%Y-%m-%d')
        self.recently_stopped[stock_code] = date_str
        logger.info(f"[{self.market_type}] 冷却期: {stock_code} 加入冷却期至 {date_str}")
    
    def cleanup_cooldown(self, current_date: str = None):
        """
        清理过期的冷却期记录
        
        Args:
            current_date: 当前日期（回测需要，格式 'YYYY-MM-DD'）
        """
        expired = []
        for code, stop_date in list(self.recently_stopped.items()):
            if current_date:
                stop_dt = datetime.strptime(stop_date, '%Y-%m-%d')
                current_dt = datetime.strptime(current_date, '%Y-%m-%d')
                days_since = (current_dt - stop_dt).days
            else:
                days_since = (datetime.now() - datetime.strptime(stop_date, '%Y-%m-%d')).days
            
            if days_since >= self.stop_loss_cooldown_days:
                expired.append(code)
        
        for code in expired:
            del self.recently_stopped[code]
            logger.info(f"[{self.market_type}] 冷却期: {code} 冷却期已过期，移除")
    
    def get_cooldown_codes(self) -> List[str]:
        """获取所有在冷却期的股票代码"""
        return list(self.recently_stopped.keys())
    
    # ==================== 持仓操作（子类实现）====================
    
    @abstractmethod
    def execute_buy(self, stock_code: str, quantity: int, price: float,
                    stock_name: str = '', **kwargs) -> Tuple[bool, Optional[TradeRecord]]:
        """
        执行买入
        
        Args:
            stock_code: 股票代码
            quantity: 买入数量
            price: 买入价格
            stock_name: 股票名称
            **kwargs: 其他参数
            
        Returns:
            (success, trade_record) - 是否成功，交易记录
        """
        pass
    
    @abstractmethod
    def execute_sell(self, stock_code: str, quantity: int, price: float,
                     reason: str = '', **kwargs) -> Tuple[bool, Optional[TradeRecord]]:
        """
        执行卖出
        
        Args:
            stock_code: 股票代码
            quantity: 卖出数量
            price: 卖出价格
            reason: 卖出原因
            **kwargs: 其他参数
            
        Returns:
            (success, trade_record) - 是否成功，交易记录
        """
        pass
    
    @abstractmethod
    def calculate_buy_quantity(self, stock_code: str, per_stock_capital: float,
                               current_price: float, total_remaining: float) -> int:
        """
        计算买入数量
        
        Args:
            stock_code: 股票代码
            per_stock_capital: 每只股票的目标资金
            current_price: 当前价格
            total_remaining: 剩余总资金
            
        Returns:
            买入数量（股数）
        """
        pass
    
    @abstractmethod
    def save_state(self):
        """保存状态（持仓、资金、冷却期等）"""
        pass
    
    # ==================== 辅助方法 ====================
    
    def get_stop_loss_reasons(self) -> List[str]:
        """获取止损原因列表"""
        return [
            'atr_stop_loss',
            'decline_stop',
            'rsrs_stop',
            'early_hard_stop'
        ]
    
    def is_stop_loss_reason(self, reason: str) -> bool:
        """判断是否为止损原因"""
        return reason in self.get_stop_loss_reasons()
    
    def record_trade(self, trade_record: TradeRecord):
        """记录交易"""
        self.trade_records.append(trade_record)
    
    def get_trade_records(self, stock_code: str = None, 
                          direction: str = None,
                          start_date: str = None,
                          end_date: str = None) -> List[TradeRecord]:
        """
        获取交易记录
        
        Args:
            stock_code: 股票代码过滤
            direction: 方向过滤 'BUY' 或 'SELL'
            start_date: 开始日期 'YYYY-MM-DD'
            end_date: 结束日期 'YYYY-MM-DD'
        """
        records = self.trade_records
        
        if stock_code:
            records = [r for r in records if r.stock_code == stock_code]
        if direction:
            records = [r for r in records if r.direction == direction]
        if start_date:
            start_dt = datetime.strptime(start_date, '%Y-%m-%d')
            records = [r for r in records if r.trade_time >= start_dt]
        if end_date:
            end_dt = datetime.strptime(end_date, '%Y-%m-%d')
            records = [r for r in records if r.trade_time <= end_dt]
        
        return records


# ==================== 子模块导出 ====================
from .backtest_position_manager import BacktestPositionManager
from .live_position_manager import LivePositionManager

__all__ = [
    'Position',
    'TradeRecord',
    'BasePositionManager',
    'BacktestPositionManager',
    'LivePositionManager',
    '_normalize_market_type',
]
