"""
实盘交易枚举定义
"""
from enum import Enum, auto
from mutifactor.trading.futu_constants import OrderStatus


class ConnectionState(Enum):
    """连接状态枚举"""
    DISCONNECTED = auto()
    CONNECTING = auto()
    CONNECTED = auto()


class OrderType(Enum):
    """订单类型枚举"""
    MARKET = auto()       # 市价单
    LIMIT = auto()        # 限价单
    STOP = auto()         # 止损单
    STOP_LIMIT = auto()   # 止损限价单