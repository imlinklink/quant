"""
策略框架 - 提供统一的策略测试接口
"""

import pandas as pd
from typing import Dict, List, Optional
import logging

from mutifactor.base import BaseStrategy


class StrategyFactory:
    """策略工厂 - 用于创建策略实例"""

    _strategies = {}

    @classmethod
    def register_strategy(cls, strategy_name: str, strategy_class: type):
        """注册策略"""
        cls._strategies[strategy_name] = strategy_class
        logging.info(f"策略已注册: {strategy_name}")

    @classmethod
    def create_strategy(cls, strategy_name: str, **kwargs) -> Optional[BaseStrategy]:
        """创建策略实例"""
        strategy_class = cls._strategies.get(strategy_name)
        if strategy_class:
            return strategy_class(**kwargs)
        else:
            logging.error(f"策略未找到: {strategy_name}")
            return None

    @classmethod
    def list_strategies(cls) -> List[str]:
        """列出所有已注册策略"""
        return list(cls._strategies.keys())


class BacktestRunner:
    """回测运行器 - 统一管理回测流程"""

    def __init__(self, strategy_name: str, strategy_params: dict = None):
        """
        初始化回测运行器

        Args:
            strategy_name: 策略名称
            strategy_params: 策略参数
        """
        self.strategy_name = strategy_name
        self.strategy_params = strategy_params or {}
        self.logger = logging.getLogger(f"BacktestRunner.{strategy_name}")
        # 预先创建策略实例，用于传递市场状态信息
        self.strategy_instance = None

    def prepare_data(self, data: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
        """准备数据"""
        strategy_data = {}

        for stock_code, df in data.items():
            try:
                if 'date' not in df.columns:
                    self.logger.warning(f"{stock_code} 缺少date列,跳过")
                    continue

                required_cols = ['date', 'open', 'high', 'low', 'close', 'volume']
                if not all(col in df.columns for col in required_cols):
                    self.logger.warning(f"{stock_code} 缺少必要的列,跳过")
                    continue

                df_copy = df.copy()
                df_copy['date'] = pd.to_datetime(df_copy['date'])
                # 转换数值列为float，避免Decimal类型导致计算错误
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    if col in df_copy.columns:
                        df_copy[col] = df_copy[col].astype(float)
                df_copy = df_copy.sort_values('date').reset_index(drop=True)
                df_copy = df_copy.dropna(subset=required_cols)

                if len(df_copy) < 30:
                    self.logger.warning(f"{stock_code} 数据量不足({len(df_copy)}天),跳过")
                    continue

                strategy_data[stock_code] = df_copy

            except (KeyError, ValueError, TypeError, IndexError) as e:
                self.logger.warning(f"处理 {stock_code} 数据错误: {e}")
                continue
            except Exception as e:
                self.logger.error(f"处理 {stock_code} 未知错误: {e}", exc_info=True)
                raise

        self.logger.info(f"数据准备完成,有效股票数: {len(strategy_data)}")
        return strategy_data

    def run_backtest(self, data: Dict[str, pd.DataFrame],
                     start_date: str = None, end_date: str = None) -> Dict:
        """运行回测"""
        # 如果已经有了预先创建的策略实例，使用它
        if self.strategy_instance:
            strategy = self.strategy_instance
        else:
            # 否则创建新的策略实例
            strategy = StrategyFactory.create_strategy(self.strategy_name, **self.strategy_params)

        if strategy is None:
            self.logger.error(f"创建策略失败: {self.strategy_name}")
            return None

        prepared_data = self.prepare_data(data)

        if not prepared_data:
            self.logger.error("没有有效的数据")
            return None

        self.logger.info(f"开始回测: {self.strategy_name}")
        results = strategy.run_backtest(prepared_data, start_date, end_date)

        # 保存回测结果与参数配置
        if results and hasattr(strategy, 'save_backtest_result'):
            strategy.save_backtest_result(results)

        return results

    def set_strategy_instance(self, strategy_instance):
        """设置策略实例（用于传递市场状态等信息）"""
        self.strategy_instance = strategy_instance
        self.logger.info(f"已设置策略实例: {self.strategy_name}")


class ModelEvaluator:
    """模型评估器 - 比较不同策略的表现"""

    @staticmethod
    def compare_strategies(results_list: List[Dict], strategy_names: List[str]) -> pd.DataFrame:
        """比较多个策略的表现"""
        comparison = []

        for results, name in zip(results_list, strategy_names):
            if results:
                comparison.append({
                    '策略名称': name,
                    '初始资金': results.get('initial_capital', 0),
                    '最终价值': results.get('final_value', 0),
                    '总收益率': f"{results.get('total_return', 0)*100:.2f}%",
                    '年化收益率': f"{results.get('annual_return', 0)*100:.2f}%",
                    '最大回撤': f"{results.get('max_drawdown', 0)*100:.2f}%",
                    '夏普比率': f"{results.get('sharpe_ratio', 0):.2f}",
                    '交易次数': results.get('total_trades', 0),
                })

        return pd.DataFrame(comparison)

    @staticmethod
    def find_best_strategy(results_list: List[Dict], strategy_names: List[str],
                           metric: str = 'sharpe_ratio') -> tuple:
        """找出最优策略"""
        if metric == 'max_drawdown':
            best_idx = min(range(len(results_list)),
                          key=lambda i: results_list[i].get(metric, float('-inf')))
        else:
            best_idx = max(range(len(results_list)),
                          key=lambda i: results_list[i].get(metric, float('-inf')))

        return strategy_names[best_idx], results_list[best_idx]


class DataValidator:
    """数据验证器"""

    @staticmethod
    def validate_data(data: Dict[str, pd.DataFrame]) -> tuple:
        """验证数据质量"""
        issues = []

        if not data:
            issues.append("数据为空")
            return False, issues

        for stock_code, df in data.items():
            if df is None or len(df) == 0:
                issues.append(f"{stock_code}: 数据为空")
                continue

            required_cols = ['date', 'open', 'high', 'low', 'close', 'volume']
            missing_cols = [col for col in required_cols if col not in df.columns]
            if missing_cols:
                issues.append(f"{stock_code}: 缺少列 {missing_cols}")

            if len(df) < 30:
                issues.append(f"{stock_code}: 数据量不足 ({len(df)} < 30)")

            if df.isnull().any().any():
                null_cols = df.columns[df.isnull().any()].tolist()
                issues.append(f"{stock_code}: 存在空值 {null_cols}")

        is_valid = len(issues) == 0
        return is_valid, issues
