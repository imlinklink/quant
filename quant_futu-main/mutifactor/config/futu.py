"""
富途API配置
"""


class FutuConfig:
    """富途OpenD配置"""

    # 连接配置
    HOST = '127.0.0.1'
    PORT = 11111

    # 数据缓存
    CACHE_DIR = './cache'
    CACHE_ENABLED = True
    CACHE_EXPIRE_DAYS = 7

    # K线数据配置
    KLINE_TYPE = 'K_DAY'
    KLINE_FIELDS = ['date', 'open', 'high', 'low', 'close', 'volume']

    # 请求配置
    MAX_RETRY = 3
    REQUEST_TIMEOUT = 30
