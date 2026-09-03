# -*- coding: utf-8 -*-
"""
运行回测 - 重构后版本
使用模块化设计拆分各职责
"""
import sys
import os

# 添加项目根目录到Python路径
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, project_root)

from mutifactor.utils import setup_logger
from mutifactor.config import Config
from mutifactor.data import FutuHKDataFetcher

# 导入拆分后的模块 (支持直接运行和包导入两种方式)
try:
    from .config_loader import load_config
    from .connection_checker import check_futu_connection
    from .backtest_engine import BacktestEngine
    from .result_saver import ResultSaver
    from .chart_generator import ChartGenerator
    from .backtest_utils import print_hk_results
except ImportError:
    from config_loader import load_config
    from connection_checker import check_futu_connection
    from backtest_engine import BacktestEngine
    from result_saver import ResultSaver
    from chart_generator import ChartGenerator
    from backtest_utils import print_hk_results


def run_backtest_with_futu(
    stock_codes: list = None,
    start_date: str = None,
    end_date: str = None,
    initial_capital: float = None,
    strategy_name: str = 'momentum',
    config_file: str = 'config.yaml',
    disable_market_analysis: bool = False
):
    """使用富途接口运行回测"""
    # 加载配置
    config = load_config(config_file)

    # 设置日志
    log_path = os.path.join(Config.backtest.OUTPUT_DIR, Config.backtest.BACKTEST_LOG_FILE)
    logger = setup_logger('backtest', log_file=log_path)

    # 使用配置文件或参数默认值
    if initial_capital is None:
        initial_capital = config.get('strategy', {}).get('initial_capital', 100000.0)

    try:
        # 创建回测引擎并运行
        engine = BacktestEngine(config, logger)
        results = engine.run(
            stock_codes=stock_codes,
            start_date=start_date,
            end_date=end_date,
            strategy_name=strategy_name
        )

        if not results:
            logger.error("回测失败")
            return None

        # 保存结果
        saver = ResultSaver(Config.backtest.OUTPUT_DIR)
        saver.save_run_results(results, config)
        saver.save_csv_results(results)

        # 生成对比图表（可选，无网络时跳过）
        if 'equity_curve' in results and len(results['equity_curve']) > 0:
            try:
                with FutuHKDataFetcher(host=Config.futu.HOST, port=Config.futu.PORT) as fetcher:
                    chart_gen = ChartGenerator(Config.backtest.OUTPUT_DIR)
                    chart_gen.generate_comparison_chart(
                        results['equity_curve'],
                        results['start_date'],
                        results['end_date'],
                        fetcher,
                        logger
                    )
            except Exception as e:
                logger.warning(f"生成对比图表失败（可能无网络）: {e}")
                print("⚠️ 对比图表生成失败，回测结果不受影响")

        return results

    except Exception as e:
        logger.error(f"回测过程异常: {e}")
        import traceback
        logger.error(traceback.format_exc())
        print(f"\n❌ 回测失败: {e}")
        return None


def main():
    """主入口"""
    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == 'check':
            check_futu_connection()
        elif command == 'backtest':
            strategy = 'momentum'
            config_file = 'config.yaml'
            enable_market_analysis = False
            end_date = None

            i = 2
            while i < len(sys.argv):
                arg = sys.argv[i]
                if arg == 'backtest':
                    pass
                elif arg == '--end-date' and i + 1 < len(sys.argv):
                    end_date = sys.argv[i + 1]
                    i += 1
                elif not arg.startswith('--'):
                    strategy = arg
                i += 1

            config = load_config(config_file)

            # 验证回测配置
            from scripts.live_trading.config_validator import (
                validate_required_config,
                validate_backtest_config,
                ConfigValidationError
            )
            try:
                validate_required_config(config, [
                    'strategy.initial_capital',
                ], "回测")
                validate_backtest_config(config)
            except ConfigValidationError as e:
                print(f"\n❌ 回测配置验证失败:\n{e}\n")
                return

            # 检查富途连接（可选，仅提示，不阻塞回测）
            futu_connected = check_futu_connection()
            if futu_connected:
                print(f"\n✅ 富途连接正常，数据库缺失时可自动补充数据")
            else:
                print(f"\n⚠️ 富途未连接，将使用数据库已有数据进行回测")

            print(f"\n开始回测 [{strategy}]策略...")
            print(f"使用配置文件: {config_file}")

            results = run_backtest_with_futu(
                strategy_name=strategy,
                config_file=config_file,
                disable_market_analysis=not enable_market_analysis,
                end_date=end_date
            )

            if results:
                print_hk_results(results)
        else:
            print("未知命令")
    else:
        print("使用方法:")
        print("  python run_hk_backtest.py check              # 检查富途连接")
        print("  python run_hk_backtest.py backtest [策略名] [--end-date YYYY-MM-DD]  # 运行回测")
        print("\n可用策略:")
        print("  momentum - 动量策略")
        print("\n可选参数:")
        print("  --end-date YYYY-MM-DD   截止日期（默认今天）")


if __name__ == "__main__":
    main()
