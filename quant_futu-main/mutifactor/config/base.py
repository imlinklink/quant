"""
基础配置
"""

import logging


class BaseConfig:
    """基础配置"""

    # 日志配置
    LOG_LEVEL = logging.DEBUG
    LOG_FORMAT = '%(asctime)s - %(levelname)s - %(message)s'

    # 数据格式
    DATE_FORMAT = '%Y-%m-%d'
    DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S'
