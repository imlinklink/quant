# -*- coding: utf-8 -*-
"""
配置验证模块 - 启动时检查关键配置
"""
import logging

logger = logging.getLogger(__name__)


class ConfigValidationError(Exception):
    """配置验证错误"""
    pass


def validate_required_config(config: dict, required_fields: list, context: str = ""):
    """
    验证必需的配置项是否存在
    
    Args:
        config: 配置字典
        required_fields: 必需的字段列表，格式如 "section.key"
        context: 上下文信息（用于错误提示）
    
    Raises:
        ConfigValidationError: 配置缺失时抛出
    """
    missing = []
    for field in required_fields:
        parts = field.split('.')
        value = config
        for part in parts:
            if not isinstance(value, dict) or part not in value:
                missing.append(field)
                break
            value = value[part]
    
    if missing:
        context_str = f" [{context}]" if context else ""
        raise ConfigValidationError(
            f"配置验证失败{context_str}：缺少以下必需配置项:\n  " + 
            "\n  ".join(f"- {f}" for f in missing)
        )


def validate_live_trading_config(config: dict):
    """
    验证实盘交易的必需配置
    
    Args:
        config: 配置字典
        
    Raises:
        ConfigValidationError: 配置缺失时抛出
    """
    # 富途连接配置
    validate_required_config(config, [
        'trading.futu.host',
        'trading.futu.port',
    ], "实盘交易")
    
    # 检查环境配置
    env = config.get('trading', {}).get('env', 'SIMULATE')
    if env == 'REAL':
        logger.warning("⚠️ 实盘交易模式已启用 (env=REAL)，将执行真实交易！")
    else:
        logger.info(f"交易环境: SIMULATE (模拟交易)")


def validate_backtest_config(config: dict):
    """
    验证回测的必需配置
    
    Args:
        config: 配置字典
        
    Raises:
        ConfigValidationError: 配置缺失时抛出
    """
    # 初始资金
    validate_required_config(config, [
        'strategy.initial_capital',
    ], "回测")
    
    initial_capital = config.get('strategy', {}).get('initial_capital', 0)
    if initial_capital <= 0:
        raise ConfigValidationError(
            f"回测配置错误：initial_capital 必须大于 0，当前值: {initial_capital}"
        )


def validate_strategy_config(config: dict):
    """
    验证策略配置的合理性
    
    Args:
        config: 配置字典
    """
    # 最大持仓数
    max_positions = config.get('momentum', {}).get('max_positions', 3)
    if max_positions <= 0:
        logger.warning(f"⚠️ max_positions={max_positions} 无效，设为默认值 3")
        config.setdefault('momentum', {})['max_positions'] = 3
    
    # 最小动量得分
    min_momentum = config.get('momentum', {}).get('min_momentum_score', 0.02)
    if min_momentum < 0:
        logger.warning(f"⚠️ min_momentum_score={min_momentum} 不能为负，设为 0")
        config.setdefault('momentum', {})['min_momentum_score'] = 0
