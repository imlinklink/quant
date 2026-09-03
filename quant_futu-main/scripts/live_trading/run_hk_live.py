#!/usr/bin/env python3
"""
实盘交易入口脚本
支持多种运行模式: start/stop/status/manual
"""
import argparse
import logging
import sys
import time
from pathlib import Path

# 添加项目根目录到Python路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from scripts.live_trading.hk_live_manager import HKLiveTradingManager
from mutifactor.trading import FutuTrader
from mutifactor.utils.config_loader import get_project_config
from mutifactor.utils.logger import setup_logging

logger = logging.getLogger(__name__)


def setup_args():
    """设置命令行参数"""
    parser = argparse.ArgumentParser(description='实盘交易系统')
    subparsers = parser.add_subparsers(dest='command', help='命令')

    # start 命令
    start_parser = subparsers.add_parser('start', help='启动交易')
    start_parser.add_argument('-c', '--config', default='config.yaml',
                             help='配置文件路径')
    start_parser.add_argument('--debug', action='store_true',
                             help='调试模式')

    # stop 命令
    subparsers.add_parser('stop', help='停止交易')

    # status 命令
    subparsers.add_parser('status', help='查看状态')

    # manual 命令
    manual_parser = subparsers.add_parser('manual', help='手动操作')
    manual_parser.add_argument('-s', '--stock', required=True,
                              help='股票代码')
    manual_parser.add_argument('-a', '--action', required=True,
                              choices=['buy', 'sell', 'close'],
                              help='操作类型')
    manual_parser.add_argument('-q', '--quantity', type=int,
                              help='数量(买入时需要)')
    manual_parser.add_argument('-p', '--price', type=float,
                              help='价格(限价单时需要)')

    return parser.parse_args()


def start_trading(config_path: str, debug: bool = False):
    """启动交易"""
    import signal

    try:
        config = get_project_config(config_path)

        # 验证配置
        from scripts.live_trading.config_validator import (
            validate_required_config, 
            validate_live_trading_config,
            ConfigValidationError
        )
        try:
            validate_required_config(config, [
                'trading.futu.host',
                'trading.futu.port',
            ], "实盘交易")
            validate_live_trading_config(config)
            logger.info("✅ 配置验证通过")
        except ConfigValidationError as e:
            logger.error(f"❌ 配置验证失败: {e}")
            print(f"\n❌ 配置验证失败:\n{e}\n")
            return False

        # 设置日志
        log_level = logging.DEBUG if debug else logging.INFO
        setup_logging(level=log_level)

        # 保存进程信息
        from scripts.live_trading.process_manager import process_manager
        import os

        process_info = {
            'config_path': config_path,
            'debug': debug,
            'start_time': time.time()
        }
        process_manager.save_process_info(os.getpid(), process_info)

        manager = HKLiveTradingManager(config)

        # 设置信号处理 - 使用标志位控制退出
        shutdown_requested = False

        def signal_handler(signum, frame):
            nonlocal shutdown_requested
            signal_name = 'SIGINT' if signum == signal.SIGINT else 'SIGTERM'
            logger.info(f"收到 {signal_name} 信号，正在优雅退出...")
            manager.stop()
            process_manager.cleanup()
            shutdown_requested = True

        signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
        signal.signal(signal.SIGTERM, signal_handler)  # make stop / kill

        if not manager.start():
            logger.error("交易启动失败（连接步骤失败），请检查上面的错误日志")
            # 确保确认页/连接资源被释放，避免残留进程占住端口
            try:
                manager.stop()
            except Exception as e:
                logger.warning(f"清理启动失败资源时出错: {e}")
            process_manager.cleanup()
            sys.exit(1)

        logger.info("✅ 交易启动成功")
        logger.info("按 Ctrl+C 或执行 'make hk-live-stop' 停止交易")

        # 保持主线程运行，直到收到停止信号
        try:
            while not shutdown_requested:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

        # 清理进程信息
        if not shutdown_requested:
            process_manager.cleanup()

    except Exception as e:
        logger.error(f"启动失败: {e}")
        # 清理进程信息
        process_manager.cleanup()
        sys.exit(1)


def stop_trading():
    """停止交易"""
    try:
        from scripts.live_trading.process_manager import process_manager
        logger.info("正在停止运行中的交易进程...")

        if process_manager.stop_process():
            logger.info("✅ 交易进程已停止")
        else:
            logger.info("没有运行中的交易进程")

    except Exception as e:
        logger.error(f"停止进程失败: {e}")
        sys.exit(1)


