#!/usr/bin/env python3
"""
清空选股结果脚本
用于清除过时的选股快照数据和交易状态
"""
import sys
import os

# 添加项目根目录到Python路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from scripts.live_trading.state_persistence import StatePersistence
from mutifactor.infra.yaml_storage import yaml_storage

def clear_selection_results():
    """清空选股结果和交易状态"""
    try:
        # 1. 清空选股结果
        state = StatePersistence()
        state.save_selection_results([])
        print("✅ 选股结果已清空")
        
        # 2. MySQL→YAML迁移：trading_state表已不存在，跳过数据库清理
        print("⚠️  交易状态清空已禁用（MySQL→YAML迁移）")
        print("✅ 选股结果已清空，交易状态请手动清理")
        
        return True
    except Exception as e:
        print(f"❌ 清空失败: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    clear_selection_results()
