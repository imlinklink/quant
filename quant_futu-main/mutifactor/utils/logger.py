"""
日志工具 - 支持日志轮转和结构化日志
"""

import logging
import os
import json
import sys
from datetime import datetime
from typing import Dict, Any, Optional
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path


# 默认日志目录
DEFAULT_LOG_DIR = Path(__file__).parent.parent.parent / 'logs'


class StructuredFormatter(logging.Formatter):
    """结构化日志格式化器 - 支持JSON格式输出"""
    
    def __init__(self, json_format: bool = False):
        super().__init__()
        self.json_format = json_format
    
    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录"""
        # 基础信息
        log_data: Dict[str, Any] = {
            'timestamp': datetime.fromtimestamp(record.created).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
        }
        
        # 添加异常信息
        if record.exc_info:
            log_data['exception'] = self.formatException(record.exc_info)
        
        # 添加额外字段（如果有的话）
        if hasattr(record, 'stock_code'):
            log_data['stock_code'] = record.stock_code
        if hasattr(record, 'order_id'):
            log_data['order_id'] = record.order_id
        if hasattr(record, 'trade_amount'):
            log_data['trade_amount'] = record.trade_amount
        
        if self.json_format:
            return json.dumps(log_data, ensure_ascii=False)
        else:
            # 人类可读格式
            base_msg = f"{log_data['timestamp']} - {log_data['logger']} - {log_data['level']} - {log_data['message']}"
            extra_parts = []
            if 'stock_code' in log_data:
                extra_parts.append(f"stock={log_data['stock_code']}")
            if 'order_id' in log_data:
                extra_parts.append(f"order={log_data['order_id']}")
            if extra_parts:
                base_msg += f" | {' '.join(extra_parts)}"
            if 'exception' in log_data:
                base_msg += f"\n{log_data['exception']}"
            return base_msg


def setup_logger(
    name: str, 
    log_file: str = None, 
    level: int = None,
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5,
    json_format: bool = False
) -> logging.Logger:
    """
    设置日志（支持轮转）

    Args:
        name: 日志名称
        log_file: 日志文件路径
        level: 日志级别
        max_bytes: 单个日志文件最大大小（字节），默认10MB
        backup_count: 保留的备份文件数量，默认5个
        json_format: 是否使用JSON格式输出

    Returns:
        Logger实例
    """
    logger = logging.getLogger(name)

    if level is None:
        level = logging.INFO

    logger.setLevel(level)

    # 清除已有的handlers
    logger.handlers.clear()

    # 阻止日志消息冒泡到父logger（避免重复打印）
    logger.propagate = False

    # 创建格式化器
    formatter = StructuredFormatter(json_format=json_format)

    # 控制台handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件handler（带轮转）
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 使用RotatingFileHandler实现日志轮转
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def setup_logging(
    level: int = logging.INFO, 
    log_file: str = None,
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5,
    json_format: bool = False,
    when: str = 'midnight'  # 按 天轮转
) -> None:
    """
    设置根日志记录器
    
    Args:
        level: 日志级别
        log_file: 日志文件路径
        max_bytes: 单个日志文件最大大小（字节），默认10MB
        backup_count: 保留的备份文件数量，默认5个
        json_format: 是否使用JSON格式输出
        when: 时间轮转周期，'midnight' 每天轮转，'H' 每小时轮转
    """
    # 设置根日志级别
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # 清除所有现有的handlers
    root_logger.handlers.clear()

    # 创建格式化器
    formatter = StructuredFormatter(json_format=json_format)

    # 添加控制台handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 如果指定了日志文件，添加带轮转的文件handler
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 使用RotatingFileHandler实现大小轮转
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # 设置第三方库的日志级别
    logging.getLogger('futu').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)


def get_trade_logger(log_dir: str = None) -> logging.Logger:
    """
    获取专门的交易日志记录器（用于审计）
    
    Args:
        log_dir: 日志目录
        
    Returns:
        交易日志Logger实例
    """
    if log_dir is None:
        log_dir = DEFAULT_LOG_DIR
    else:
        log_dir = Path(log_dir)
    
    log_dir.mkdir(parents=True, exist_ok=True)
    
    trade_log_file = log_dir / 'trades.log'
    
    # 创建专门的交易日志记录器
    trade_logger = logging.getLogger('trade_audit')
    trade_logger.setLevel(logging.INFO)
    trade_logger.handlers.clear()
    
    # 使用JSON格式便于后续分析
    formatter = StructuredFormatter(json_format=True)
    
    # 文件handler（独立轮转）
    file_handler = RotatingFileHandler(
        trade_log_file,
        maxBytes=50 * 1024 * 1024,  # 50MB，交易日志可能更大
        backupCount=10,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    trade_logger.addHandler(file_handler)
    
    # 不向父logger传播
    trade_logger.propagate = False
    
    return trade_logger


def log_trade(
    logger: logging.Logger,
    action: str,
    stock_code: str,
    quantity: int,
    price: float,
    order_id: str = None,
    **kwargs: Any
) -> None:
    """
    记录交易日志（辅助函数）
    
    Args:
        logger: 日志记录器
        action: 交易动作 ('buy', 'sell')
        stock_code: 股票代码
        quantity: 数量
        price: 价格
        order_id: 订单ID
        **kwargs: 其他额外信息
    """
    extra = {
        'stock_code': stock_code,
        'order_id': order_id,
        'trade_amount': quantity * price,
    }
    
    msg = f"TRADE | {action.upper()} | {stock_code} | qty={quantity} | price={price:.3f} | amount={extra['trade_amount']:.2f}"
    
    if order_id:
        msg += f" | order_id={order_id}"
    
    for key, value in kwargs.items():
        msg += f" | {key}={value}"
    
    logger.info(msg, extra=extra)
