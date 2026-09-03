"""
统一配置加载器 - 消除代码重复
"""
from pathlib import Path
from typing import Dict

from mutifactor.utils.config import load_config


def get_project_config(config_path: str = None) -> Dict:
    """
    获取项目配置（自动查找config.yaml）
    
    Args:
        config_path: 配置文件路径，如果为None则自动查找
        
    Returns:
        配置字典
    """
    if config_path is None:
        # 自动查找项目根目录下的config.yaml
        project_root = Path(__file__).parent.parent.parent
        config_path = project_root / 'config.yaml'
        
        # 如果找不到，使用当前工作目录
        if not config_path.exists():
            config_path = 'config.yaml'
    
    return load_config(str(config_path))


def get_default_config_path() -> str:
    """获取默认配置文件路径"""
    project_root = Path(__file__).parent.parent.parent
    config_path = project_root / 'config.yaml'
    
    if config_path.exists():
        return str(config_path)
    return 'config.yaml'