def show_status():
    """显示状态"""
    try:
        import os
        from scripts.live_trading.process_manager import process_manager
        from scripts.live_trading.hk_live_manager import HKLiveTradingManager
        from mutifactor.utils.config_loader import get_project_config
        from datetime import datetime

        config = get_project_config()

        # 检查进程是否在运行
        process_info = process_manager.get_process_info()
        if process_info:
            pid = process_info.get('pid')
            info = process_info.get('info', {})
            if process_manager.is_process_running(pid):
                print(f"✅ 交易进程运行中 (PID: {pid})")
                start_time = datetime.fromtimestamp(info.get('start_time', 0))
                print(f"   启动时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"   配置: {info.get('config_path', 'N/A')}")
            else:
                print("❌ 进程已停止但状态文件存在，清理中...")
                process_manager.cleanup()
        else:
            print("❌ 没有运行中的交易进程")

        # 从数据库加载状态
        from scripts.live_trading.state_persistence import StatePersistence
        from mutifactor.infra.yaml_storage import TradingEnv

        config_env = config.get('trading', {}).get('env', 'SIMULATE')
        env_str = 'REAL' if config_env.upper() == 'REAL' else 'SIMULATE'
        trading_env = TradingEnv.REAL if env_str == 'REAL' else TradingEnv.SIMULATE

        persistence = StatePersistence()

        # 加载交易状态
        state = persistence.load_state(env=env_str)

        # 加载持仓
        positions = persistence.yaml_storage.get_positions(trading_env)

        if state or positions:
            print("\n📊 策略状态:")
            print(f"   持仓数量: {len(positions)}")

            # 从 state 或 positions 计算资金
            used_capital = state.get('used_capital', 0) if state else 0
            capital = state.get('capital', 0) if state else 0

            # 如果没有 state，从 positions 计算已用资金
            if not state and positions:
                used_capital = sum(float(p['quantity']) * float(p['cost_price']) for p in positions)

            # 如果还没有 capital，从配置文件读取
            if capital <= 0:
                capital = float(config.get('strategy', {}).get('initial_capital', 100000))

            print(f"   已用资金: {used_capital:.2f}")
            print(f"   总资金: {capital:.2f}")
            print(f"   剩余资金: {capital - used_capital:.2f}")

            if positions:
                print("\n📈 持仓明细:")
                for pos in positions:
                    code = pos.get('stock_code')
                    qty = int(pos.get('quantity', 0))
                    cost = float(pos.get('cost_price', 0))
                    high = float(pos.get('highest_price', 0))
                    name = pos.get('stock_name', code)
                    print(f"   {code} ({name}): {qty}股, 成本价 {cost:.2f}, 最高价 {high:.2f}")

            # 显示今日买入
            today_buy = persistence.get_today_buy_quantity(env=env_str)
            print(f"\n📝 今日已买入: {today_buy} 股")
        else:
            print("\n📊 无持久化状态记录")

    except (KeyError, ValueError, TypeError) as e:
        print(f"❌ 查询状态失败 - 数据错误: {e}")
    except Exception as e:
        logger.error(f"查询状态失败: {e}", exc_info=True)
        print(f"❌ 查询状态失败 - 系统错误，请查看日志")


def manual_operation(args):
    """手动操作"""
    try:
        config = get_project_config(args.config)
        setup_logging()

        # 从配置文件读取交易环境
        trading_config = config.get('trading', {})
        futu_config = trading_config.get('futu', {})

        # 转换env字符串为枚举
        from futu import TrdEnv
        env_str = trading_config.get('env', 'SIMULATE')
        env = TrdEnv.SIMULATE if env_str.upper() == 'SIMULATE' else TrdEnv.REAL

        with FutuTrader(
            host=futu_config.get('host', '127.0.0.1'),
            port=futu_config.get('port', 11111),
            env=env
        ) as trader:
            if args.action == 'buy':
                if not args.quantity:
                    print("错误: 买入操作需要指定数量 (-q)")
                    sys.exit(1)

                order_id, avg_price = trader.place_order(
                    stock_code=args.stock,
                    quantity=args.quantity,
                    order_type='MARKET',
                    price=args.price
                )
                print(f"✅ 买入成功: 订单 {order_id}, 均价 {avg_price}")

            elif args.action in ['sell', 'close']:
                if not args.quantity:
                    print("错误: 卖出操作需要指定数量 (-q)")
                    sys.exit(1)

                order_id, avg_price = trader.place_order(
                    stock_code=args.stock,
                    quantity=args.quantity,
                    order_type='MARKET',
                    side='sell',
                    price=args.price
                )
                print(f"✅ 卖出成功: 订单 {order_id}, 均价 {avg_price}")

    except Exception as e:
        logger.error(f"手动操作失败: {e}")
        sys.exit(1)


def main():
    """主函数"""
    args = setup_args()

    if not args.command:
        print("请指定命令: start, stop, status, manual")
        sys.exit(1)

    try:
        if args.command == 'start':
            start_trading(args.config, args.debug)
        elif args.command == 'stop':
            stop_trading()
        elif args.command == 'status':
            show_status()
        elif args.command == 'manual':
            manual_operation(args)

    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"程序异常: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
