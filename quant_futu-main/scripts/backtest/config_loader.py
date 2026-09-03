# -*- coding: utf-8 -*-
"""
配置加载模块
"""
import yaml


def load_config(config_path: str = 'config.yaml') -> dict:
    """加载配置文件"""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        return config if config else {}
    except FileNotFoundError:
        print(f"配置文件 {config_path} 不存在，使用默认配置")
        return {}
    except yaml.YAMLError as e:
        print(f"配置文件解析错误: {e}")
        return {}
