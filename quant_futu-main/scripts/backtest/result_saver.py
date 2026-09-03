# -*- coding: utf-8 -*-
"""
结果保存模块 - 保存CSV文件（数据库保存已禁用）
"""
import os
import sys
import pandas as pd

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

# MySQL→YAML迁移：禁用数据库保存
from mutifactor.utils import setup_logger  # no YAMLStorage import needed

# 配置日志
logger = setup_logger('result_saver')


class ResultSaver:
    """结果保存器 - 仅使用数据库"""

    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def save_run_results(self, results: dict, config: dict):
        """保存运行结果（CSV格式）"""
        # MySQL→YAML迁移：禁用数据库保存，只输出CSV
        self.save_csv_results(results)
        logger.info("✅ 回测结果已保存为CSV")

    def save_csv_results(self, results: dict):
        """保存CSV格式的结果文件"""
        import pandas as pd

        # 保存净值曲线
        if 'equity_curve' in results:
            equity_file = os.path.join(self.output_dir, 'equity_curve.csv')
            results['equity_curve'].to_csv(equity_file, index=False, encoding='utf-8-sig')

        # 保存交易历史
        if 'trade_history' in results and len(results['trade_history']) > 0:
            trade_file = os.path.join(self.output_dir, 'trade_history.csv')
            results['trade_history'].to_csv(trade_file, index=False, encoding='utf-8-sig')
