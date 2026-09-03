"""
实盘交易异常类定义
"""


class TradingError(Exception):
    """交易基础异常"""
    pass


class ConnectionError(TradingError):
    """连接异常"""
    def __init__(self, message="Connection failed", retry_count=0):
        super().__init__(message)
        self.retry_count = retry_count
        self.message = message

    def __str__(self):
        return f"ConnectionError(retry={self.retry_count}): {self.message}"


class TimeoutError(TradingError):
    """超时异常"""
    def __init__(self, message="Operation timeout", order_id=None):
        super().__init__(message)
        self.order_id = order_id
        self.message = message

    def __str__(self):
        if self.order_id:
            return f"TimeoutError(order={self.order_id}): {self.message}"
        return f"TimeoutError: {self.message}"


class OrderError(TradingError):
    """订单异常"""
    def __init__(self, message="Order operation failed", order_id=None, code=None):
        super().__init__(message)
        self.order_id = order_id
        self.code = code
        self.message = message

    def __str__(self):
        details = []
        if self.order_id:
            details.append(f"order={self.order_id}")
        if self.code:
            details.append(f"code={self.code}")
        detail_str = ", ".join(details)
        return f"OrderError({detail_str}): {self.message}"


# ==================== 数据相关异常 ====================

class DataError(TradingError):
    """数据获取/处理异常"""
    def __init__(self, message="Data operation failed", data_source=None, stock_code=None):
        super().__init__(message)
        self.data_source = data_source
        self.stock_code = stock_code

    def __str__(self):
        details = []
        if self.data_source:
            details.append(f"source={self.data_source}")
        if self.stock_code:
            details.append(f"code={self.stock_code}")
        detail_str = ", ".join(details)
        if detail_str:
            return f"DataError({detail_str}): {self.message}"
        return f"DataError: {self.message}"


class DataFetchError(DataError):
    """数据获取失败"""
    pass


class DataFormatError(DataError):
    """数据格式错误"""
    pass


# ==================== 数据库相关异常 ====================

class DatabaseError(TradingError):
    """数据库操作异常"""
    def __init__(self, message="Database operation failed", operation=None, table=None):
        super().__init__(message)
        self.message = message
        self.operation = operation
        self.table = table

    def __str__(self):
        details = []
        if self.operation:
            details.append(f"op={self.operation}")
        if self.table:
            details.append(f"table={self.table}")
        detail_str = ", ".join(details)
        if detail_str:
            return f"DatabaseError({detail_str}): {self.message}"
        return f"DatabaseError: {self.message}"


class DatabaseConnectionError(DatabaseError):
    """数据库连接失败"""
    pass


class DatabaseQueryError(DatabaseError):
    """数据库查询失败"""
    pass


class DatabaseIntegrityError(DatabaseError):
    """数据库完整性错误（如唯一约束冲突）"""
    pass


# ==================== 配置相关异常 ====================

class ConfigError(TradingError):
    """配置异常"""
    pass


class ConfigNotFoundError(ConfigError):
    """配置文件不存在"""
    pass


class ConfigParseError(ConfigError):
    """配置解析错误"""
    pass


# ==================== 策略相关异常 ====================

class StrategyError(TradingError):
    """策略执行异常"""
    pass


class SelectionError(StrategyError):
    """选股异常"""
    pass


class PositionError(TradingError):
    """持仓操作异常"""
    pass


# ==================== 网络相关异常 ====================

class NetworkError(TradingError):
    """网络请求异常"""
    def __init__(self, message="Network operation failed", url=None, status_code=None):
        super().__init__(message)
        self.url = url
        self.status_code = status_code

    def __str__(self):
        details = []
        if self.url:
            details.append(f"url={self.url}")
        if self.status_code:
            details.append(f"status={self.status_code}")
        detail_str = ", ".join(details)
        if detail_str:
            return f"NetworkError({detail_str}): {self.message}"
        return f"NetworkError: {self.message}"


class APIRateLimitError(NetworkError):
    """API限流异常"""
    pass


class APIAuthenticationError(NetworkError):
    """API认证失败"""
    pass